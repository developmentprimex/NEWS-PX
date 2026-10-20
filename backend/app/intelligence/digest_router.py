"""Settings and test delivery for the source email report."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import User, get_current_user
from app.config import settings
from app.db import get_db
from app.intelligence.digest_service import ensure_digest_settings, send_digest_test
from app.intelligence.cycle_service import trial_active, trial_configuration
from app.intelligence.cycle_digest import recipient_fingerprint
from app.intelligence.schemas import DigestSettingsInput
from app.intelligence.social_schedule import SCHEDULE_TIMES

router = APIRouter()
logger = logging.getLogger(__name__)


async def _report_credentials(db: AsyncSession, user_id) -> tuple[str, str] | None:
    if settings.EMAIL_USER and settings.EMAIL_PASSWORD:
        return settings.EMAIL_USER, settings.EMAIL_PASSWORD
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED and settings.NEWS_TRIAL_EMAIL_USER and settings.NEWS_TRIAL_EMAIL_PASSWORD:
        config = await trial_configuration(db)
        if (trial_active(config, datetime.now(timezone.utc))
                and config.get('user_id') == str(user_id)
                and config.get('recipient_fingerprint') == recipient_fingerprint(settings.NEWS_EMAIL_RECIPIENT)):
            return settings.NEWS_TRIAL_EMAIL_USER, settings.NEWS_TRIAL_EMAIL_PASSWORD
    return None


def _settings_out(digest, credentials: tuple[str, str] | None) -> dict:
    return {"times": SCHEDULE_TIMES if settings.NEWS_SOCIAL_SCHEDULE_ENABLED else digest.times,
            "scheduleManaged": settings.NEWS_SOCIAL_SCHEDULE_ENABLED, "timezone": "Europe/Budapest", "recipient": settings.NEWS_EMAIL_RECIPIENT,
            "lastSentAt": digest.last_sent_at, "lastError": digest.last_error,
            "emailConfigured": credentials is not None}


@router.get("/digest-settings")
async def get_digest_settings(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    digest = await ensure_digest_settings(db, user.id)
    credentials = await _report_credentials(db, user.id)
    await db.commit()
    return _settings_out(digest, credentials)


@router.put("/digest-settings")
async def update_digest_settings(payload: DigestSettingsInput, db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)) -> dict:
    digest = await ensure_digest_settings(db, user.id)
    if settings.NEWS_SOCIAL_SCHEDULE_ENABLED and payload.times != SCHEDULE_TIMES:
        raise HTTPException(status_code=422, detail="Combined reports follow the 07:00 and 21:00 collection cycles.")
    digest.times = payload.times
    if not settings.NEWS_SOCIAL_SCHEDULE_ENABLED:
        digest.last_slot_at = datetime.now(timezone.utc)
    credentials = await _report_credentials(db, user.id)
    await db.commit()
    return _settings_out(digest, credentials)


@router.post("/digest-settings/test", status_code=status.HTTP_204_NO_CONTENT)
async def test_digest_email(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    credentials = await _report_credentials(db, user.id)
    if credentials is None:
        raise HTTPException(status_code=503, detail="SMTP is not configured on the server.")
    try:
        await send_digest_test(credentials=credentials)
    except Exception as exc:
        logger.exception("Digest test email failed")
        raise HTTPException(status_code=502, detail="Test email failed. Check the SMTP settings and server logs.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
