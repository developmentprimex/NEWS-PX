from __future__ import annotations

import logging
import smtplib
import asyncio
import httpx
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import User, get_current_user, require_admin
from app.config import settings
from app.db import SessionLocal, get_db
from app.intelligence.collection import check_source, linkedin_configured
from app.intelligence.credit_control import budget_status
from app.intelligence.email_service import INTELLIGENCE_RECIPIENT, send_news_email
from app.intelligence.focus import opportunity_focus_score
from app.intelligence.insights import InsightGenerationError, cached_brief, deep_insight, strategic_brief
from app.intelligence.linkedin_adapter import LinkedInCollectionError
from app.intelligence.linkedin_identity import identity_or_none, lock_source_identity, lock_identity_change, matching_sources
from app.intelligence.models import LinkedInSourceIdentity
from app.intelligence.models import BrightDataExecution, NewsAnalysis, NewsItem, NewsSource, NewsSourceEmailSubscription, NewsUserState
from app.intelligence.priority import analyzed_news_priority
from app.intelligence.rss_adapter import rss_url_is_supported
from app.intelligence.schemas import DeepInsightOut, EmailShareOut, IntelligenceStatusOut, LinkedInMonitorOut, NewsFeedOut, NewsSourceCreate, NewsSourceEmailInput, NewsSourceOut, NewsSourceUpdate, SourceCheckOut, StrategicBriefOut
from app.intelligence.website_adapter import source_kind

router = APIRouter()
logger = logging.getLogger(__name__)
_check_all_lock = asyncio.Lock()
_check_all_task: asyncio.Task[None] | None = None
_check_all_status: dict[str, object] = {"state": "idle", "total": 0, "processed": 0, "failed": 0, "started": 0, "pending": 0}


async def _check_source_background(source_id: uuid.UUID) -> None:
    from app.db import SessionLocal

    try:
        async with SessionLocal() as session:
            await check_source(session, source_id, force=True)
    except Exception:
        logger.exception("Manual source check failed for %s", source_id)


async def _check_all_sources(source_ids: list[uuid.UUID]) -> None:
    try:
        for source_id in source_ids:
            try:
                async with SessionLocal() as session:
                    result = await check_source(session, source_id, force=True)
                if result == "error":
                    _check_all_status["failed"] = int(_check_all_status["failed"]) + 1
                elif result == "started":
                    _check_all_status["started"] = int(_check_all_status["started"]) + 1
                elif result == "pending":
                    _check_all_status["pending"] = int(_check_all_status["pending"]) + 1
            except Exception:
                _check_all_status["failed"] = int(_check_all_status["failed"]) + 1
                logger.exception("Manual source check failed for %s", source_id)
            finally:
                _check_all_status["processed"] = int(_check_all_status["processed"]) + 1
    finally:
        _check_all_status["state"] = "finished"


def _source_out(source: NewsSource, email_enabled: bool = False) -> NewsSourceOut:
    output = NewsSourceOut.model_validate(source)
    output.collection_supported = source.type == "LINKEDIN" or (source.type in {"WEBSITE", "RSS"} and rss_url_is_supported(source.url))
    output.email_enabled = email_enabled
    output.collection_enabled = source.type == "LINKEDIN" and source.status == "ACTIVE" and settings.NEWS_ENABLE_COLLECTION and settings.NEWS_LINKEDIN_FETCH_ENABLED and not settings.NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED and not source.linkedin_paused_reason
    if source.type == 'FACEBOOK':
        from app.intelligence.facebook_identity import identity_or_none as facebook_identity, setup_note, source_approved
        output.collection_supported = bool(facebook_identity(source.url))
        output.collection_enabled = bool(source.status == 'ACTIVE' and settings.NEWS_ENABLE_COLLECTION
            and settings.NEWS_FACEBOOK_FETCH_ENABLED and source_approved(source) and not source.linkedin_paused_reason)
        output.collection_note = setup_note(source)
    return output


@router.get("/status", response_model=IntelligenceStatusOut)
async def intelligence_status(_: User = Depends(require_admin)) -> IntelligenceStatusOut:
    return IntelligenceStatusOut(linkedinConfigured=linkedin_configured())


@router.get("/collection-budget")
async def collection_budget(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    return await budget_status(db)


@router.get("/collection-executions")
async def collection_executions(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500), source_id: uuid.UUID | None = None) -> list[dict]:
    query = select(BrightDataExecution).order_by(BrightDataExecution.started_at.desc()).limit(limit)
    if source_id:
        query = query.where(BrightDataExecution.source_id == source_id)
    rows = (await db.scalars(query)).all()
    return [{column.name: getattr(row, column.name) for column in BrightDataExecution.__table__.columns} for row in rows]


@router.get("/items", response_model=NewsFeedOut)
async def list_items(
    read_state: Literal["all", "unread", "read"] = "all",
    due_soon: bool = False,
    saved_only: bool = False,
    repost_group: Literal["ALL", "CEO", "COMPANY"] | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    source_id: uuid.UUID | None = None,
    activity_kind: Literal["POST", "REPOST"] | None = None,
    source_ids: Annotated[list[uuid.UUID] | None, Query(max_length=20)] = None,
) -> NewsFeedOut:
    source_rows = (await db.execute(select(NewsSource.type, NewsSource.url).where(
        NewsSource.type.in_(["LINKEDIN", "WEBSITE", "RSS"]),
    ))).all()
    has_live_sources = any(kind == "LINKEDIN" or (kind in {"WEBSITE", "RSS"} and rss_url_is_supported(url)) for kind, url in source_rows)
    query = (
        select(NewsItem, NewsSource, NewsAnalysis, NewsUserState.read_at, NewsUserState.emailed_at, NewsUserState.saved_at)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .outerjoin(NewsUserState, and_(
            NewsUserState.news_item_id == NewsItem.id,
            NewsUserState.user_id == user.id,
        ))
        .order_by(NewsItem.published_at.desc().nullslast(), NewsItem.created_at.desc())
        .limit(200)
    )
    if read_state == "unread":
        query = query.where(NewsUserState.read_at.is_(None))
    elif read_state == "read":
        query = query.where(NewsUserState.read_at.is_not(None))
    if saved_only:
        query = query.where(NewsUserState.saved_at.is_not(None))
    if repost_group == "ALL":
        query = query.where(NewsSource.repost_groups != [])
    elif repost_group:
        query = query.where(NewsSource.repost_groups.contains([repost_group]))
    if source_id is not None:
        query = query.where(NewsSource.id == source_id)
    if source_ids is not None:
        query = query.where(NewsSource.id.in_(source_ids))
    if activity_kind:
        kind = func.coalesce(NewsItem.linkedin_data["kind"].as_string(), "POST")
        query = query.where(NewsSource.type == "LINKEDIN", kind == "REPOST" if activity_kind == "REPOST" else kind.in_(["POST", "ARTICLE"]))
    if due_soon:
        today = datetime.now(ZoneInfo("Europe/Budapest")).date()
        query = query.where(NewsAnalysis.category.in_(["GRANT", "TENDER", "BUSINESS", "PARTNERSHIP"]),
                            NewsAnalysis.deadline.between(today, today + timedelta(days=30)))
        query = query.order_by(None).order_by(NewsAnalysis.deadline.asc(), NewsItem.published_at.desc())
    rows = (await db.execute(query)).all()
    return NewsFeedOut(hasLiveSources=has_live_sources, emailConfigured=bool(settings.EMAIL_USER and settings.EMAIL_PASSWORD), emailRecipient=settings.NEWS_EMAIL_RECIPIENT, aiConfigured=bool(settings.OPENAI_API_KEY), items=[{
        "id": str(item.id), "sourceId": str(source.id), "sourceName": source.name,
        "sourceType": source.type, "externalId": item.external_id,
        "url": item.url, "title": item.title, "originalText": item.original_text,
        "publishedAt": (item.published_at or item.created_at).isoformat(),
        "imageUrl": item.image_url, "contentHash": item.content_hash,
        "createdAt": item.created_at.isoformat(), "location": None,
        "linkedin": item.linkedin_data,
        "repostGroups": source.repost_groups,
        "repostGuidance": source.repost_guidance,
        "readAt": read_at.isoformat() if read_at else None,
        "emailedAt": emailed_at.isoformat() if emailed_at else None,
        "focusScore": opportunity_focus_score(item, source, analysis),
        "priority": analyzed_news_priority(item, source, analysis),
        "analysis": {
            "summary": analysis.summary, "category": analysis.category,
            "importanceScore": analysis.importance_score, "relevanceScore": analysis.relevance_score,
            "whyItMatters": analysis.why_it_matters,
            "deadline": analysis.deadline.isoformat() if analysis.deadline else None,
            "fundingAmount": analysis.funding_amount, "eligibility": analysis.eligibility,
            "opportunityType": analysis.opportunity_type, "tags": analysis.tags,
        },
        "savedAt": saved_at.isoformat() if saved_at else None,
    } for item, source, analysis, read_at, emailed_at, saved_at in rows])


async def _brief_rows(db: AsyncSession) -> list[tuple[NewsItem, NewsSource, NewsAnalysis]]:
    rows = (await db.execute(
        select(NewsItem, NewsSource, NewsAnalysis)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .order_by(NewsItem.published_at.desc().nullslast(), NewsItem.created_at.desc())
        .limit(80)
    )).all()
    return [tuple(row) for row in rows]


@router.get("/brief", response_model=StrategicBriefOut | None)
async def get_strategic_brief(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> StrategicBriefOut | None:
    rows = await _brief_rows(db)
    return await cached_brief(db, rows) if rows else None


@router.post("/brief", response_model=StrategicBriefOut)
async def generate_strategic_brief(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> StrategicBriefOut:
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI analysis is not configured.")
    rows = await _brief_rows(db)
    if not rows:
        raise HTTPException(status_code=404, detail="No updates are available for a briefing yet.")
    try:
        return await strategic_brief(db, rows)
    except InsightGenerationError as exc:
        logger.exception("Could not generate strategic brief")
        raise HTTPException(status_code=502, detail="Could not generate the briefing right now.") from exc


@router.post("/items/{item_id}/deep-analysis", response_model=DeepInsightOut)
async def analyze_item_deeply(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> DeepInsightOut:
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI analysis is not configured.")
    result = (await db.execute(
        select(NewsItem, NewsSource, NewsAnalysis)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .where(NewsItem.id == item_id)
    )).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="Update not found")
    try:
        return await deep_insight(db, *result)
    except InsightGenerationError as exc:
        logger.exception("Could not analyze update %s", item_id)
        raise HTTPException(status_code=502, detail="Could not analyze this update right now.") from exc


@router.post("/items/{item_id}/email", response_model=EmailShareOut)
async def email_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailShareOut:
    result = (await db.execute(
        select(NewsItem, NewsSource, NewsAnalysis)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .where(NewsItem.id == item_id)
    )).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="Update not found")
    item, source, analysis = result
    await db.execute(pg_insert(NewsUserState).values(
        user_id=user.id, news_item_id=item_id,
    ).on_conflict_do_nothing(index_elements=[NewsUserState.user_id, NewsUserState.news_item_id]))
    user_state = (await db.execute(select(NewsUserState).where(
        NewsUserState.user_id == user.id,
        NewsUserState.news_item_id == item_id,
    ).with_for_update())).scalar_one()
    if user_state.emailed_at:
        sent_at = user_state.emailed_at
        await db.rollback()
        return EmailShareOut(recipient=INTELLIGENCE_RECIPIENT, sentAt=sent_at, alreadySent=True)
    try:
        await send_news_email(item, source, analysis)
    except ValueError as exc:
        await db.rollback()
        logger.warning("Intelligence email configuration unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Email sending is not configured right now.") from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("Could not email Intelligence update %s", item_id)
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            detail = "Email account authentication failed. Ask an admin to check the SMTP credentials."
        elif isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            detail = "The SMTP server could not be reached. Please try again later."
        else:
            detail = f"Email delivery failed ({type(exc).__name__}). Please try again."
        raise HTTPException(status_code=502, detail=detail) from exc
    user_state.emailed_at = datetime.now(timezone.utc)
    await db.commit()
    return EmailShareOut(recipient=INTELLIGENCE_RECIPIENT, sentAt=user_state.emailed_at, alreadySent=False)


@router.put("/items/{item_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_item_read(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    if await db.get(NewsItem, item_id) is None:
        raise HTTPException(status_code=404, detail="Update not found")
    statement = pg_insert(NewsUserState).values(
        user_id=user.id, news_item_id=item_id, read_at=func.now(),
    ).on_conflict_do_update(
        index_elements=[NewsUserState.user_id, NewsUserState.news_item_id],
        set_={"read_at": func.now()},
    )
    await db.execute(statement)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/items/{item_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_item_unread(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    await db.execute(update(NewsUserState).where(
        NewsUserState.user_id == user.id,
        NewsUserState.news_item_id == item_id,
    ).values(read_at=None))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/items/{item_id}/saved", status_code=status.HTTP_204_NO_CONTENT)
async def save_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    if await db.get(NewsItem, item_id) is None:
        raise HTTPException(status_code=404, detail="Update not found")
    await db.execute(pg_insert(NewsUserState).values(
        user_id=user.id, news_item_id=item_id, saved_at=func.now(),
    ).on_conflict_do_update(
        index_elements=[NewsUserState.user_id, NewsUserState.news_item_id],
        set_={"saved_at": func.now()},
    ))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/items/{item_id}/saved", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    await db.execute(update(NewsUserState).where(
        NewsUserState.user_id == user.id,
        NewsUserState.news_item_id == item_id,
    ).values(saved_at=None))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/repost-watch/status")
async def repost_watch_status(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    unread = (
        select(NewsItem.id, NewsSource.repost_groups)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .outerjoin(NewsUserState, and_(NewsUserState.news_item_id == NewsItem.id, NewsUserState.user_id == user.id))
        .where(NewsSource.repost_groups != [], NewsUserState.read_at.is_(None))
    )
    rows = (await db.execute(unread)).all()
    return {"unread": len(rows), "CEO": sum("CEO" in groups for _, groups in rows),
            "COMPANY": sum("COMPANY" in groups for _, groups in rows)}


@router.get("/sources", response_model=list[NewsSourceOut])
async def list_sources(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> list[NewsSourceOut]:
    sources = (await db.execute(select(NewsSource).order_by(NewsSource.created_at.desc()))).scalars().all()
    subscribed = set((await db.scalars(select(NewsSourceEmailSubscription.source_id).where(
        NewsSourceEmailSubscription.user_id == user.id))).all())
    return [_source_out(source, source.id in subscribed) for source in sources]


@router.get("/linkedin-sources", response_model=list[LinkedInMonitorOut])
async def linkedin_monitoring_sources(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[LinkedInMonitorOut]:
    # Feed readers can discover monitored accounts without receiving admin settings.
    sources = await db.scalars(select(NewsSource).where(NewsSource.type == "LINKEDIN").order_by(NewsSource.created_at, NewsSource.id))
    return [LinkedInMonitorOut(**_source_out(source).model_dump()) for source in sources]


@router.get("/sources/check-all/status")
async def check_all_sources_status(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict[str, object]:
    from app.intelligence.cycle_service import cycle_status, trial_configuration, trial_active
    config = await trial_configuration(db)
    if config:
        current = await cycle_status(db)
        if current['cycle_id'] and (current['state'] == 'running' or trial_active(config, datetime.now(timezone.utc))):
            return current
    return _check_all_status.copy()


@router.post("/sources/check-all")
async def check_all_sources_now(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin), repost_group: Literal["CEO", "COMPANY"] | None = None, source_id: uuid.UUID | None = None, source_ids: Annotated[list[uuid.UUID] | None, Query(max_length=20)] = None) -> dict[str, object]:
    global _check_all_task, _check_all_status
    from app.intelligence.cycle_service import trial_configuration, trial_active, accept_manual_cycle, run_collection_trial_cycle
    now = datetime.now(timezone.utc)
    if trial_active(await trial_configuration(db), now):
        try:
            accepted = await accept_manual_cycle(db, user_id=_.id, now=now)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        asyncio.create_task(run_collection_trial_cycle())
        return accepted
    async with _check_all_lock:
        if _check_all_task and not _check_all_task.done():
            return _check_all_status.copy()
        query = select(NewsSource).where(NewsSource.status == "ACTIVE", NewsSource.type != "LINKEDIN")
        if repost_group:
            query = query.where(NewsSource.repost_groups.contains([repost_group]))
        if source_id is not None:
            query = query.where(NewsSource.id == source_id)
        if source_ids is not None:
            query = query.where(NewsSource.id.in_(source_ids))
        sources = (await db.execute(query)).scalars().all()
        source_ids = [source.id for source in sources if
                      source.type in {"WEBSITE", "RSS"} and rss_url_is_supported(source.url)]
        if not source_ids:
            raise HTTPException(status_code=409, detail="LinkedIn checks run on the protected daily scheduler. Refresh the feed to see stored posts.")
        _check_all_status = {"state": "running", "total": len(source_ids), "processed": 0, "failed": 0, "started": 0, "pending": 0}
        _check_all_task = asyncio.create_task(_check_all_sources(source_ids))
        return _check_all_status.copy()


@router.post("/sources", response_model=NewsSourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(payload: NewsSourceCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> NewsSourceOut:
    if payload.type == 'FACEBOOK':
        from app.intelligence.facebook_identity import identity_or_none as facebook_identity, lock_identity_change as fb_lock, matching_sources as fb_matches
        identity=facebook_identity(payload.url)
        if identity:
            await fb_lock(db,'',payload.url)
            if await fb_matches(db,identity):raise HTTPException(status_code=409,detail='This Facebook source already exists.')
    if payload.type in {"RSS", "WEBSITE"} and not rss_url_is_supported(payload.url):
        raise HTTPException(status_code=422, detail="Use a public HTTP or HTTPS URL without credentials or a custom port.")
    if payload.type == "LINKEDIN":
        identity = await lock_source_identity(db, payload.url)
        if await matching_sources(db, identity):
            raise HTTPException(status_code=409, detail="This LinkedIn source already exists. Edit or resume the existing source.")
    source = NewsSource(**payload.model_dump(exclude={"email_enabled"}))
    db.add(source)
    await db.flush()
    if payload.type == "LINKEDIN":
        db.add(LinkedInSourceIdentity(canonical_url=identity, source_id=source.id))
    if payload.email_enabled:
        from app.intelligence.digest_service import ensure_digest_settings
        await ensure_digest_settings(db, user.id)
        db.add(NewsSourceEmailSubscription(source_id=source.id, user_id=user.id, enabled_at=datetime.now(timezone.utc)))
    await db.commit()
    await db.refresh(source)
    return _source_out(source, payload.email_enabled)


async def _get_source(db: AsyncSession, source_id: uuid.UUID) -> NewsSource:
    source = await db.get(NewsSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.post("/sources/{source_id}/check", response_model=SourceCheckOut)
async def check_source_now(source_id: uuid.UUID, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> SourceCheckOut:
    source = await _get_source(db, source_id)
    if source.type in {"WEBSITE", "RSS"}:
        if not rss_url_is_supported(source.url):
            raise HTTPException(status_code=422, detail="Use a public HTTP or HTTPS URL.")
        if source.status != "ACTIVE":
            raise HTTPException(status_code=422, detail="Activate this source before checking it.")
        asyncio.create_task(_check_source_background(source_id))
        return SourceCheckOut(state="started")
    if source.type != "LINKEDIN":
        raise HTTPException(status_code=422, detail="No connector is available for this source yet.")
    raise HTTPException(status_code=409, detail="LinkedIn checks run on the protected daily scheduler. Refresh the feed to see stored posts.")


@router.put("/sources/{source_id}", response_model=NewsSourceOut)
async def update_source(source_id: uuid.UUID, payload: NewsSourceUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> NewsSourceOut:
    if payload.type in {"RSS", "WEBSITE"} and not rss_url_is_supported(payload.url):
        raise HTTPException(status_code=422, detail="Use a public HTTP or HTTPS URL without credentials or a custom port.")
    initial = await _get_source(db, source_id)
    initial_identity = (initial.type, initial.url)
    from app.intelligence.facebook_identity import lock_identity_change as fb_lock, identity_or_none as facebook_identity, matching_sources as fb_matches
    await fb_lock(db,initial.url if initial.type=='FACEBOOK' else '',payload.url if payload.type=='FACEBOOK' else '')
    await lock_identity_change(db, initial.url if initial.type == "LINKEDIN" else "", payload.url if payload.type == "LINKEDIN" else "")
    source = await db.scalar(select(NewsSource).where(NewsSource.id == source_id).with_for_update().execution_options(populate_existing=True))
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if (source.type, source.url) != initial_identity:
        raise HTTPException(status_code=409, detail="Source changed during editing. Refresh and try again.")
    url_changed = source.type != payload.type or (identity_or_none(source.url) != identity_or_none(payload.url) if payload.type == "LINKEDIN" else source.url != payload.url)
    if url_changed and (source.pending_execution_id or source.pending_snapshot_id):
        raise HTTPException(status_code=409, detail="Reconcile the pending collection before changing this source URL or type.")
    if url_changed and {source.type, payload.type}.intersection({'LINKEDIN','FACEBOOK'}):
        history = source.last_started_at or source.last_successful_fetch_at or await db.scalar(select(NewsItem.id).where(NewsItem.source_id == source_id).limit(1)) or await db.scalar(select(BrightDataExecution.id).where(BrightDataExecution.source_id == source_id).limit(1))
        if history:
            raise HTTPException(status_code=409, detail="A collected LinkedIn identity cannot be changed. Pause it and add a separate source to preserve history and daily limits.")
    if payload.type=='FACEBOOK' and (identity:=facebook_identity(payload.url)):
        if any(s.id!=source_id for s in await fb_matches(db,identity)):
            raise HTTPException(status_code=409,detail='This Facebook source already exists.')
    if payload.type == "LINKEDIN":
        identity = await lock_source_identity(db, payload.url)
        if any(row.id != source_id for row in await matching_sources(db, identity)):
            raise HTTPException(status_code=409, detail="This LinkedIn source already exists. Edit or resume the existing source.")
    source_changed = url_changed or source.status != payload.status
    if url_changed:
        owner = await db.scalar(select(LinkedInSourceIdentity).where(LinkedInSourceIdentity.source_id == source_id))
        if owner is not None:
            await db.delete(owner)
            await db.flush()
        source.pending_snapshot_id = None
        source.last_started_at = None
        source.last_checked_at = None
        source.last_error = None
    if payload.type == "LINKEDIN" and url_changed:
        db.add(LinkedInSourceIdentity(canonical_url=identity, source_id=source_id))
    for field, value in payload.model_dump(exclude={"email_enabled"}).items():
        setattr(source, field, value)
    subscription = await db.get(NewsSourceEmailSubscription, source_id)
    if subscription is not None and subscription.user_id != user.id:
        raise HTTPException(status_code=409, detail="Email updates for this source are managed by another administrator.")
    if payload.email_enabled and subscription is None:
        from app.intelligence.digest_service import ensure_digest_settings
        await ensure_digest_settings(db, user.id)
        db.add(NewsSourceEmailSubscription(source_id=source_id, user_id=user.id, enabled_at=datetime.now(timezone.utc)))
    elif not payload.email_enabled and subscription is not None and subscription.user_id == user.id:
        await db.delete(subscription)
    elif payload.email_enabled and subscription is not None and source_changed:
        subscription.enabled_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(source)
    return _source_out(source, payload.email_enabled)


@router.put("/sources/{source_id}/email", response_model=NewsSourceOut)
async def set_source_email(source_id: uuid.UUID, payload: NewsSourceEmailInput, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> NewsSourceOut:
    source = await _get_source(db, source_id)
    subscription = await db.get(NewsSourceEmailSubscription, source_id)
    if subscription is not None and subscription.user_id != user.id:
        raise HTTPException(status_code=409, detail="Email updates for this source are managed by another administrator.")
    if payload.enabled and subscription is None:
        from app.intelligence.digest_service import ensure_digest_settings
        await ensure_digest_settings(db, user.id)
        db.add(NewsSourceEmailSubscription(source_id=source_id, user_id=user.id, enabled_at=datetime.now(timezone.utc)))
    elif not payload.enabled and subscription is not None and subscription.user_id == user.id:
        await db.delete(subscription)
    await db.commit()
    return _source_out(source, payload.enabled)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> Response:
    source = await _get_source(db, source_id)
    identity = (source.type, source.url)
    from app.intelligence.facebook_identity import lock_identity_change as fb_lock
    await fb_lock(db,source.url if source.type=='FACEBOOK' else '','')
    await lock_identity_change(db, source.url if source.type == "LINKEDIN" else "", "")
    source = await db.scalar(select(NewsSource).where(NewsSource.id == source_id).with_for_update().execution_options(populate_existing=True))
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if (source.type, source.url) != identity:
        raise HTTPException(status_code=409, detail="Source changed. Refresh before deleting.")
    if source.type in {'LINKEDIN','FACEBOOK'}:
        if source.pending_execution_id or source.pending_snapshot_id or source.last_started_at or source.last_successful_fetch_at or await db.scalar(select(NewsItem.id).where(NewsItem.source_id == source_id).limit(1)) or await db.scalar(select(BrightDataExecution.id).where(BrightDataExecution.source_id == source_id).limit(1)):
            raise HTTPException(status_code=409, detail="This LinkedIn source has history or a pending collection. Pause it to preserve posts and saved state.")
    await db.delete(source)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/sources/{source_id}/resolve-facebook',response_model=NewsSourceOut)
async def resolve_facebook_source(source_id: uuid.UUID,db: AsyncSession=Depends(get_db),user: User=Depends(require_admin)):
    from app.intelligence.facebook_resolution import resolve_facebook_url
    source=await _get_source(db,source_id)
    if source.type!='FACEBOOK':raise HTTPException(status_code=422,detail='Select a Facebook source.')
    initial=source.url
    try:resolved=await resolve_facebook_url(initial)
    except (ValueError,httpx.HTTPError) as exc:
        raise HTTPException(status_code=422,detail='Facebook link could not be resolved publicly. Enter the direct page or public group URL.') from exc
    await db.refresh(source)
    if source.url!=initial:raise HTTPException(status_code=409,detail='Source changed; refresh before resolving.')
    subscription=await db.get(NewsSourceEmailSubscription,source.id)
    fields={key:getattr(source,key) for key in NewsSourceCreate.model_fields if key!='email_enabled'}
    return await update_source(source_id,NewsSourceUpdate(**{**fields,**resolved,'email_enabled':bool(subscription)}),db,user)
