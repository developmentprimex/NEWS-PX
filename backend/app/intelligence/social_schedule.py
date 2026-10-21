"""Recurring 07:00/21:00 collection with durable slots and combined email reports."""
import hashlib
import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.intelligence.collection_cycles import (
    CollectionCycle, CollectionCycleSource, ScheduledDigestReport, confirmed_activity_time,
)
from app.intelligence.models import (
    NewsAnalysis, NewsDigestDelivery, NewsDigestSettings, NewsItem,
    NewsSource, NewsSourceEmailSubscription,
)
from app.intelligence.digest_service import DIGEST_ZONE, activity_delivery_key, activity_kind, digest_content, _send_digest

logger = logging.getLogger(__name__)
SCHEDULE_TIMES = ['07:00', '21:00']
SCHEDULE_PHASES = {'scheduled-am': 7, 'scheduled-pm': 21}
COLLECTION_PHASES = [*SCHEDULE_PHASES, 'scheduled-day']
START_GRACE = timedelta(minutes=30)


def social_fetch_slot(cycle):
    return cycle.phase == 'scheduled-day' if settings.NEWS_SOCIAL_DAILY_FETCH else cycle.phase in SCHEDULE_PHASES


def previous_fetch_slot(cutoff):
    if not settings.NEWS_SOCIAL_DAILY_FETCH:
        return previous_slot(cutoff)
    return cutoff - timedelta(days=1)


def due_daily_slot(now):
    if not settings.NEWS_SOCIAL_DAILY_FETCH:
        return None
    cutoff = datetime.combine(now.astimezone(timezone.utc).date(), time(), timezone.utc)
    # Allow the provider five minutes to close its UTC day; DST cannot create gaps.
    return ('scheduled-day', cutoff) if cutoff + timedelta(minutes=5) <= now < cutoff + START_GRACE else None


def utc_now():
    return datetime.now(timezone.utc)


def due_slot(now):
    """Catch up a delayed scan within its slot, never replay missed days."""
    local = now.astimezone(DIGEST_ZONE)
    for phase, hour in reversed(list(SCHEDULE_PHASES.items())):
        cutoff = datetime.combine(local.date(), time(hour), DIGEST_ZONE).astimezone(timezone.utc)
        if cutoff <= now < cutoff + START_GRACE:
            return phase, cutoff
    return None


def previous_slot(cutoff):
    local = cutoff.astimezone(DIGEST_ZONE)
    day = local.date() - timedelta(days=int(local.hour == 7))
    return datetime.combine(day, time(21 if local.hour == 7 else 7), DIGEST_ZONE).astimezone(timezone.utc)


def cycle_authorized(cycle, now):
    slot = due_daily_slot(now) if cycle.phase == 'scheduled-day' else due_slot(now)
    return bool(settings.NEWS_SOCIAL_SCHEDULE_ENABLED and settings.NEWS_ENABLE_COLLECTION
                and slot and cycle.phase == slot[0] and cycle.cutoff_at == slot[1]
                and cycle.trial_date == now.astimezone(DIGEST_ZONE).date())


async def ensure_scheduled_cycle(db, now):
    from app.intelligence.cycle_service import freeze_inventory
    slot = due_slot(now) or due_daily_slot(now)
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED or not slot:
        return None
    phase, cutoff = slot
    day = cutoff.astimezone(DIGEST_ZONE).date()
    await db.execute(insert(CollectionCycle).values(
        trial_date=day, phase=phase, cutoff_at=cutoff, deadline_at=cutoff + START_GRACE,
        state='queued', inventory_frozen=False,
    ).on_conflict_do_nothing(index_elements=['trial_date', 'phase']))
    await db.commit()
    cycle = await db.scalar(select(CollectionCycle).where(
        CollectionCycle.trial_date == day, CollectionCycle.phase == phase))
    await freeze_inventory(db, cycle)
    return cycle


async def run_social_collection(now=None, *, clock=None):
    from app.intelligence import collection
    from app.intelligence.cycle_service import TERMINAL, dispatch_cycle_source, cycle_status, cycle_authorized as authorized
    clock = clock or ((lambda: now) if now is not None else utc_now)
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED or not settings.NEWS_ENABLE_COLLECTION:
        return
    # Shared with the legacy coordinator, including rolling restarts.
    async with collection.engine.connect() as lock:
        key = 7302192100
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return
        try:
            async with collection.SessionLocal() as db:
                await ensure_scheduled_cycle(db, clock())
                cycles = list(await db.scalars(select(CollectionCycle).where(
                    CollectionCycle.phase.in_([*COLLECTION_PHASES, 'recovery-day']), CollectionCycle.state != 'finished'
                ).order_by(CollectionCycle.cutoff_at)))
            for cycle in cycles:
                async with collection.SessionLocal() as db:
                    allow_new = True
                    has_new = await db.scalar(select(CollectionCycleSource.id).where(
                        CollectionCycleSource.cycle_id == cycle.id,
                        CollectionCycleSource.type.in_(['LINKEDIN', 'FACEBOOK']),
                        CollectionCycleSource.execution_id.is_(None),
                        CollectionCycleSource.state.not_in(TERMINAL)).limit(1))
                    if has_new and not await authorized(db, cycle, clock()):
                        allow_new = False
                    elif has_new and settings.NEWS_TRIAL_BILLING_BINDING_FILE:
                        from app.intelligence.trial_usage import refresh_shared_billing
                        try:
                            await refresh_shared_billing(db, clock())
                            await db.commit()
                            from app.intelligence.collection_exception import applies, observe
                            if applies(cycle, clock()):
                                await observe(db, cycle, clock(), before=True)
                        except Exception:
                            await db.rollback()
                            allow_new = False
                    ids = list(await db.scalars(select(CollectionCycleSource.id)
                        .join(NewsSource, NewsSource.id == CollectionCycleSource.source_id).where(
                        CollectionCycleSource.cycle_id == cycle.id,
                        CollectionCycleSource.state.not_in(TERMINAL))
                        .order_by(NewsSource.last_started_at.asc().nullsfirst(), NewsSource.id)))
                for row_id in ids:
                    async with collection.SessionLocal() as db:
                        try:
                            await dispatch_cycle_source(db, row_id, clock(), clock=clock,
                                                        allow_new_requests=allow_new)
                        except Exception:
                            await db.rollback()
                            row = await db.get(CollectionCycleSource, row_id)
                            row.state = 'uncertain'
                            row.coverage_warning = 'Collection interrupted; reconcile the existing execution.'
                            await db.commit()
                async with collection.SessionLocal() as db:
                    status = await cycle_status(db, cycle.id)
                    if not status['pending']:
                        stored = await db.get(CollectionCycle, cycle.id)
                        stored.state, stored.finished_at = 'finished', clock()
                        await db.commit()
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})


async def scheduled_entries(db, user_id, start, cutoff, now, *, strict_interval=False):
    """Include late inserts from the completed social job, plus periodic website/RSS updates."""
    activity = func.coalesce(NewsItem.published_at, NewsItem.created_at)
    rows = (await db.execute(select(NewsItem, NewsAnalysis, NewsSource)
        .outerjoin(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsSourceEmailSubscription, NewsSourceEmailSubscription.source_id == NewsSource.id)
        .where(NewsSourceEmailSubscription.user_id == user_id, NewsSource.status == 'ACTIVE',
            NewsItem.created_at >= NewsSourceEmailSubscription.enabled_at,
            NewsItem.created_at <= now, activity <= cutoff,
            or_(activity > start, and_(NewsItem.created_at > start,
                activity >= NewsSourceEmailSubscription.enabled_at)))
        .order_by(NewsSource.name, activity.desc(), NewsItem.id))).all()
    entries = {}
    for item, analysis, source in rows:
        if strict_interval:
            stamp = confirmed_activity_time(item)
            if stamp is None or not start < stamp <= cutoff:
                continue
        if source.type in {'LINKEDIN', 'FACEBOOK'}:
            stamp = confirmed_activity_time(item)
            if stamp is None or stamp > cutoff:
                continue
        entries[activity_delivery_key(item)] = {'item': item, 'analysis': analysis, 'source': source}
    legacy = {key: hashlib.sha256(entry['item'].url.encode()).hexdigest() for key, entry in entries.items()}
    receipts = {r.url_hash: r.sent_at for r in await db.scalars(select(NewsDigestDelivery).where(
        NewsDigestDelivery.user_id == user_id,
        NewsDigestDelivery.url_hash.in_(set(entries) | set(legacy.values()))))} if entries else {}
    return [entry for key, entry in entries.items() if key not in receipts
        and not (legacy[key] in receipts and activity_kind(entry['item'], entry['source']) != 'REPOST'
                 and receipts[legacy[key]] >= entry['item'].created_at)]


async def process_scheduled_digest(db, cycle, user_id, now):
    from app.intelligence.cycle_service import TERMINAL, source_identity
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED or cycle.phase not in SCHEDULE_PHASES or now < cycle.cutoff_at:
        return 0
    # Serialize both slots and concurrent workers for this user's delivery history.
    digest = await db.scalar(select(NewsDigestSettings).where(NewsDigestSettings.user_id == user_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if not digest:
        return 0
    fingerprint = hashlib.sha256(settings.NEWS_EMAIL_RECIPIENT.strip().casefold().encode()).hexdigest()
    if await db.scalar(select(ScheduledDigestReport.id).where(
        ScheduledDigestReport.user_id == user_id, ScheduledDigestReport.recipient_fingerprint == fingerprint,
        ScheduledDigestReport.state.in_(['sending', 'uncertain'])).limit(1)):
        return 0
    if await db.scalar(select(ScheduledDigestReport.id).where(
        ScheduledDigestReport.cycle_id == cycle.id, ScheduledDigestReport.user_id == user_id,
        ScheduledDigestReport.recipient_fingerprint == fingerprint)):
        return 0
    states = list(await db.scalars(select(CollectionCycleSource.state).where(
        CollectionCycleSource.cycle_id == cycle.id)))
    if any(state not in TERMINAL for state in states) and now < cycle.deadline_at:
        return 0
    if digest.last_slot_at >= cycle.cutoff_at:
        return 0
    from app.intelligence.collection_exception import applies
    strict_interval = applies(cycle, cycle.cutoff_at)
    start = previous_slot(cycle.cutoff_at) if strict_interval else digest.last_slot_at
    entries = await scheduled_entries(db, user_id, start, cycle.cutoff_at, now,
        strict_interval=strict_interval)
    if settings.NEWS_SOCIAL_DAILY_FETCH:
        # Late, verified updates remain deliverable; insertion time never certifies publication.
        entries = [e for e in entries if confirmed_activity_time(e['item']) is not None]
    # Honor source edits, pauses and unsubscribe that occurred during selection.
    source_ids = sorted({e['source'].id for e in entries})
    identities = {e['source'].id: source_identity(e['source']) for e in entries}
    sources = {s.id: s for s in await db.scalars(select(NewsSource).where(NewsSource.id.in_(source_ids))
        .order_by(NewsSource.id).with_for_update().execution_options(populate_existing=True))}
    subscriptions = {s.source_id: s for s in await db.scalars(select(NewsSourceEmailSubscription).where(
        NewsSourceEmailSubscription.user_id == user_id, NewsSourceEmailSubscription.source_id.in_(source_ids))
        .order_by(NewsSourceEmailSubscription.source_id).with_for_update().execution_options(populate_existing=True))}
    entries = [e for e in entries if e['source'].id in subscriptions
        and sources[e['source'].id].status == 'ACTIVE'
        and source_identity(sources[e['source'].id]) == identities[e['source'].id]
        and e['item'].created_at >= subscriptions[e['source'].id].enabled_at]
    report = ScheduledDigestReport(cycle_id=cycle.id, user_id=user_id,
        recipient_fingerprint=fingerprint, state='empty', content={'item_ids': []})
    db.add(report)
    if not entries:
        digest.last_slot_at, digest.last_error = cycle.cutoff_at, None
        await db.commit()
        return 0
    # Ordinary SMTP is the recurring transport. Trial-only credentials retain their date/user scope.
    if not settings.EMAIL_USER or not settings.EMAIL_PASSWORD:
        await db.rollback()
        return 0
    if (not settings.NEWS_SOCIAL_SCHEDULE_ENABLED
            or hashlib.sha256(settings.NEWS_EMAIL_RECIPIENT.strip().casefold().encode()).hexdigest() != fingerprint):
        await db.rollback()
        return 0
    subject, body, html = digest_content(entries, cycle.cutoff_at, interval_start=start)
    report.content = {'item_ids': [str(e['item'].id) for e in entries], 'subject': subject, 'body': body, 'html': html}
    report.state, report.claimed_at = 'sending', now
    await db.commit()  # A crash after claiming cannot cause a second SMTP submission.
    try:
        await _send_digest(subject, body, html)
    except Exception:
        report.state, report.error = 'uncertain', 'SMTP outcome requires verification; automatic retry disabled.'
        digest.last_error = report.error
        await db.commit()
        return 0
    for entry in entries:
        await db.execute(insert(NewsDigestDelivery).values(user_id=user_id,
            url_hash=activity_delivery_key(entry['item']), sent_at=now).on_conflict_do_nothing())
    report.state, report.sent_at = 'sent', now
    digest.last_slot_at, digest.last_sent_at, digest.last_error = cycle.cutoff_at, now, None
    await db.commit()
    return len(entries)


async def run_social_reports(now=None):
    """Separate job lets email proceed at the deadline even while a collector is busy."""
    from app.intelligence import collection
    now = now or utc_now()
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED or not settings.EMAIL_USER or not settings.EMAIL_PASSWORD:
        return
    async with collection.SessionLocal() as db:
        await ensure_scheduled_cycle(db, now)
        users = list(await db.scalars(select(NewsDigestSettings.user_id)))
        if settings.NEWS_COLLECTION_EXCEPTION_DATE:
            cycles = list(await db.scalars(select(CollectionCycle).where(
                CollectionCycle.trial_date == settings.NEWS_COLLECTION_EXCEPTION_DATE,
                CollectionCycle.phase == 'scheduled-pm', CollectionCycle.state == 'finished')))
            for cycle in cycles:
                try:
                    from app.intelligence.collection_exception import observe
                    await observe(db, cycle, now)
                except Exception:
                    await db.rollback()
                    logger.warning('One-evening usage observation deferred; existing evidence retained')
    fingerprint = hashlib.sha256(settings.NEWS_EMAIL_RECIPIENT.strip().casefold().encode()).hexdigest()
    for user_id in users:
        async with collection.SessionLocal() as db:
            if await db.scalar(select(ScheduledDigestReport.id).where(
                ScheduledDigestReport.user_id == user_id, ScheduledDigestReport.recipient_fingerprint == fingerprint,
                ScheduledDigestReport.state.in_(['sending', 'uncertain'])).limit(1)):
                continue
            digest = await db.get(NewsDigestSettings, user_id)
            already_reported = select(ScheduledDigestReport.id).where(
                ScheduledDigestReport.cycle_id == CollectionCycle.id,
                ScheduledDigestReport.user_id == user_id,
                ScheduledDigestReport.recipient_fingerprint == fingerprint).exists()
            cycle_ids = list(await db.scalars(select(CollectionCycle.id).where(
                CollectionCycle.phase.in_(SCHEDULE_PHASES), CollectionCycle.cutoff_at <= now,
                CollectionCycle.cutoff_at > digest.last_slot_at, ~already_reported,
            ).order_by(CollectionCycle.cutoff_at)))
        for cycle_id in cycle_ids:
            try:
                async with collection.SessionLocal() as db:
                    cycle = await db.get(CollectionCycle, cycle_id)
                    await process_scheduled_digest(db, cycle, user_id, now)
            except Exception:
                logger.exception('Scheduled report processing failed')
