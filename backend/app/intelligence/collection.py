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
from app.intelligence.generic_website_adapter import GenericWebsiteAdapter, GenericWebsiteError
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
        if post.published_at and (post.publication_time_verified or not existing.publication_time_verified):
            existing.published_at = post.published_at
            existing.publication_time_verified = post.publication_time_verified
        return False
    if existing:
        if post.publication_time_verified and post.published_at:
            existing.published_at, existing.publication_time_verified = post.published_at, True
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
        publication_time_verified=post.publication_time_verified,
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


async def _check_website_source(db: AsyncSession, source: NewsSource, now: datetime, force: bool, *, strict_times=False) -> str:
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
        return 'partial' if strict_times and any(not post.publication_time_verified for post in posts) else 'completed'
    adapter = OfficialWebsiteAdapter(source.url)
    cutoff = now - timedelta(days=60)
    unknown_times = False
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
            unknown_times = unknown_times or not post.publication_time_verified
    source.last_started_at = now
    source.last_checked_at = now
    source.last_error = None
    await db.commit()
    return 'partial' if strict_times and unknown_times else 'completed'


async def _check_rss_source(db: AsyncSession, source: NewsSource, now: datetime, force: bool, *, strict_times=False) -> str:
    minimum_interval = 5 if force else source.fetch_interval_minutes
    if source.last_started_at and source.last_started_at > now - timedelta(minutes=minimum_interval):
        return "pending"
    cutoff = now - timedelta(days=60)
    unknown_times = False
    for post in await RSSAdapter(source.url).collect():
        if post.published_at and post.published_at < cutoff:
            continue
        await _store_post(db, source, post)
        unknown_times = unknown_times or not post.publication_time_verified
    source.last_started_at = now
    source.last_checked_at = now
    source.last_error = None
    await db.commit()
    return 'partial' if strict_times and unknown_times else 'completed'


async def check_source(db: AsyncSession, source_id: uuid.UUID, *, force: bool = False, now: datetime | None = None,
                       expected_identity: tuple[str, str] | None = None, strict_times=False) -> str:
    source = (await db.execute(
        select(NewsSource).where(NewsSource.id == source_id).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if source is None:
        return "pending"
    if expected_identity and (source.type, source.url) != expected_identity:
        return 'identity-changed'
    if source.type not in {"LINKEDIN", "FACEBOOK", "WEBSITE", "RSS"} or (source.type == "WEBSITE" and not rss_url_is_supported(source.url)) or (source.type == "RSS" and not rss_url_is_supported(source.url)):
        raise LinkedInCollectionError("No collector is available for this source URL yet.")
    if source.status != "ACTIVE":
        raise LinkedInCollectionError("Activate this source before checking it.")

    now = now or datetime.now(timezone.utc)
    try:
        if source.type == "WEBSITE":
            return await _check_website_source(db, source, now, force, strict_times=strict_times)
        if source.type == "RSS":
            return await _check_rss_source(db, source, now, force, strict_times=strict_times)
        if source.type == 'FACEBOOK':
            if force:return 'paused'
            return await protected_facebook_check(db, source, now)
        if not settings.BRIGHTDATA_API_TOKEN:
            return "unavailable"
        # Web requests cannot start or poll LinkedIn snapshots, including force=True.
        if force:
            return "paused"
        return await protected_linkedin_check(db, source, now)
    except (LinkedInCollectionError, WebsiteCollectionError, GenericWebsiteError, RSSCollectionError, httpx.HTTPError) as exc:
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
            NewsSource.type.in_(["LINKEDIN", "FACEBOOK", "WEBSITE", "RSS"]), NewsSource.status == "ACTIVE",
        ).order_by(NewsSource.last_started_at.asc().nullsfirst(),
                   case((NewsSource.priority == "HIGH", 0), (NewsSource.priority == "NORMAL", 1), else_=2),
                   NewsSource.last_new_posts.desc(), NewsSource.id))).scalars().all())
    ids = [source.id for source in sources if
           (source.type == "LINKEDIN" and linkedin_configured()) or
           (source.type == "FACEBOOK" and settings.NEWS_FACEBOOK_FETCH_ENABLED) or
           (source.type == "WEBSITE" and rss_url_is_supported(source.url)) or
           (source.type == "RSS" and rss_url_is_supported(source.url))]
    checked = 0
    for source_id in ids:
        try:
            async with SessionLocal() as db:
                from app.intelligence.cycle_service import trial_owns_source
                if await trial_owns_source(db, source_id, datetime.now(timezone.utc)):
                    continue
                state = await check_source(db, source_id)
            if state in {"started", "completed"}:
                checked += 1
        except Exception:
            logger.exception("Intelligence collection failed for source %s", source_id)
    return {"checked": checked}


async def protected_linkedin_check(db, source, now, *, reason="scheduled", backfill=None, cycle_source_id=None, clock=None):
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
            if not cycle_source_id and reason.startswith('scheduled'):
                from app.intelligence.cycle_service import trial_owns_source
                if await trial_owns_source(db, source.id, now):
                    return 'paused'
            if source.type != "LINKEDIN" or source.status != "ACTIVE" or identity_or_none(source.url) != identity:
                return "paused"
            if not await collection_owner(db, source):
                await db.commit()
                return "paused"
            return await collect_linkedin(db, source, now, _store_post, reason=reason, backfill=backfill,
                                          cycle_source_id=cycle_source_id, clock=clock)
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


async def protected_facebook_check(db, source, now, *, reason='scheduled', backfill=None, cycle_source_id=None, clock=None):
    from app.intelligence.facebook_identity import identity_or_none as facebook_identity, source_approved
    from app.intelligence.facebook_adapter import dataset_for
    from app.intelligence.facebook_collection import refresh_facebook_billing
    identity=facebook_identity(source.url)
    source_id=source.id
    if not settings.NEWS_ENABLE_COLLECTION or not settings.NEWS_FACEBOOK_FETCH_ENABLED or not identity or not source_approved(source):
        return 'paused'
    key=lock_key(identity)
    async with engine.connect() as connection:
        if not await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}):return 'pending'
        try:
            await db.refresh(source,with_for_update=True)
            if source.type!='FACEBOOK' or source.status!='ACTIVE' or facebook_identity(source.url)!=identity or not source_approved(source):return 'paused'
            if not cycle_source_id and reason.startswith('scheduled'):
                from app.intelligence.cycle_service import trial_owns_source
                if await trial_owns_source(db,source.id,now):return 'paused'
            candidates=list(await db.scalars(select(NewsSource).where(NewsSource.type=='FACEBOOK').order_by(NewsSource.created_at,NewsSource.id)))
            owners=[s for s in candidates if facebook_identity(s.url)==identity]
            if not owners or owners[0].id!=source.id:return 'paused'
            if source.linkedin_paused_reason:
                from app.intelligence.collection_exception import allows_saturation_retry
                if not await allows_saturation_retry(db,source,cycle_source_id,now):return 'paused'
            if not source.pending_execution_id and not cycle_source_id and not backfill:
                from app.intelligence.linkedin_schedule import is_due
                if not is_due(source,now):return 'pending'
            if not source.pending_execution_id:
                try:await refresh_facebook_billing(db,now,dataset_for(source))
                except Exception:
                    await db.rollback()
                    source=await db.get(NewsSource,source_id)
                    source.last_error='Fresh shared-account billing proof for Facebook is required; no request sent.'
                    await db.commit()
                    return 'paused'
            return await collect_linkedin(db,source,now,_store_post,reason=reason,backfill=backfill,cycle_source_id=cycle_source_id,clock=clock)
        finally:await connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':key})
