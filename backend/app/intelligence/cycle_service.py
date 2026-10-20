"""One-day collection coordination; no work is enabled by importing this module."""
import json
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db import AppMetadata
from app.config import settings
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, CycleStatus
from app.intelligence.models import BrightDataExecution, NewsSource

TRIAL_KEY = 'news_morning_evening_trial_v1'
ZONE = ZoneInfo('Europe/Budapest')
TERMINAL = {'completed', 'partial', 'blocked', 'failed', 'uncertain'}


def trial_date_for_activation(now):
    local = now.astimezone(ZONE)
    return local.date() + timedelta(days=int(local.time() >= time(21)))


def trial_active(config, now):
    return bool(config.get('enabled') and config.get('date') == now.astimezone(ZONE).date().isoformat())


async def trial_configuration(db):
    record = await db.get(AppMetadata, TRIAL_KEY)
    return json.loads(record.value) if record else {}


async def cycle_authorized(db, cycle, now):
    from app.intelligence.social_schedule import SCHEDULE_PHASES, cycle_authorized as scheduled_authorized
    if cycle.phase in SCHEDULE_PHASES:
        return scheduled_authorized(cycle, now)
    return not settings.NEWS_SOCIAL_SCHEDULE_ENABLED and trial_active(await trial_configuration(db), now)


async def latest_verified_checkpoint(db, source_id):
    cycle_point = await db.scalar(select(func.max(CollectionCycleSource.window_end)).where(
        CollectionCycleSource.source_id == source_id,
        CollectionCycleSource.state == 'completed', CollectionCycleSource.coverage_confirmed.is_(True)))
    day = await db.scalar(select(func.max(BrightDataExecution.end_date)).where(
        BrightDataExecution.source_id == source_id, BrightDataExecution.state == 'completed',
        BrightDataExecution.trigger_reason == 'scheduled-day'))
    daily_point = datetime.combine(day + timedelta(days=1), time(), timezone.utc) if day else None
    return max(point for point in (cycle_point, daily_point) if point) if cycle_point or daily_point else None


async def ensure_cycle(db, *, phase, now):
    config = await trial_configuration(db)
    if phase not in {'manual', 'evening'} or not trial_active(config, now):
        raise ValueError('No active collection trial for this date.')
    day = now.astimezone(ZONE).date()
    evening = datetime.combine(day, time(21), ZONE).astimezone(timezone.utc)
    if phase == 'evening' and now < evening:
        raise ValueError('Evening collection is not due yet.')
    cutoff = evening if phase == 'evening' else now.astimezone(timezone.utc)
    await db.execute(insert(CollectionCycle).values(id=uuid4(), trial_date=day, phase=phase,
        cutoff_at=cutoff, deadline_at=cutoff + timedelta(minutes=30), state='queued', inventory_frozen=False)
        .on_conflict_do_nothing(index_elements=['trial_date', 'phase']))
    await db.commit()
    return await db.scalar(select(CollectionCycle).where(CollectionCycle.trial_date == day,
                                                        CollectionCycle.phase == phase))


async def cycle_status(db, cycle_id=None) -> CycleStatus:
    query = select(CollectionCycle)
    if cycle_id:
        query = query.where(CollectionCycle.id == cycle_id)
    cycle = await db.scalar(query.order_by(CollectionCycle.cutoff_at.desc()).limit(1))
    states = list(await db.scalars(select(CollectionCycleSource.state).where(
        CollectionCycleSource.cycle_id == cycle.id))) if cycle else []
    return dict(state=('finished' if cycle.state == 'finished' else 'running') if cycle else 'idle',
        total=len(states), processed=sum(s in TERMINAL for s in states), failed=states.count('failed') + states.count('uncertain'),
        started=sum(s in {'running', 'completed', 'partial'} for s in states), pending=sum(s not in TERMINAL for s in states),
        partial=states.count('partial'), blocked=states.count('blocked'), cycle_id=str(cycle.id) if cycle else None,
        phase=cycle.phase if cycle else None, cutoff_at=cycle.cutoff_at.isoformat() if cycle else None)


def source_identity(source):
    if source.type == 'LINKEDIN':
        from app.intelligence.linkedin_identity import identity_or_none
        return identity_or_none(source.url)
    if source.type == 'FACEBOOK':
        from app.intelligence.facebook_identity import identity_or_none
        return identity_or_none(source.url)
    return source.url


async def freeze_inventory(db, cycle):
    from app.intelligence.rss_adapter import rss_url_is_supported
    cycle = await db.scalar(select(CollectionCycle).where(CollectionCycle.id == cycle.id)
                            .with_for_update().execution_options(populate_existing=True))
    if not cycle.inventory_frozen:
        sources = list(await db.scalars(select(NewsSource).where(NewsSource.status == 'ACTIVE').order_by(NewsSource.id)))
        for source in sources:
            from app.intelligence.social_schedule import SCHEDULE_PHASES
            if cycle.phase in SCHEDULE_PHASES:
                from app.intelligence.collection_exception import applies
                experiment = applies(cycle, cycle.cutoff_at)
                if not settings.NEWS_ENABLE_COLLECTION or (source.type not in {'LINKEDIN', 'FACEBOOK'} and not experiment):
                    continue
                if source.type == 'LINKEDIN' and not settings.NEWS_LINKEDIN_FETCH_ENABLED:
                    continue
            from app.intelligence.facebook_identity import source_approved
            supported = (source.type == 'LINKEDIN' or source.type in {'RSS', 'WEBSITE'} and rss_url_is_supported(source.url)
                         or source.type == 'FACEBOOK' and settings.NEWS_FACEBOOK_FETCH_ENABLED and source_approved(source))
            identity = source_identity(source)
            if not supported or not identity:
                continue
            point = await latest_verified_checkpoint(db, source.id)
            if point and point > cycle.cutoff_at:
                point = cycle.cutoff_at
            bootstrap = datetime.combine(cycle.cutoff_at.date()-timedelta(days=1), time(), timezone.utc)
            if cycle.phase in SCHEDULE_PHASES:
                from app.intelligence.social_schedule import previous_slot
                bootstrap = previous_slot(cycle.cutoff_at)
                from app.intelligence.collection_exception import applies
                if applies(cycle, cycle.cutoff_at):
                    # This owner-authorized experiment measures the last slot;
                    # it does not silently buy a historical backfill.
                    point = bootstrap
            db.add(CollectionCycleSource(cycle_id=cycle.id, source_id=source.id, name=source.name,
                type=source.type, canonical_url=identity, window_start=point or bootstrap,
                window_end=cycle.cutoff_at, verified_start=point is not None))
        cycle.inventory_frozen, cycle.state = True, 'running'
    await db.commit()


async def accept_manual_cycle(db, *, user_id, now):
    config = await trial_configuration(db)
    if settings.NEWS_SOCIAL_SCHEDULE_ENABLED:
        raise ValueError("LinkedIn and Facebook checks run at 07:00 and 21:00 Europe/Budapest.")
    if not trial_active(config, now) or config.get('user_id') != str(user_id):
        raise ValueError('No collection trial is enabled for this user and date.')
    if not settings.NEWS_ENABLE_COLLECTION:
        raise ValueError('Source collection is paused.')
    phase = 'evening' if now.astimezone(ZONE).time() >= time(21) else 'manual'
    cycle = await ensure_cycle(db, phase=phase, now=now)
    await freeze_inventory(db, cycle)
    return await cycle_status(db, cycle.id)


async def trial_owns_source(db, source_id, now):
    if settings.NEWS_SOCIAL_SCHEDULE_ENABLED:
        return await db.scalar(select(NewsSource.type).where(NewsSource.id == source_id)) in {'LINKEDIN', 'FACEBOOK'}
    if trial_active(await trial_configuration(db), now):
        return True
    return bool(await db.scalar(select(CollectionCycleSource.id).join(BrightDataExecution,
        BrightDataExecution.id == CollectionCycleSource.execution_id).where(
        CollectionCycleSource.source_id == source_id,
        BrightDataExecution.state.in_(['reserved', 'running', 'retry', 'uncertain'])).limit(1)))


async def dispatch_cycle_source(db, row_id, now, *, allow_new_requests=True, clock=None):
    from app.intelligence.collection import check_source, protected_linkedin_check, protected_facebook_check
    row = await db.get(CollectionCycleSource, row_id)
    now = clock() if clock else now
    if row.state in TERMINAL:
        return row.state
    source = await db.get(NewsSource, row.source_id)
    if not source or source.status != 'ACTIVE' or source.type != row.type or source_identity(source) != row.canonical_url:
        row.state, row.coverage_warning = 'blocked', 'Source was paused, removed or changed after acceptance.'
        await db.commit()
        return row.state
    cycle = await db.get(CollectionCycle, row.cycle_id)
    if not row.execution_id and not await cycle_authorized(db, cycle, now):
        row.state, row.coverage_warning = 'blocked', 'Collection cycle expired or was stopped before collection started.'
        await db.commit()
        return row.state
    if source.pending_execution_id and source.pending_execution_id != row.execution_id:
        owner = await db.scalar(select(CollectionCycleSource).where(
            CollectionCycleSource.execution_id == source.pending_execution_id).limit(1))
        protected_check = protected_facebook_check if source.type == 'FACEBOOK' else protected_linkedin_check
        await protected_check(db, source, now, reason='trial-poll',
                                       cycle_source_id=owner.id if owner else None, clock=clock)
        return 'pending'
    if not row.execution_id and not allow_new_requests:
        row.state, row.coverage_warning = 'blocked', 'Fresh billing verification failed; no new request was authorized.'
        await db.commit()
        return row.state
    if not row.execution_id:
        point = await latest_verified_checkpoint(db, source.id)
        if point and row.window_start < point <= row.window_end:
            row.window_start, row.verified_start = point, True
        await db.commit()
    if row.type in {'LINKEDIN','FACEBOOK'}:
        protected_check = protected_facebook_check if row.type == 'FACEBOOK' else protected_linkedin_check
        result = await protected_check(db, source, now, cycle_source_id=row.id, clock=clock)
        await db.refresh(row)
        execution = await db.get(BrightDataExecution, row.execution_id) if row.execution_id else None
        if execution and execution.state in {'failed', 'uncertain'}:
            row.state, row.coverage_warning = execution.state, 'Collection failed or outcome is uncertain; existing reservation retained.'
        elif result in {'paused', 'unavailable'} and row.state not in TERMINAL:
            row.state, row.coverage_warning = 'blocked', source.linkedin_paused_reason or source.last_error or 'Collection is unavailable.'
    else:
        result = await check_source(db, source.id, force=True, now=clock() if clock else now,
                                    expected_identity=(row.type, row.canonical_url), strict_times=True)
        if result == 'completed':
            row.state, row.coverage_confirmed, row.completed_at = 'completed', True, now
        elif result in {'error', 'paused', 'unavailable'}:
            row.state, row.coverage_warning = 'failed', 'Source check did not complete.'
        elif result == 'identity-changed':
            row.state, row.coverage_warning = 'blocked', 'Source identity changed before collection.'
        elif result == 'partial':
            row.state, row.coverage_warning = 'partial', 'Publication times are unverified; interval coverage is incomplete.'
    await db.commit()
    return row.state


async def capture_phase_usage(db, cycle, when, now):
    from app.intelligence.collection_cycles import TrialUsageObservation
    from app.intelligence.trial_usage import capture_trial_usage
    label = f'{when}_{cycle.phase}'
    if await db.scalar(select(TrialUsageObservation.id).where(TrialUsageObservation.trial_date == cycle.trial_date,
                                                          TrialUsageObservation.label == label).limit(1)):
        return True
    try:
        await capture_trial_usage(db, label=label, now=now)
        return True
    except Exception:
        await db.rollback()
        # Missing billing proof blocks spending; no HTTP response/secrets in logs.
        return False


def utc_now():
    return datetime.now(timezone.utc)


async def run_collection_trial_reports(now=None):
    """Separate scheduler job: a long source check cannot hold the report deadline."""
    if settings.NEWS_SOCIAL_SCHEDULE_ENABLED:
        return
    from app.intelligence import collection
    from app.intelligence.cycle_digest import process_trial_digest
    now = now or utc_now()
    async with collection.SessionLocal() as db:
        if trial_active(await trial_configuration(db), now) and now.astimezone(ZONE).time() >= time(21):
            cycle = await ensure_cycle(db, phase='evening', now=now)
            await freeze_inventory(db, cycle)
        ids = list(await db.scalars(select(CollectionCycle.id).where(CollectionCycle.phase == 'evening')))
    for cid in ids:
        async with collection.SessionLocal() as db:
            await process_trial_digest(db, cid, now)


async def run_collection_trial_cycle(now=None, *, clock=None):
    if settings.NEWS_SOCIAL_SCHEDULE_ENABLED:
        return
    from app.intelligence import collection
    clock = clock or ((lambda: now) if now is not None else utc_now)
    now = clock()
    if not settings.NEWS_ENABLE_COLLECTION:
        return
    async with collection.engine.connect() as lock:
        key = 7302192100
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}):
            return
        try:
            async with collection.SessionLocal() as db:
                if trial_active(await trial_configuration(db), now) and now.astimezone(ZONE).time() >= time(21):
                    cycle = await ensure_cycle(db, phase='evening', now=now)
                    await freeze_inventory(db, cycle)
                cycles = list(await db.scalars(select(CollectionCycle).order_by(CollectionCycle.cutoff_at)))
            for cycle in cycles:
                async with collection.SessionLocal() as db:
                    billing_verified = await capture_phase_usage(db, cycle, 'before', clock())
                async with collection.SessionLocal() as db:
                    ids = list(await db.scalars(select(CollectionCycleSource.id).where(
                        CollectionCycleSource.cycle_id == cycle.id, CollectionCycleSource.state.not_in(TERMINAL))))
                for row_id in ids:
                    async with collection.SessionLocal() as db:
                        try:
                            await dispatch_cycle_source(db, row_id, clock(), allow_new_requests=billing_verified, clock=clock)
                        except Exception:
                            await db.rollback()
                            row = await db.get(CollectionCycleSource, row_id)
                            row.coverage_warning = 'Collection interrupted; inspect the reserved execution before resuming.'
                            row.state = 'uncertain'
                            await db.commit()
                async with collection.SessionLocal() as db:
                    status = await cycle_status(db, cycle.id)
                    if not status['pending']:
                        stored = await db.get(CollectionCycle, cycle.id)
                        stored.state, stored.finished_at = 'finished', stored.finished_at or clock()
                        await db.commit()
                    if not status['pending'] or clock() >= cycle.deadline_at:
                        await capture_phase_usage(db, cycle, 'after', clock())
                if cycle.phase == 'evening':
                    from app.intelligence.cycle_digest import process_trial_digest
                    async with collection.SessionLocal() as db:
                        await process_trial_digest(db, cycle.id, clock())
            async with collection.SessionLocal() as db:
                from app.intelligence.trial_usage import record_settled_usage
                await record_settled_usage(db, clock())
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
