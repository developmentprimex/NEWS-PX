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
    return source.url


async def freeze_inventory(db, cycle):
    from app.intelligence.rss_adapter import rss_url_is_supported
    cycle = await db.scalar(select(CollectionCycle).where(CollectionCycle.id == cycle.id)
                            .with_for_update().execution_options(populate_existing=True))
    if not cycle.inventory_frozen:
        sources = list(await db.scalars(select(NewsSource).where(NewsSource.status == 'ACTIVE').order_by(NewsSource.id)))
        for source in sources:
            supported = source.type == 'LINKEDIN' or source.type in {'RSS', 'WEBSITE'} and rss_url_is_supported(source.url)
            identity = source_identity(source)
            if not supported or not identity:
                continue
            point = await latest_verified_checkpoint(db, source.id)
            if point and point > cycle.cutoff_at:
                point = cycle.cutoff_at
            bootstrap = datetime.combine(cycle.cutoff_at.date()-timedelta(days=1), time(), timezone.utc)
            db.add(CollectionCycleSource(cycle_id=cycle.id, source_id=source.id, name=source.name,
                type=source.type, canonical_url=identity, window_start=point or bootstrap,
                window_end=cycle.cutoff_at, verified_start=point is not None))
        cycle.inventory_frozen, cycle.state = True, 'running'
    await db.commit()


async def accept_manual_cycle(db, *, user_id, now):
    config = await trial_configuration(db)
    if not trial_active(config, now) or config.get('user_id') != str(user_id):
        raise ValueError('No collection trial is enabled for this user and date.')
    if not settings.NEWS_ENABLE_COLLECTION:
        raise ValueError('Source collection is paused.')
    phase = 'evening' if now.astimezone(ZONE).time() >= time(21) else 'manual'
    cycle = await ensure_cycle(db, phase=phase, now=now)
    await freeze_inventory(db, cycle)
    return await cycle_status(db, cycle.id)


async def trial_owns_source(db, source_id, now):
    if trial_active(await trial_configuration(db), now):
        return True
    return bool(await db.scalar(select(CollectionCycleSource.id).join(BrightDataExecution,
        BrightDataExecution.id == CollectionCycleSource.execution_id).where(
        CollectionCycleSource.source_id == source_id,
        BrightDataExecution.state.in_(['reserved', 'running', 'retry', 'uncertain'])).limit(1)))


async def dispatch_cycle_source(db, row_id, now):
    from app.intelligence.collection import check_source, protected_linkedin_check
    row = await db.get(CollectionCycleSource, row_id)
    if row.state in TERMINAL:
        return row.state
    source = await db.get(NewsSource, row.source_id)
    if not source or source.status != 'ACTIVE' or source.type != row.type or source_identity(source) != row.canonical_url:
        row.state, row.coverage_warning = 'blocked', 'Source was paused, removed or changed after acceptance.'
        await db.commit()
        return row.state
    if not row.execution_id and not trial_active(await trial_configuration(db), now):
        row.state, row.coverage_warning = 'blocked', 'Trial expired or was stopped before collection started.'
        await db.commit()
        return row.state
    if source.pending_execution_id and source.pending_execution_id != row.execution_id:
        await protected_linkedin_check(db, source, now, reason='trial-poll')
        return 'pending'
    if not row.execution_id:
        point = await latest_verified_checkpoint(db, source.id)
        if point and row.window_start < point <= row.window_end:
            row.window_start, row.verified_start = point, True
        await db.commit()
    if row.type == 'LINKEDIN':
        result = await protected_linkedin_check(db, source, now, cycle_source_id=row.id)
        await db.refresh(row)
        execution = await db.get(BrightDataExecution, row.execution_id) if row.execution_id else None
        if execution and execution.state in {'failed', 'uncertain'}:
            row.state, row.coverage_warning = execution.state, 'Collection failed or outcome is uncertain; existing reservation retained.'
        elif result in {'paused', 'unavailable'} and row.state not in TERMINAL:
            row.state, row.coverage_warning = 'blocked', source.linkedin_paused_reason or source.last_error or 'Collection is unavailable.'
    else:
        result = await check_source(db, source.id, force=True)
        if result == 'completed':
            row.state, row.coverage_confirmed, row.completed_at = 'completed', True, now
        elif result in {'error', 'paused', 'unavailable'}:
            row.state, row.coverage_warning = 'failed', 'Source check did not complete.'
    await db.commit()
    return row.state


async def run_collection_trial_cycle(now=None):
    from app.intelligence import collection
    now = now or datetime.now(timezone.utc)
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
                    ids = list(await db.scalars(select(CollectionCycleSource.id).where(
                        CollectionCycleSource.cycle_id == cycle.id, CollectionCycleSource.state.not_in(TERMINAL))))
                for row_id in ids:
                    async with collection.SessionLocal() as db:
                        try:
                            await dispatch_cycle_source(db, row_id, now)
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
                        stored.state, stored.finished_at = 'finished', stored.finished_at or now
                        await db.commit()
                if cycle.phase == 'evening':
                    from app.intelligence.cycle_digest import process_trial_digest
                    async with collection.SessionLocal() as db:
                        await process_trial_digest(db, cycle.id, now)
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
