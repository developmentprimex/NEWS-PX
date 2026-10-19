import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.intelligence import digest_router, digest_service
from app.intelligence.schemas import DigestSettingsInput


@pytest.fixture
def configured_trial(monkeypatch):
    user = SimpleNamespace(id=uuid4())
    recipient = 'recipient@example.com'
    config = {'enabled': True, 'date': datetime.now(ZoneInfo('Europe/Budapest')).date().isoformat(),
              'user_id': str(user.id),
              'recipient_fingerprint': hashlib.sha256(recipient.encode()).hexdigest()}
    for key, value in {'EMAIL_USER': '', 'EMAIL_PASSWORD': '', 'NEWS_EMAIL_RECIPIENT': recipient,
                       'NEWS_TRIAL_EMAIL_USER': 'sender@example.com',
                       'NEWS_TRIAL_EMAIL_PASSWORD': 'test-app-password'}.items():
        monkeypatch.setattr(digest_router.settings, key, value)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(value=json.dumps(config))),
                         commit=AsyncMock())
    digest = SimpleNamespace(times=['21:00'], last_sent_at=None, last_error=None)
    monkeypatch.setattr(digest_router, 'ensure_digest_settings', AsyncMock(return_value=digest))
    return user, config, db, digest


def configure_db(db, config):
    db.get.return_value = SimpleNamespace(value=json.dumps(config))


@pytest.mark.parametrize('method', ['get', 'put'])
def test_active_authorized_trial_is_configured_in_settings(configured_trial, method):
    user, _, db, _ = configured_trial
    if method == 'get':
        output = asyncio.run(digest_router.get_digest_settings(db=db, user=user))
    else:
        output = asyncio.run(digest_router.update_digest_settings(DigestSettingsInput(times=['21:00']), db=db, user=user))
    assert output['emailConfigured'] is True
    assert output['recipient'] == 'recipient@example.com'
    assert 'test-app-password' not in str(output)


@pytest.mark.parametrize('invalid', ['disabled', 'expired', 'future', 'other_user', 'other_recipient', 'no_config', 'no_password'])
def test_ineligible_trial_stays_unconfigured_and_cannot_send(configured_trial, monkeypatch, invalid):
    user, config, db, _ = configured_trial
    if invalid == 'disabled':
        config['enabled'] = False
    elif invalid in {'expired', 'future'}:
        day = datetime.now(ZoneInfo('Europe/Budapest')).date()
        config['date'] = (day + timedelta(days=1 if invalid == 'future' else -1)).isoformat()
    elif invalid == 'other_user':
        config['user_id'] = str(uuid4())
    elif invalid == 'other_recipient':
        config['recipient_fingerprint'] = 'wrong'
    elif invalid == 'no_password':
        monkeypatch.setattr(digest_router.settings, 'NEWS_TRIAL_EMAIL_PASSWORD', '')
    configure_db(db, config)
    if invalid == 'no_config':
        db.get.return_value = None
    send = AsyncMock()
    monkeypatch.setattr(digest_router, 'send_digest_test', send)
    output = asyncio.run(digest_router.get_digest_settings(db=db, user=user))
    assert output['emailConfigured'] is False
    with pytest.raises(HTTPException) as error:
        asyncio.run(digest_router.test_digest_email(user=user, db=db))
    assert error.value.status_code == 503
    send.assert_not_awaited()


def test_test_button_uses_the_authorized_trial_transport(configured_trial, monkeypatch):
    user, _, db, _ = configured_trial
    send = AsyncMock()
    monkeypatch.setattr(digest_router, 'send_digest_test', send)
    response = asyncio.run(digest_router.test_digest_email(user=user, db=db))
    assert response.status_code == 204
    send.assert_awaited_once_with(credentials=('sender@example.com', 'test-app-password'))


def test_standard_smtp_still_works_without_a_trial(configured_trial, monkeypatch):
    user, _, db, _ = configured_trial
    monkeypatch.setattr(digest_router.settings, 'EMAIL_USER', 'ordinary@example.com')
    monkeypatch.setattr(digest_router.settings, 'EMAIL_PASSWORD', 'ordinary-password')
    db.get.return_value = None
    send = AsyncMock()
    monkeypatch.setattr(digest_router, 'send_digest_test', send)
    output = asyncio.run(digest_router.get_digest_settings(db=db, user=user))
    assert output['emailConfigured'] is True
    asyncio.run(digest_router.test_digest_email(user=user, db=db))
    send.assert_awaited_once_with(credentials=('ordinary@example.com', 'ordinary-password'))


def test_trial_smtp_failure_returns_safe_error(configured_trial, monkeypatch):
    user, _, db, _ = configured_trial
    monkeypatch.setattr(digest_router, 'send_digest_test', AsyncMock(side_effect=RuntimeError('private credentials')))
    with pytest.raises(HTTPException) as error:
        asyncio.run(digest_router.test_digest_email(user=user, db=db))
    assert error.value.status_code == 502
    assert 'private credentials' not in error.value.detail


def test_test_template_passes_explicit_credentials_without_changing_recipient(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(digest_service, '_send_digest', send)
    credentials = ('sender@example.com', 'test-password')
    asyncio.run(digest_service.send_digest_test(credentials=credentials))
    send.assert_awaited_once()
    assert send.await_args.kwargs == {'credentials': credentials}


def test_explicit_test_credentials_reach_transport_and_fixed_recipient(monkeypatch):
    sent = AsyncMock()
    calls = []
    credentials = ('sender@example.com', 'test-password')

    def mailer(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(host='smtp.gmail.com', port=465, send_verified=sent)

    monkeypatch.setattr(digest_service, 'GmailService', mailer)
    monkeypatch.setattr(digest_service.settings, 'NEWS_EMAIL_RECIPIENT', 'recipient@example.com')
    asyncio.run(digest_service._send_digest('Test', 'Body', '<p>Body</p>', credentials=credentials))
    assert calls == [{'credentials': credentials}]
    sent.assert_awaited_once_with('Test', ['recipient@example.com'], 'Body', html='<p>Body</p>')
