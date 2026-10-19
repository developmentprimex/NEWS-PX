"""One-day collection coordination; no work is enabled by importing this module."""
import json
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import AppMetadata
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, CycleStatus
from app.intelligence.models import BrightDataExecution

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
