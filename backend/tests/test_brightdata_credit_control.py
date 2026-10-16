import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app.brightdata_admin import parse_evidence
from app.intelligence import credit_control as credit
from app.intelligence.linkedin_collection import incremental_start
from app.intelligence.linkedin_schedule import interval_minutes
from app.intelligence.schemas import NewsSourceCreate

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def account(**kwargs):
    return NS(provider_month="2026-10", provider_billable_records=0, provider_balance=0,
              provider_active=True, verified_monthly_allowance=5000, usage_as_of=NOW,
              verified_at=NOW, paused_reason=None, alert_month=None, alert_levels=[], **kwargs)


def execution(usage=6, **kwargs):
    data = dict(estimated_billable_usage=usage, budget_month="2026-10", started_at=NOW, state="completed")
    return NS(**{**data, **kwargs})


def test_default_interval_and_adaptive_backoff(monkeypatch):
    source = NS(empty_fetch_streak=0, priority="HIGH", last_new_posts=2, fetch_interval_minutes=5)
    assert interval_minutes(source) == 1440
    source.empty_fetch_streak = 7
    assert interval_minutes(source) == 4320
    source.empty_fetch_streak = 14
    assert interval_minutes(source) == 10080
    source.empty_fetch_streak = 0
    monkeypatch.setattr(credit.settings, "NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA", True)
    assert interval_minutes(source, extra=True) == 720
    source.last_new_posts = 0
    assert interval_minutes(source, extra=True) == 1440


def test_linkedin_form_clamps_legacy_interval():
    source = NewsSourceCreate(name="Mendex", url="https://www.linkedin.com/company/mendex-ai/", type="LINKEDIN", fetch_interval_minutes=5)
    assert source.fetch_interval_minutes == 1440


def test_incremental_dates_never_default_to_sixty_days():
    assert incremental_start(None, NOW).isoformat() == "2026-10-13"
    assert incremental_start(NOW - timedelta(days=1), NOW).isoformat() == "2026-10-12"
    assert incremental_start(NOW - timedelta(days=60), NOW).isoformat() == "2026-10-07"


@pytest.mark.parametrize("used,limit,blocked", [(3994, 6, False), (3995, 6, True), (4000, 1, True)])
def test_monthly_reservation_boundaries(monkeypatch, used, limit, blocked):
    monkeypatch.setattr(credit.settings, "NEWS_LINKEDIN_FETCH_ENABLED", True)
    a = account()
    e = execution(used, started_at=NOW - timedelta(days=1))
    totals = credit.usage_totals(a, [e], NOW)
    assert bool(credit.denial_reason(a, totals, NOW, limit)) == blocked


def test_provider_usage_separate_and_overrides_low_estimate(monkeypatch):
    monkeypatch.setattr(credit.settings, "NEWS_LINKEDIN_FETCH_ENABLED", True)
    a = account()
    a.provider_billable_records = 3998
    totals = credit.usage_totals(a, [execution(6, started_at=NOW - timedelta(days=2))], NOW)
    assert totals["estimatedRecords"] == 6 and totals["providerRecords"] == 3998
    assert credit.denial_reason(a, totals, NOW, 6)


def test_pending_prior_month_is_not_forgotten():
    totals = credit.usage_totals(account(), [execution(100, budget_month="2026-09", state="uncertain")], NOW)
    assert totals["outstandingPriorMonth"] == totals["effectiveRecords"] == 100


def test_results_completed_after_provider_snapshot_remain_unreflected():
    a = account()
    a.provider_billable_records = 3995
    run = execution(6, started_at=NOW - timedelta(hours=1), completed_at=NOW + timedelta(seconds=1))
    assert credit.usage_totals(a, [run], NOW)["effectiveRecords"] == 4001


def test_prior_month_results_delivered_now_stay_reserved_until_reconciled():
    run = execution(100, budget_month="2026-09", started_at=NOW - timedelta(days=30), completed_at=NOW)
    assert credit.usage_totals(account(), [run], NOW)["outstandingPriorMonth"] == 100


@pytest.mark.parametrize("field,value", [("provider_active", False), ("provider_balance", -1),
    ("provider_balance", None), ("provider_month", "2026-09"), ("usage_as_of", NOW - timedelta(hours=25)),
    ("usage_as_of", NOW + timedelta(seconds=1)), ("verified_at", None), ("paused_reason", "inactive")])
def test_account_guards(monkeypatch, field, value):
    monkeypatch.setattr(credit.settings, "NEWS_LINKEDIN_FETCH_ENABLED", True)
    a = account()
    setattr(a, field, value)
    assert credit.denial_reason(a, credit.usage_totals(a, [], NOW), NOW, 6)


def test_alerts_are_persistent_and_emitted_once(caplog):
    a = account()
    totals = credit.usage_totals(a, [execution(3600)], NOW)
    credit.record_alerts(a, totals)
    credit.record_alerts(a, totals)
    assert a.alert_levels == [50, 75, 90]
    assert len(caplog.records) == 3


def test_billing_evidence_validation():
    payload = dict(month="2026-10", usage_as_of=NOW.isoformat(), balance=-1,
                   active=False, billable_records=7049, verified_monthly_allowance=5000, evidence="Provider billing export")
    assert parse_evidence(payload, NOW)[1] == -1
    with pytest.raises(ValueError):
        parse_evidence({**payload, "balance": "NaN"}, NOW)
    with pytest.raises(ValueError):
        parse_evidence({**payload, "billable_records": True}, NOW)
