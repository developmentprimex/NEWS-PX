"""Poll connected Intelligence sources and persist each direct item URL once."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import case, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, engine
from app.intelligence.ai_service import analyze_news_item
from app.intelligence.generic_website_adapter import GenericWebsiteAdapter
from app.intelligence.linkedin_adapter import LinkedInCollectionError
from app.intelligence.linkedin_identity import identity_or_none, lock_key, collection_owner
from app.intelligence.linkedin_collection import collect_linkedin, incremental_start
from app.intelligence.models import NewsAnalysis, NewsItem, NewsSource, NewsSourceEmailSubscription
from app.intelligence.rss_adapter import RSSAdapter, RSSCollectionError, rss_url_is_supported
from app.intelligence.services import CollectedNewsItem
from app.intelligence.website_adapter import OfficialWebsiteAdapter, WebsiteCollectionError, WebsiteEntry, source_kind

logger = logging.getLogger(__name__)
CATEGORY_SELECTION = {
    "GRANT": {"Grants", "Funding"}, "TENDER": {"Tenders"},
    "BUSINESS": {"Business"}, "EVENT": {"Events"},
    "TECHNOLOGY": {"Technology"}, "REGULATION": {"Regulations"},
    "PARTNERSHIP": {"Partnerships"}, "NEWS": {"General News"},
}


def category_allowed(selected_categories: list[str], category: str) -> bool:
    selected = set(selected_categories)
    return not selected or "General News" in selected or bool(selected.intersection(CATEGORY_SELECTION.get(category, set())))


def linkedin_configured() -> bool:
    return bool(settings.BRIGHTDATA_API_TOKEN)


def collection_start_date(last_checked_at: datetime | None, now: datetime) -> str:
    return incremental_start(last_checked_at, now).isoformat()


def merge_linkedin_activity(previous: dict | None, observed: dict) -> dict:
    """Missing provider fields are unknown; retain earlier observations."""
    merged = dict(previous or {})
    for field, value in observed.items():
        if value is None:
            continue
        if field == "comments" and not value and observed.get("commentCount") != 0:
            continue
        if field == "kind" and value == "POST" and merged.get("kind") in {"REPOST", "ARTICLE"}:
            # POST is also the parser default when activity details are absent.
            continue
        merged[field] = value
    merged["checkedAt"] = observed.get("checkedAt") or datetime.now(timezone.utc).isoformat()
    return merged


async def _store_post(db: AsyncSession, source: NewsSource, post: CollectedNewsItem) -> bool:
    content_hash = hashlib.sha256(post.url.encode("utf-8")).hexdigest()
    existing = await db.scalar(select(NewsItem).where(
        NewsItem.source_id == source.id,
        or_(NewsItem.external_id == post.external_id, NewsItem.content_hash == content_hash, NewsItem.url == post.url),
    ).limit(1))
    if existing and source.type == "LINKEDIN" and post.linkedin_data is not None:
        # A sparse engagement-only response must not replace stored text with a
        # generated title and an empty body.
        content_changed = (post.original_text is not None or not existing.original_text) and (
            existing.title != post.title or existing.original_text != post.original_text)
        if content_changed:
            analysis = await analyze_news_item(post, source)
            saved_analysis = await db.get(NewsAnalysis, existing.id)
            if saved_analysis:
                for field, value in {
                    "summary": analysis.summary, "category": analysis.category,
                    "importance_score": analysis.importanceScore, "relevance_score": analysis.relevanceScore,
                    "why_it_matters": analysis.whyItMatters, "deadline": analysis.deadline,
                    "funding_amount": analysis.fundingAmount, "eligibility": analysis.eligibility,
                    "opportunity_type": analysis.opportunityType, "tags": analysis.tags,
                }.items():
                    setattr(saved_analysis, field, value)
                saved_analysis.analyzed_at = datetime.now(timezone.utc)
            existing.title = post.title
            existing.original_text = post.original_text
        existing.linkedin_data = merge_linkedin_activity(existing.linkedin_data, post.linkedin_data)
        if post.published_at:
            existing.published_at = post.published_at
        return False
    if existing:
        return False
    analysis = await analyze_news_item(post, source)
    if not category_allowed(source.categories or [], analysis.category):
        # An email subscription requests all activity from this source.
        subscribed = await db.scalar(select(NewsSourceEmailSubscription.source_id).where(
            NewsSourceEmailSubscription.source_id == source.id,
        ))
        if subscribed is None:
            return False
    item = NewsItem(
        source_id=source.id, external_id=post.external_id, url=post.url,
        title=post.title, original_text=post.original_text,
        published_at=post.published_at, image_url=post.image_url, content_hash=content_hash,
        linkedin_data={**post.linkedin_data, "checkedAt": post.linkedin_data.get("checkedAt") or datetime.now(timezone.utc).isoformat()} if post.linkedin_data is not None else None,
    )
    db.add(item)
    await db.flush()
    db.add(NewsAnalysis(
        news_item_id=item.id, summary=analysis.summary, category=analysis.category,
        importance_score=analysis.importanceScore, relevance_score=analysis.relevanceScore,
        why_it_matters=analysis.whyItMatters, deadline=analysis.deadline,
        funding_amount=analysis.fundingAmount, eligibility=analysis.eligibility,
        opportunity_type=analysis.opportunityType, tags=analysis.tags,
    ))
    return True


def website_entry_is_current(entry: WebsiteEntry, now: datetime) -> bool:
    if entry.closes_at:
        return entry.closes_at.date() >= now.date()
    return not entry.published_at or entry.published_at >= now - timedelta(days=60)


async def _check_website_source(db: AsyncSession, source: NewsSource, now: datetime, force: bool) -> str:
    minimum_interval = 5 if force else source.fetch_interval_minutes
    if source.last_started_at and source.last_started_at > now - timedelta(minutes=minimum_interval):
        return "pending"
    if source_kind(source.url) is None:
        posts = await GenericWebsiteAdapter(source.url).collect()
        for post in posts:
            await _store_post(db, source, post)
        source.last_started_at = now
        source.last_checked_at = now
        source.last_error = None
        await db.commit()
        return "completed"
    adapter = OfficialWebsiteAdapter(source.url)
    cutoff = now - timedelta(days=60)
    async with adapter._client() as client:
        entries = await adapter.discover(client)
        for entry in entries:
            if not website_entry_is_current(entry, now):
                continue
            exists = await db.scalar(select(NewsItem.id).where(
                NewsItem.source_id == source.id, NewsItem.url == entry.url,
            ).limit(1))
            if exists:
                continue
            try:
                post = await adapter.article(client, entry)
            except (WebsiteCollectionError, httpx.HTTPError) as exc:
                logger.warning("Could not read official article %s: %s", entry.url, exc)
                continue
            if post.published_at and post.published_at < cutoff and not entry.closes_at:
                continue
            await _store_post(db, source, post)
    source.last_started_at = now
    source.last_checked_at = now
    source.last_error = None
    await db.commit()
    return "completed"


async def _check_rss_source(db: AsyncSession, source: NewsSource, now: datetime, force: bool) -> str:
    minimum_interval = 5 if force else source.fetch_interval_minutes
    if source.last_started_at and source.last_started_at > now - timedelta(minutes=minimum_interval):
        return "pending"
    cutoff = now - timedelta(days=60)
    for post in await RSSAdapter(source.url).collect():
        if post.published_at and post.published_at < cutoff:
            continue
        await _store_post(db, source, post)
    source.last_started_at = now
    source.last_checked_at = now
    source.last_error = None
    await db.commit()
    return "completed"


async def check_source(db: AsyncSession, source_id: uuid.UUID, *, force: bool = False) -> str:
    source = (await db.execute(
        select(NewsSource).where(NewsSource.id == source_id).with_for_update(skip_locked=True)
    )).scalar_one_or_none()
    if source is None:
        return "pending"
    if source.type not in {"LINKEDIN", "WEBSITE", "RSS"} or (source.type == "WEBSITE" and not rss_url_is_supported(source.url)) or (source.type == "RSS" and not rss_url_is_supported(source.url)):
        raise LinkedInCollectionError("No collector is available for this source URL yet.")
    if source.status != "ACTIVE":
        raise LinkedInCollectionError("Activate this source before checking it.")

    now = datetime.now(timezone.utc)
    try:
        if source.type == "WEBSITE":
            return await _check_website_source(db, source, now, force)
        if source.type == "RSS":
            return await _check_rss_source(db, source, now, force)
        if not settings.BRIGHTDATA_API_TOKEN:
            return "unavailable"
        # Web requests cannot start or poll LinkedIn snapshots, including force=True.
        if force:
            return "paused"
        return await protected_linkedin_check(db, source, now)
    except (LinkedInCollectionError, WebsiteCollectionError, RSSCollectionError, httpx.HTTPError) as exc:
        await db.rollback()
        source = await db.get(NewsSource, source_id)
        if source is None:
            return "error"
        source.last_error = str(exc)[:500]
        source.last_started_at = now
        await db.commit()
        logger.warning("Intelligence source %s: %s", source.id, exc)
        return "error"


async def check_due_sources() -> dict[str, int]:
    if not settings.NEWS_ENABLE_COLLECTION:
        return {"checked": 0}
    async with SessionLocal() as db:
        sources = list((await db.execute(select(NewsSource).where(
            NewsSource.type.in_(["LINKEDIN", "WEBSITE", "RSS"]), NewsSource.status == "ACTIVE",
        ).order_by(NewsSource.last_started_at.asc().nullsfirst(),
                   case((NewsSource.priority == "HIGH", 0), (NewsSource.priority == "NORMAL", 1), else_=2),
                   NewsSource.last_new_posts.desc(), NewsSource.id))).scalars().all())
    ids = [source.id for source in sources if
           (source.type == "LINKEDIN" and linkedin_configured()) or
           (source.type == "WEBSITE" and rss_url_is_supported(source.url)) or
           (source.type == "RSS" and rss_url_is_supported(source.url))]
    checked = 0
    for source_id in ids:
        try:
            async with SessionLocal() as db:
                state = await check_source(db, source_id)
            if state in {"started", "completed"}:
                checked += 1
        except Exception:
            logger.exception("Intelligence collection failed for source %s", source_id)
    return {"checked": checked}


async def protected_linkedin_check(db, source, now, *, reason="scheduled", backfill=None):
    if not settings.NEWS_ENABLE_COLLECTION:
        return "paused"
    # Dedicated connection holds a session advisory lock through every commit.
    # Row locks alone would be released by the pre-POST reservation commit.
    identity = identity_or_none(source.url)
    if not identity:
        return "unavailable"
    key = lock_key(identity)
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if not acquired:
            return "pending"
        try:
            # Another worker may have committed since this session loaded the
            # source. Read the pending job and schedule again under the lock.
            await db.refresh(source, with_for_update=True)
            if source.type != "LINKEDIN" or source.status != "ACTIVE" or identity_or_none(source.url) != identity:
                return "paused"
            if not await collection_owner(db, source):
                await db.commit()
                return "paused"
            return await collect_linkedin(db, source, now, _store_post, reason=reason, backfill=backfill)
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
