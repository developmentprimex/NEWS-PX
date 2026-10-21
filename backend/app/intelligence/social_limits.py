"""Learn a bounded request size from observed snapshots, never from an assumed post rate."""
import math
from sqlalchemy import select

from app.config import settings
from app.intelligence.models import BrightDataExecution


async def record_limit_for(db, source, window=None):
    baseline = settings.NEWS_FACEBOOK_RECORD_LIMIT if source.type == 'FACEBOOK' else settings.NEWS_LINKEDIN_RECORD_LIMIT
    if not settings.NEWS_SOCIAL_ADAPTIVE_LIMITS:
        return baseline
    baseline = min(baseline, 3)
    previous = list(await db.scalars(select(BrightDataExecution).where(
        BrightDataExecution.source_id == source.id, BrightDataExecution.returned_records.is_not(None),
        BrightDataExecution.state.in_(['completed', 'partial']))
        .order_by(BrightDataExecution.started_at.desc()).limit(7)))
    if not previous:
        return baseline
    # A small buffer protects quiet accounts from a sudden third post. High-volume
    # sources receive larger caps; returned duplicates count because they cost credits too.
    observed = max(e.returned_records for e in previous)
    limit = max(baseline, math.ceil(observed * 1.5) + 1)
    if any(e.returned_records >= e.record_limit for e in previous):
        limit = max(limit, max(e.record_limit for e in previous) * 2)
    return min(settings.NEWS_SOCIAL_MAX_RECORD_LIMIT, limit)
