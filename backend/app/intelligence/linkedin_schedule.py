"""One daily attempt initially; inactivity backs off and extras are opt-in."""
from datetime import timedelta

from app.config import settings


def interval_minutes(source, *, extra=False):
    streak = source.empty_fetch_streak or 0
    interval = 10080 if streak >= 14 else 4320 if streak >= 7 else 1440
    if extra and settings.NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA and source.priority == "HIGH" and source.last_new_posts > 0 and streak == 0:
        interval = 720
    configured = source.fetch_interval_minutes
    # A 1440-minute default permits explicitly enabled adaptive extra checks;
    # longer operator-selected intervals remain a floor.
    return max(interval, configured if configured > 1440 else 0)


def is_due(source, now, *, extra=False):
    return not source.last_started_at or now - source.last_started_at >= timedelta(minutes=interval_minutes(source, extra=extra))
