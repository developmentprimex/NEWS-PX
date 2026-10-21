"""Explicit one-evening experiment. Ordinary limits and pause history stay intact."""
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import AppMetadata
from app.intelligence.models import BrightDataAccount, BrightDataExecution

ZONE = ZoneInfo('Europe/Budapest')
RECORD_LIMIT = 80
SATURATION_PAUSE = 'Record limit reached; completeness is unknown. Authorize a separate backfill or reconcile the window.'


def applies(cycle, now):
    day = settings.NEWS_COLLECTION_EXCEPTION_DATE
    if not day or not settings.NEWS_SOCIAL_SCHEDULE_ENABLED or not settings.NEWS_ENABLE_COLLECTION:
        return False
    cutoff = datetime.combine(day, time(21), ZONE).astimezone(timezone.utc)
    return bool(cycle and cycle.phase == 'scheduled-pm' and cycle.trial_date == day
                and cycle.cutoff_at == cutoff and cutoff <= now < cutoff + timedelta(minutes=30))


async def for_source(db, cycle_source_id, source_id, now):
    if not cycle_source_id:
        return False
    from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
    row = await db.get(CollectionCycleSource, cycle_source_id)
    if not row or row.source_id != source_id:
        return False
    return applies(await db.get(CollectionCycle, row.cycle_id), now)


async def allows_saturation_retry(db, source, cycle_source_id, now):
    if source.linkedin_paused_reason != SATURATION_PAUSE or not cycle_source_id:
        return False
    if not source.pending_execution_id:
        if await for_source(db, cycle_source_id, source.id, now):
            return True
        if settings.NEWS_SOCIAL_ADAPTIVE_LIMITS:
            from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
            from app.intelligence.cycle_service import cycle_authorized
            from app.intelligence.social_limits import record_limit_for
            row = await db.get(CollectionCycleSource, cycle_source_id)
            cycle = await db.get(CollectionCycle, row.cycle_id) if row else None
            previous = await db.scalar(select(BrightDataExecution).where(
                BrightDataExecution.source_id == source.id).order_by(BrightDataExecution.started_at.desc()).limit(1))
            if (row and row.source_id == source.id and not row.execution_id and previous
                    and previous.completed_at and previous.completed_at < cycle.cutoff_at
                    and await cycle_authorized(db, cycle, now)):
                return (source.type == 'FACEBOOK' or await record_limit_for(db, source, row) > previous.record_limit)
        return False
    # A previously saturated source must also be able to retrieve this exact
    # experiment's reserved snapshot after the start window expires.
    from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
    row = await db.get(CollectionCycleSource, cycle_source_id)
    if not row or row.source_id != source.id or row.execution_id != source.pending_execution_id:
        return False
    cycle = await db.get(CollectionCycle, row.cycle_id)
    return bool(cycle and now >= cycle.cutoff_at and (applies(cycle, cycle.cutoff_at)
        or settings.NEWS_SOCIAL_ADAPTIVE_LIMITS and row.execution_id == source.pending_execution_id))


async def observe(db, cycle, now, *, before=False):
    """Persist provider-reported usage separately from conservative reservations.

    At most one GET-only refresh per ten minutes after the cycle, through noon
    tomorrow. A delayed rollup is labelled provisional until it covers completion.
    Observations do not change pauses, reservations, subscriptions or SMTP claims.
    """
    day = settings.NEWS_COLLECTION_EXCEPTION_DATE
    if not day or cycle.phase != 'scheduled-pm' or cycle.trial_date != day:
        return
    end = datetime.combine(day + timedelta(days=1), time(12), ZONE).astimezone(timezone.utc)
    if not before and (cycle.state != 'finished' or now >= end):
        return
    key = 'collection-exception:' + str(cycle.id)
    await db.execute(insert(AppMetadata).values(key=key, value='[]').on_conflict_do_nothing())
    record = await db.scalar(select(AppMetadata).where(AppMetadata.key == key).with_for_update())
    observations = json.loads(record.value)
    if before and observations:
        await db.commit()
        return
    if observations and not before and now - datetime.fromisoformat(observations[-1]['observedAt']) < timedelta(minutes=10):
        await db.commit()
        return
    if not before:
        from app.intelligence.trial_usage import refresh_shared_billing
        await refresh_shared_billing(db, now)
    account = await db.get(BrightDataAccount, 1)
    from app.intelligence.collection_cycles import CollectionCycleSource
    rows = list(await db.scalars(select(BrightDataExecution).join(CollectionCycleSource,
        CollectionCycleSource.execution_id == BrightDataExecution.id).where(CollectionCycleSource.cycle_id == cycle.id)))
    provider_cutoff = account.usage_as_of
    settled = bool(rows and all(e.state not in {'reserved','running','retry','uncertain','legacy'}
        and e.completed_at and provider_cutoff and e.completed_at < provider_cutoff for e in rows))
    baseline = observations[0] if observations else None
    delta = (account.provider_billable_records - baseline['providerRecords']
        if baseline and baseline['providerMonth'] == account.provider_month
        and baseline['providerRecords'] is not None and account.provider_billable_records is not None else None)
    observations.append({'stage': 'before' if before else 'after', 'observedAt': now.isoformat(),
        'providerUsageAsOf': provider_cutoff.isoformat() if provider_cutoff else None,
        'providerRecords': account.provider_billable_records, 'providerMonth': account.provider_month,
        'providerDeltaRecords': delta if delta is not None and delta >= 0 else None,
        'providerBalance': float(account.provider_balance) if account.provider_balance is not None else None,
        'provisional': not settled, 'executions': len(rows),
        'reservedRecords': sum(e.estimated_billable_usage for e in rows),
        'returnedRecords': sum(e.returned_records or 0 for e in rows),
        'newItems': sum(e.newly_saved_posts for e in rows),
        'partialExecutions': sum(e.state == 'partial' for e in rows),
        'failedOrUncertain': sum(e.state in {'failed', 'uncertain'} for e in rows)})
    record.value = json.dumps(observations)
    await db.commit()
