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
    ceiling = min(settings.NEWS_LINKEDIN_OPERATIONAL_BUDGET,
                  max(0, (account.verified_monthly_allowance or 0)
                      - settings.NEWS_LINKEDIN_RESERVED_ALLOWANCE))
    return {"month": month, "estimatedRecords": local, "providerRecords": actual,
            "effectiveRecords": effective, "outstandingPriorMonth": outstanding,
            "dailyRecords": daily, "operationalBudget": ceiling,
            "reservedAllowance": settings.NEWS_LINKEDIN_RESERVED_ALLOWANCE,
            "alerts": account.alert_levels or []}


def denial_reason(account, totals: dict, now: datetime, limit: int) -> str | None:
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
    if totals["dailyRecords"] + limit > settings.NEWS_LINKEDIN_DAILY_TARGET:
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


async def reserve_execution(db, source, now, start_date, end_date, *, reason="scheduled", limit=None):
    limit = limit or settings.NEWS_LINKEDIN_RECORD_LIMIT
    account = await locked_account(db)
    executions = await executions_for_budget(db)
    totals = usage_totals(account, executions, now)
    record_alerts(account, totals)
    denial = denial_reason(account, totals, now, limit)
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
        dataset_id=settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID,
        start_date=start_date, end_date=end_date, record_limit=limit,
        estimated_billable_usage=limit, state="reserved", newly_saved_posts=0,
        duplicate_posts=0, ignored_records=0, retry_count=0,
        coverage_warning=(f"Routine window starts {start_date}; older dates require an authorized backfill."
            if reason.startswith("scheduled") and source.last_successful_fetch_at
            and start_date > source.last_successful_fetch_at.date() - timedelta(days=1) else None),
    )
    db.add(execution)
    await db.flush()
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
    return {**totals, "alerts": account.alert_levels or [],
            "fetchEnabled": settings.NEWS_LINKEDIN_FETCH_ENABLED,
            "providerActive": account.provider_active,
            "providerBalance": float(account.provider_balance) if account.provider_balance is not None else None,
            "providerUsageAsOf": account.usage_as_of, "verifiedAt": account.verified_at,
            "pausedReason": denial_reason(account, totals, now, settings.NEWS_LINKEDIN_RECORD_LIMIT)}
