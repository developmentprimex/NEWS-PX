"""Retain collection accounting identity when an administrator deletes content."""
import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db import AppMetadata
from app.intelligence.models import BrightDataExecution
from app.intelligence.facebook_identity import identity_or_none as facebook_identity
from app.intelligence.linkedin_identity import identity_or_none as linkedin_identity

TIMESTAMPS = ('last_started_at', 'last_checked_at', 'last_successful_fetch_at')
COLLECTION_STATE = ('empty_fetch_streak', 'last_new_posts', 'linkedin_paused_reason', 'last_error')


def history_key(source):
    identity = (linkedin_identity(source.url) if source.type == 'LINKEDIN' else
                facebook_identity(source.url) if source.type == 'FACEBOOK' else None)
    if identity:
        return 'source-history:' + hashlib.sha256(f'{source.type}:{identity}'.encode()).hexdigest()
    return None


async def retain_collection_history(db, source):
    """Caller holds the canonical identity lock through deletion and commit."""
    key = history_key(source)
    if key is None:
        return
    record = await db.get(AppMetadata, key)
    # An old duplicate must not replace the deleted collection owner's ledger ID.
    if record is not None and json.loads(record.value)['id'] != str(source.id):
        return
    if not any(getattr(source, field) for field in TIMESTAMPS) and not source.linkedin_paused_reason:
        execution = await db.scalar(select(BrightDataExecution.id).where(
            BrightDataExecution.source_id == source.id).limit(1))
        if execution is None:
            return
    state = {field: getattr(source, field) for field in COLLECTION_STATE}
    state.update({field: value.isoformat() if (value := getattr(source, field)) else None
                  for field in TIMESTAMPS})
    state['id'] = str(source.id)
    if record is None:
        record = AppMetadata(key=key, value='')
        db.add(record)
    record.value = json.dumps(state)


async def restore_collection_history(db, source):
    """Readding the same URL reuses its ledger ID, checkpoints and coverage pause."""
    key = history_key(source)
    record = await db.get(AppMetadata, key) if key else None
    if record is None:
        return
    state = json.loads(record.value)
    source.id = UUID(state['id'])
    for field in COLLECTION_STATE:
        setattr(source, field, state[field])
    for field in TIMESTAMPS:
        setattr(source, field, datetime.fromisoformat(state[field]) if state[field] else None)
    await db.delete(record)


async def collection_history_conflict(db, source):
    key = history_key(source)
    record = await db.get(AppMetadata, key) if key else None
    return record is not None and json.loads(record.value)['id'] != str(source.id)
