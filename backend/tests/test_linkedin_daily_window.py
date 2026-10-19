"""Daily discovery covers closed UTC dates, with no routine overlap."""
from datetime import date, datetime

import pytest

from app.intelligence import linkedin_collection
from app.intelligence.linkedin_schedule import interval_minutes
from types import SimpleNamespace


@pytest.mark.parametrize("covered,first_attempt,now,want", [
    (None, None, "2026-10-20T08:00:00+00:00", (date(2026, 10, 19), date(2026, 10, 19))),
    (date(2026, 10, 19), None, "2026-10-21T08:00:00+00:00", (date(2026, 10, 20), date(2026, 10, 20))),
    (date(2026, 10, 19), None, "2026-10-20T23:59:59+00:00", None),
    (date(2026, 10, 21), None, "2026-10-20T08:00:00+00:00", None),
    (date(2026, 10, 16), date(2026, 10, 16), "2026-10-20T08:00:00+00:00", (date(2026, 10, 17), date(2026, 10, 17))),
    (None, date(2026, 10, 17), "2026-10-20T08:00:00+00:00", (date(2026, 10, 17), date(2026, 10, 17))),
    (None, None, "2026-10-20T00:30:00+02:00", (date(2026, 10, 18), date(2026, 10, 18))),
    (date(2026, 10, 18), None, "2026-10-19T23:30:00-02:00", (date(2026, 10, 19), date(2026, 10, 19))),
    (None, None, "2027-01-01T00:00:00+00:00", (date(2026, 12, 31), date(2026, 12, 31))),
    (date(2028, 2, 28), None, "2028-03-01T08:00:00+00:00", (date(2028, 2, 29), date(2028, 2, 29))),
])
def test_closed_day_progression_never_overlaps_or_skips_uncovered_days(covered, first_attempt, now, want):
    window = getattr(linkedin_collection, "daily_window", None)
    assert callable(window), "Closed UTC daily-window selection is missing"
    assert window(covered, datetime.fromisoformat(now), first_attempt=first_attempt) == want


@pytest.mark.parametrize("streak", [0, 7, 14, 50])
def test_quiet_sources_still_get_daily_coverage(streak):
    source = SimpleNamespace(empty_fetch_streak=streak, priority="HIGH", last_new_posts=0,
                             fetch_interval_minutes=1440)
    assert interval_minutes(source) == 1440


def test_explicitly_slower_source_interval_remains_a_floor():
    source = SimpleNamespace(empty_fetch_streak=14, priority="HIGH", last_new_posts=2,
                             fetch_interval_minutes=2880)
    assert interval_minutes(source, extra=True) == 2880
