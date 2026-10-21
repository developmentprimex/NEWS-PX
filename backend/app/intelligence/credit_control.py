"""Durable conservative record reservations, serialized across every API worker."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.intelligence.models import BrightDataAccount, BrightDataExecution

logger = logging.getLogger(__name__)
UNSETTLED = {"reserved", "running", "retry", "uncertain", "legacy"}


def verified_scope(account, now):
    scope = getattr(account, 'billing_scope', None) or {}
    if not isinstance(scope, dict):
        return None
    if (scope.get('complete') is not True or not scope.get('account_ref')
            or scope.get('month') != month_key(now) or account.provider_month != month_key(now)
            or not account.usage_as_of or not account.verified_at
            or not timedelta(0) <= now - account.usage_as_of <= timedelta(hours=24)
            or not timedelta(0) <= now - account.verified_at <= timedelta(hours=24)):
        return None
    try:
        if datetime.fromisoformat(scope['usage_as_of']) != account.usage_as_of:
            return None
        datasets = scope['datasets']
        if (not datasets or sum(v['month'] for v in datasets.values()) != account.provider_billable_records
                or any(type(v['month']) is not int or v['month'] < 0 or len(v['daily']) != now.day
                    or any(type(n) is not int or n < 0 for n in v['daily'])
                    or sum(v['daily']) != v['month'] for v in datasets.values())):
            return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    return scope


def unreflected_execution(execution, scope, cutoff):
    # Unknown account provenance, unresolved POSTs and missing completion remain reserved.
    return (getattr(execution, 'billing_account_ref', None) != scope['account_ref']
        or getattr(execution, 'dataset_id', None) not in scope['datasets']
        or execution.state in UNSETTLED or not getattr(execution, 'completed_at', None)
        or execution.started_at >= cutoff or execution.completed_at >= cutoff)


def dataset_usage_totals(account, executions, now, dataset_ids):
    rows = [e for e in executions if e.dataset_id in dataset_ids]
    scope = verified_scope(account, now)
    if not scope or not set(dataset_ids).issubset(scope['datasets']):
        return {'month': sum(e.estimated_billable_usage for e in rows if e.budget_month == month_key(now)),
            'daily': sum(e.estimated_billable_usage for e in rows if e.started_at.date() == now.date())}
    pending = [e for e in rows if unreflected_execution(e, scope, account.usage_as_of)]
    return {'month': sum(scope['datasets'][k]['month'] for k in dataset_ids)
        + sum(e.estimated_billable_usage for e in pending if e.budget_month == month_key(now)
            or e.state in UNSETTLED),
        'daily': sum(scope['datasets'][k]['daily'][-1] for k in dataset_ids)
        + sum(e.estimated_billable_usage for e in pending if e.started_at.date() == now.date())}


def month_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def usage_totals(account, executions, now: datetime) -> dict:
    month = month_key(now)
    as_of = account.usage_as_of
    local = sum(e.estimated_billable_usage for e in executions if e.budget_month == month)
    # Old outstanding snapshots still expose the new month to charges.
    outstanding = sum(e.estimated_billable_usage for e in executions
                      if e.budget_month != month and (e.state in UNSETTLED or
                          (getattr(e, "completed_at", None) and month_key(e.completed_at) == month
                           and (not as_of or e.completed_at >= as_of))))
    actual = account.provider_billable_records if account.provider_month == month else None
    unreflected = sum(e.estimated_billable_usage for e in executions
                     if e.budget_month == month and
                     (not as_of or e.started_at >= as_of or e.state in UNSETTLED
                      or (getattr(e, "completed_at", None) and e.completed_at >= as_of)))
    effective = max(local, (actual or 0) + unreflected) + outstanding
    daily = sum(e.estimated_billable_usage for e in executions if e.started_at.date() == now.date())
    scope = verified_scope(account, now)
    if scope:
        pending = [e for e in executions if unreflected_execution(e, scope, as_of)]
        outstanding = sum(e.estimated_billable_usage for e in pending
            if e.budget_month != month and (e.state in UNSETTLED or
                (getattr(e, 'completed_at', None) and month_key(e.completed_at) == month)))
        effective = (actual or 0) + sum(e.estimated_billable_usage for e in pending if e.budget_month == month) + outstanding
        daily = sum(v['daily'][-1] for v in scope['datasets'].values()) + sum(
            e.estimated_billable_usage for e in pending if e.started_at.date() == now.date())
    ceiling = min(settings.NEWS_LINKEDIN_OPERATIONAL_BUDGET,
                  max(0, (account.verified_monthly_allowance or 0)
                      - settings.NEWS_LINKEDIN_RESERVED_ALLOWANCE))
    return {"month": month, "estimatedRecords": local, "providerRecords": actual,
            "effectiveRecords": effective, "outstandingPriorMonth": outstanding,
            "dailyRecords": daily, "operationalBudget": ceiling,
            "reservedAllowance": settings.NEWS_LINKEDIN_RESERVED_ALLOWANCE,
            "alerts": account.alert_levels or []}


def denial_reason(account, totals: dict, now: datetime, limit: int, *, daily_target=None) -> str | None:
    if not settings.NEWS_LINKEDIN_FETCH_ENABLED:
        return "LinkedIn fetching is disabled by configuration."
    if account.paused_reason:
        return account.paused_reason
    if not account.provider_active or account.provider_balance is None or account.provider_balance < 0:
        return "Provider account is inactive or has insufficient balance."
    if (account.provider_month != month_key(now) or account.provider_billable_records is None
            or not account.verified_at or not account.usage_as_of
            or account.usage_as_of > now or account.verified_at > now
            or now - account.usage_as_of > timedelta(hours=24)
            or now - account.verified_at > timedelta(hours=24)):
        return "Recent provider billing evidence is required (maximum age: 24 hours)."
    if totals["effectiveRecords"] + limit > totals["operationalBudget"]:
        return "Operational monthly record budget reached."
    if daily_target is None:
        daily_target = settings.NEWS_LINKEDIN_DAILY_TARGET
        if settings.NEWS_SOCIAL_FLEXIBLE_DAILY_BUDGET:
            # Unspent days may catch up, while the hard monthly ceiling remains authoritative.
            daily_target = totals['operationalBudget']
    if totals["dailyRecords"] + limit > daily_target:
        return "Daily record target reached."
    return None


async def locked_account(db):
    await db.execute(insert(BrightDataAccount).values(
        id=1, provider_active=False, alert_levels=[]
    ).on_conflict_do_nothing(index_elements=[BrightDataAccount.id]))
    return await db.scalar(select(BrightDataAccount).where(BrightDataAccount.id == 1)
                           .with_for_update().execution_options(populate_existing=True))


async def executions_for_budget(db):
    # Ledger is small at daily cadence and includes unresolved previous months.
    return list((await db.scalars(select(BrightDataExecution))).all())


def record_alerts(account, totals):
    if account.alert_month != totals["month"]:
        account.alert_month, account.alert_levels = totals["month"], []
    levels = set(account.alert_levels or [])
    for level in (50, 75, 90):
        if totals["operationalBudget"] and totals["effectiveRecords"] * 100 >= totals["operationalBudget"] * level and level not in levels:
            logger.warning("Bright Data record budget alert: %s%% (%s/%s)", level,
                           totals["effectiveRecords"], totals["operationalBudget"])
            levels.add(level)
    account.alert_levels = sorted(levels)


async def reserve_execution(db, source, now, start_date, end_date, *, reason="scheduled", limit=None, cycle_source_id=None, dataset_id=None):
    limit = limit or settings.NEWS_LINKEDIN_RECORD_LIMIT
    account = await locked_account(db)
    executions = await executions_for_budget(db)
    if cycle_source_id:
        from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
        row = await db.get(CollectionCycleSource, cycle_source_id)
        cycle = await db.get(CollectionCycle, row.cycle_id) if row else None
        if cycle and cycle.phase == 'recovery-day':
            import json
            from app.db import AppMetadata
            from app.intelligence.cycle_service import cycle_authorized
            record = await db.get(AppMetadata, 'collection-recovery:' + str(cycle.id))
            permit = json.loads(record.value) if record else {}
            consumed = sum(e.estimated_billable_usage for e in executions
                if e.trigger_reason == reason)
            if (not await cycle_authorized(db, cycle, now) or str(source.id) not in permit.get('source_ids', [])
                    or limit > permit.get('source_limits', {}).get(str(source.id), 0)
                    or consumed + limit > permit.get('max_reserved', 0)):
                source.last_error = 'Recovery inventory, reservation cap or authorization expired.'
                await db.commit()
                return None
    totals = usage_totals(account, executions, now)
    record_alerts(account, totals)
    from app.intelligence.collection_exception import for_source
    exception = await for_source(db, cycle_source_id, source.id, now)
    denial = denial_reason(account, totals, now, limit,
        daily_target=totals['operationalBudget'] if exception else None)
    if source.type == 'FACEBOOK':
        from app.intelligence.facebook_adapter import dataset_for
        from app.intelligence.facebook_identity import source_approved
        from app.intelligence.facebook_adapter import PAGE_DATASET, GROUP_DATASET
        facebook_totals = dataset_usage_totals(account, executions, now, {PAGE_DATASET, GROUP_DATASET})
        if not settings.NEWS_FACEBOOK_FETCH_ENABLED or not source_approved(source):
            denial = 'Facebook source is outside the activated pilot.'
        elif not exception and not settings.NEWS_SOCIAL_FLEXIBLE_DAILY_BUDGET and facebook_totals['daily'] + limit > settings.NEWS_FACEBOOK_DAILY_TARGET:
            denial = 'Facebook daily reservation budget reached.'
        elif not exception and not settings.NEWS_SOCIAL_FLEXIBLE_DAILY_BUDGET and facebook_totals['month'] + limit > settings.NEWS_FACEBOOK_MONTHLY_BUDGET:
            denial = 'Facebook monthly reservation budget reached.'
        if dataset_id != dataset_for(source):
            raise ValueError('Facebook dataset does not match the approved source kind.')
    if denial:
        source.last_error = denial
        await db.commit()
        return None
    # Extra checks cannot spend the allowance needed for the normal daily schedule.
    if reason == "scheduled-extra":
        from app.intelligence.models import NewsSource
        sources = (await db.scalars(select(NewsSource).where(
            NewsSource.type == "LINKEDIN", NewsSource.status == "ACTIVE"))).all()
        remaining_days = calendar.monthrange(now.year, now.month)[1] - now.day + 1
        baseline = len(sources) * settings.NEWS_LINKEDIN_RECORD_LIMIT
        if (totals["effectiveRecords"] + baseline * remaining_days + limit > totals["operationalBudget"]
                or totals["dailyRecords"] + baseline + limit > settings.NEWS_LINKEDIN_DAILY_TARGET):
            await db.commit()
            return None
    execution = BrightDataExecution(
        source_id=source.id, started_at=now, budget_month=month_key(now), trigger_reason=reason,
        endpoint="https://api.brightdata.com/datasets/v3/trigger",
        dataset_id=dataset_id or settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID,
        start_date=start_date, end_date=end_date, record_limit=limit,
        estimated_billable_usage=limit, state="reserved", newly_saved_posts=0,
        billing_account_ref=(verified_scope(account, now) or {}).get('account_ref'),
        duplicate_posts=0, ignored_records=0, retry_count=0,
        coverage_warning=(f"Routine window starts {start_date}; older dates require an authorized backfill."
            if reason.startswith("scheduled") and reason != "scheduled-day" and source.last_successful_fetch_at
            and start_date > source.last_successful_fetch_at.date() - timedelta(days=1) else None),
    )
    db.add(execution)
    await db.flush()
    if cycle_source_id:
        from app.intelligence.collection_cycles import CollectionCycleSource
        row = await db.get(CollectionCycleSource, cycle_source_id)
        if row is None or row.source_id != source.id or row.execution_id:
            raise ValueError('Trial source already reserved or identity changed.')
        row.execution_id, row.state = execution.id, 'reserved'
    source.pending_execution_id = execution.id
    source.last_started_at = now
    record_alerts(account, usage_totals(account, [*executions, execution], now))
    # Commit before the billable POST: even a crash consumes its reservation.
    await db.commit()
    return execution


async def budget_status(db):
    account = await db.get(BrightDataAccount, 1)
    if not account:
        return {"fetchEnabled": settings.NEWS_LINKEDIN_FETCH_ENABLED, "pausedReason": "Provider billing evidence has not been verified."}
    now = datetime.now(timezone.utc)
    totals = usage_totals(account, await executions_for_budget(db), now)
    return {**totals, "alerts": [level for level in (account.alert_levels or [])
                if totals['effectiveRecords'] * 100 >= totals['operationalBudget'] * level],
            "historicalAlerts": account.alert_levels or [],
            "dailyFetchEnabled": settings.NEWS_SOCIAL_DAILY_FETCH,
            "dailyFetchUTC": '00:05' if settings.NEWS_SOCIAL_DAILY_FETCH else None,
            "sharedMonthlyPool": settings.NEWS_SOCIAL_FLEXIBLE_DAILY_BUDGET,
            "fetchEnabled": settings.NEWS_LINKEDIN_FETCH_ENABLED,
            "providerActive": account.provider_active,
            "providerBalance": float(account.provider_balance) if account.provider_balance is not None else None,
            "providerUsageAsOf": account.usage_as_of, "verifiedAt": account.verified_at,
            "pausedReason": denial_reason(account, totals, now, settings.NEWS_LINKEDIN_RECORD_LIMIT)}
