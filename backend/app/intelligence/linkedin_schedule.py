"""Daily eligibility; even quiet sources retain one closed-day check per day."""
from datetime import timedelta


def interval_minutes(source, *, extra=False):
    # Keep the keyword for existing operator callers. Daily coverage does not
    # shorten for priority or silently lengthen after empty results.
    return max(1440, source.fetch_interval_minutes)


def is_due(source, now, *, extra=False):
    return not source.last_started_at or now - source.last_started_at >= timedelta(minutes=interval_minutes(source, extra=extra))
