"""Real PostgreSQL coverage for private accounts; no SMTP or provider requests."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Response
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select, text

from app import auth
from app.config import settings
from app.db import get_db, AppMetadata
from app.intelligence import router as feeds, digest_router, social_schedule
from app.intelligence.models import NewsSource, NewsItem, NewsAnalysis, NewsDigestSettings, NewsSourceEmailSubscription, NewsUserState, NewsDigestDelivery
from app.intelligence.collection_cycles import CollectionCycle, ScheduledDigestReport
from test_brightdata_postgres_integration import database, private_postgres


def test_unicode_member_login_unit():
    account = SimpleNamespace(id=uuid4(), email='unicode@example.com', active=True,
                              session_version=1, password_hash=auth.hash_password('pässword-long-123'))
    result = asyncio.run(auth.login(auth.LoginInput(email=account.email, password='pässword-long-123'),
                                   Response(), SimpleNamespace(scalar=AsyncMock(return_value=account))))
    assert result['id'] == str(account.id)


@asynccontextmanager
async def client(sessions):
    app = FastAPI()
    app.include_router(auth.router, prefix='/auth')
    app.include_router(feeds.router, prefix='/intelligence')
    app.include_router(digest_router.router, prefix='/intelligence')
    async def db_dependency():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = db_dependency
    # Match the production cookie path.
    app_with_api = FastAPI()
    app_with_api.mount('/api', app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with_api), base_url='http://offline/api/') as api:
        yield api


async def admin_login(api):
    result = await api.post('auth/login', json={'email': settings.NEWS_ADMIN_EMAIL, 'password': settings.NEWS_ADMIN_PASSWORD})
    assert result.status_code == 200
    assert result.json()['id'] == str(auth.ADMIN_ID)


async def add_member(api, email='member@example.com'):
    result = await api.post('auth/users', json={'email': email, 'password': 'member-password-123'})
    assert result.status_code == 201, result.text
    return result.json()


def test_report_archive_is_visible_only_to_report_owner(database):
    _, sessions = database

    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            user = await add_member(admin)
            login = await member.post('auth/login', json={'email': user['email'], 'password': 'member-password-123'})
            assert login.status_code == 200
            async with sessions() as db:
                source = NewsSource(name='Official archive', type='WEBSITE', url='https://example.org')
                db.add(source)
                await db.flush()
                article = NewsItem(source_id=source.id, title='Archived article', url='https://example.org/article')
                db.add(article)
                await db.flush()
                report = ScheduledDigestReport(cycle_id=uuid4(), user_id=auth.ADMIN_ID,
                    recipient_fingerprint='test', state='sent', content={'archive_ids': [str(article.id)]})
                db.add(report)
                await db.commit()
            url = f'intelligence/digest-archive/{report.id}'
            response = await admin.get(url)
            assert response.status_code == 200
            assert response.json() == [{'title': article.title, 'url': article.url, 'source': source.name}]
            assert (await member.get(url)).status_code == 404

    asyncio.run(run())


def test_member_login_and_admin_only_creation_and_disable(database):
    _, sessions = database
    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            created = await add_member(admin)
            assert created['role'] == 'MEMBER' and 'password' not in created
            duplicate = await admin.post('auth/users', json={'email': 'MEMBER@example.com', 'password': 'member-password-123'})
            assert duplicate.status_code == 409
            bad = await member.post('auth/login', json={'email': created['email'], 'password': 'incorrect'})
            assert bad.status_code == 401
            good = await member.post('auth/login', json={'email': 'MEMBER@example.com', 'password': 'member-password-123'})
            assert good.status_code == 200 and good.json()['role'] == 'MEMBER'
            assert (await member.get('auth/users')).status_code == 403
            assert (await member.post('auth/users', json={'email': 'intruder@example.com', 'password': 'member-password-123'})).status_code == 403
            assert (await member.get('intelligence/collection-executions')).status_code == 403
            assert (await admin.put('auth/users/' + created['id'], json={'active': False})).status_code == 200
            assert (await member.get('auth/me')).status_code == 401
            assert (await member.post('auth/login', json={'email': created['email'], 'password': 'member-password-123'})).status_code == 401
    asyncio.run(run())


def test_unicode_password_and_reset_revoke_old_sessions(database):
    _, sessions = database
    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            response = await admin.post('auth/users', json={'email': 'unicode@example.com', 'password': 'pässword-long-123'})
            assert response.status_code == 201
            account = response.json()
            login = await member.post('auth/login', json={'email': account['email'], 'password': 'pässword-long-123'})
            assert login.status_code == 200
            reset = await admin.put('auth/users/' + account['id'], json={'password': 'new-password-123'})
            assert reset.status_code == 200
            assert (await member.get('auth/me')).status_code == 401
            assert (await member.post('auth/login', json={'email': account['email'], 'password': 'pässword-long-123'})).status_code == 401
            assert (await member.post('auth/login', json={'email': account['email'], 'password': 'new-password-123'})).status_code == 200
    asyncio.run(run())


def test_sources_items_and_email_are_private(database, monkeypatch):
    _, sessions = database
    sent = []
    async def send(*args, **kwargs):
        sent.append(kwargs['recipient'])
    monkeypatch.setattr(feeds, 'send_news_email', send)
    monkeypatch.setattr(settings, 'OPENAI_API_KEY', 'offline-test')
    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            created = await add_member(admin)
            await member.post('auth/login', json={'email': created['email'], 'password': 'member-password-123'})
            payload = {'name': 'Personal news', 'type': 'RSS', 'url': 'https://example.org/feed'}
            a = await admin.post('intelligence/sources', json=payload)
            b = await member.post('intelligence/sources', json=payload)
            assert a.status_code == b.status_code == 201
            aid, bid = a.json()['id'], b.json()['id']
            assert [s['id'] for s in (await admin.get('intelligence/sources')).json()] == [aid]
            assert [s['id'] for s in (await member.get('intelligence/sources')).json()] == [bid]
            async with sessions() as db:
                item = NewsItem(source_id=a.json()['id'], title='Private admin article', url='https://example.org/a')
                db.add(item)
                await db.flush()
                db.add(NewsAnalysis(news_item_id=item.id, summary='Private summary', category='BUSINESS', importance_score=50, relevance_score=50))
                await db.commit()
                iid = str(item.id)
            assert (await member.get('intelligence/items')).json()['items'] == []
            assert len((await admin.get('intelligence/items')).json()['items']) == 1
            assert (await member.get('intelligence/items', params={'source_id': aid})).json()['items'] == []
            for verb, path, body in [
                ('put', f'sources/{aid}', payload), ('delete', f'sources/{aid}', None),
                ('post', f'sources/{aid}/check', None), ('put', f'sources/{aid}/email', {'enabled': True}),
                ('put', f'items/{iid}/read', None), ('delete', f'items/{iid}/read', None),
                ('put', f'items/{iid}/saved', None), ('delete', f'items/{iid}/saved', None),
                ('post', f'items/{iid}/email', None),
                ('post', f'items/{iid}/deep-analysis', None),
            ]:
                response = await member.request(verb, 'intelligence/' + path, **({'json': body} if body else {}))
                assert response.status_code == 404, (path, response.text)
            assert (await member.get('intelligence/brief')).json() is None
            assert (await admin.post(f'intelligence/items/{iid}/email')).status_code == 200
            assert sent == [settings.NEWS_EMAIL_RECIPIENT]
            async with sessions() as db:
                item = NewsItem(source_id=bid, title='Member article', url='https://example.org/member')
                db.add(item)
                await db.flush()
                db.add(NewsAnalysis(news_item_id=item.id, summary='Member summary', category='BUSINESS', importance_score=50, relevance_score=50))
                await db.commit()
                member_item = str(item.id)
            assert (await member.post(f'intelligence/items/{member_item}/email')).status_code == 200
            assert sent == [settings.NEWS_EMAIL_RECIPIENT, created['email']]
            assert (await member.get('intelligence/digest-settings')).json()['recipient'] == created['email']
            assert (await admin.get('intelligence/digest-settings')).json()['recipient'] == settings.NEWS_EMAIL_RECIPIENT
            for api in (admin, member):
                social = {'name': 'Same public account', 'type': 'LINKEDIN', 'url': 'https://linkedin.com/company/private-test/'}
                assert (await api.post('intelligence/sources', json=social)).status_code == 201
                assert (await api.post('intelligence/sources', json=social)).status_code == 409
    asyncio.run(run())


@pytest.mark.parametrize('kind,url', [('LINKEDIN', 'https://linkedin.com/company/shared-private/'), ('FACEBOOK', 'https://facebook.com/shared-private/')])
def test_two_members_independently_own_same_social_url(database, kind, url):
    _, sessions = database
    async def run():
        async with client(sessions) as admin, client(sessions) as a, client(sessions) as b:
            await admin_login(admin)
            first = await add_member(admin, 'first@example.com')
            second = await add_member(admin, 'second@example.com')
            for api, account in ((a, first), (b, second)):
                assert (await api.post('auth/login', json={'email': account['email'], 'password': 'member-password-123'})).status_code == 200
            payload = {'name': 'Personal monitor', 'type': kind, 'url': url}
            aid = (await a.post('intelligence/sources', json=payload)).json()['id']
            bid = (await b.post('intelligence/sources', json=payload)).json()['id']
            assert aid != bid
            assert [row['id'] for row in (await a.get('intelligence/sources')).json()] == [aid]
            assert [row['id'] for row in (await b.get('intelligence/sources')).json()] == [bid]
            assert (await a.delete('intelligence/sources/' + bid)).status_code == 404
            if kind == 'LINKEDIN':
                from app.intelligence.linkedin_identity import collection_owner
                async with sessions() as db:
                    for sid in (aid, bid):
                        assert await collection_owner(db, await db.get(NewsSource, UUID(sid)))
    asyncio.run(run())


def test_member_test_email_uses_own_address(database, monkeypatch):
    _, sessions = database
    monkeypatch.setattr(settings, 'EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'EMAIL_PASSWORD', 'offline')
    sender = AsyncMock()
    monkeypatch.setattr(digest_router, 'send_digest_test', sender)
    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            account = await add_member(admin)
            await member.post('auth/login', json={'email': account['email'], 'password': 'member-password-123'})
            assert (await member.post('intelligence/digest-settings/test')).status_code == 204
            assert sender.await_args.kwargs['recipient'] == account['email']
    asyncio.run(run())


def test_existing_quality_exclusions_stay_hidden_for_owned_items(database):
    _, sessions = database
    async def run():
        async with client(sessions) as admin:
            await admin_login(admin)
            async with sessions() as db:
                source = NewsSource(name='Existing filtered source', type='RSS', url='https://example.org/quality')
                db.add(source)
                await db.flush()
                item = NewsItem(source_id=source.id, title='Privacy policy', url='https://example.org/privacy')
                db.add(item)
                await db.flush()
                db.add(NewsAnalysis(news_item_id=item.id, summary='Excluded navigation page', category='BUSINESS', importance_score=50, relevance_score=50))
                db.add(AppMetadata(key='news-quality-excluded:'+item.id.hex, value='Previously excluded navigation content'))
                await db.commit()
            response = await admin.get('intelligence/items')
            assert response.status_code == 200
            assert response.json()['items'] == []
    asyncio.run(run())


def test_source_edit_uses_own_archived_identity(database):
    from app.intelligence.source_history import retain_collection_history
    _, sessions = database
    async def run():
        async with client(sessions) as admin, client(sessions) as member:
            await admin_login(admin)
            account = await add_member(admin)
            await member.post('auth/login', json={'email': account['email'], 'password': 'member-password-123'})
            async with sessions() as db:
                for owner, slug in ((auth.ADMIN_ID, 'admin-archive'), (UUID(account['id']), 'member-archive')):
                    source = NewsSource(owner_id=owner, name=slug, type='LINKEDIN', url=f'https://www.linkedin.com/company/{slug}/', last_started_at=datetime.now(timezone.utc))
                    db.add(source)
                    await db.flush()
                    await retain_collection_history(db, source)
                    await db.delete(source)
                await db.commit()
            payload = {'name': 'Fresh', 'type': 'WEBSITE', 'url': 'https://example.org/fresh'}
            sid = (await member.post('intelligence/sources', json=payload)).json()['id']
            payload.update(type='LINKEDIN', url='https://www.linkedin.com/company/member-archive/')
            assert (await member.put('intelligence/sources/'+sid, json=payload)).status_code == 409
            payload['url'] = 'https://www.linkedin.com/company/admin-archive/'
            assert (await member.put('intelligence/sources/'+sid, json=payload)).status_code == 200
    asyncio.run(run())


def test_legacy_source_migration_is_idempotent(database):
    engine, sessions = database
    async def run():
        async with sessions() as db:
            stamp = datetime(2026, 11, 15, 7, tzinfo=timezone.utc)
            source = NewsSource(name='Existing source', type='RSS', url='https://example.org/rss')
            db.add(source)
            await db.flush()
            item = NewsItem(source_id=source.id, title='Existing history', url='https://example.org/existing')
            db.add(item)
            await db.flush()
            db.add_all([
                NewsUserState(user_id=auth.ADMIN_ID, news_item_id=item.id, read_at=stamp, saved_at=stamp, emailed_at=stamp),
                NewsSourceEmailSubscription(source_id=source.id, user_id=auth.ADMIN_ID, enabled_at=stamp),
                NewsDigestDelivery(user_id=auth.ADMIN_ID, url_hash='legacy-receipt', sent_at=stamp),
                NewsDigestSettings(user_id=auth.ADMIN_ID, times=['07:00', '21:00'], last_slot_at=stamp, last_sent_at=stamp),
            ])
            await db.commit()
            sid, iid = source.id, item.id
        async with engine.begin() as connection:
            await connection.execute(text('ALTER TABLE intelligence_sources DROP COLUMN owner_id'))
            await auth.migrate_users(connection)
            await auth.migrate_users(connection)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.owner_id == auth.ADMIN_ID and row.name == 'Existing source'
            state = await db.get(NewsUserState, (auth.ADMIN_ID, iid))
            assert state.read_at == state.saved_at == state.emailed_at == stamp
            assert (await db.get(NewsSourceEmailSubscription, sid)).enabled_at == stamp
            assert (await db.get(NewsDigestDelivery, (auth.ADMIN_ID, 'legacy-receipt'))).sent_at == stamp
            digest = await db.get(NewsDigestSettings, auth.ADMIN_ID)
            assert digest.last_slot_at == digest.last_sent_at == stamp
            assert digest.times == ['07:00', '21:00']
    asyncio.run(run())


def test_scheduled_reports_use_personal_recipient_and_ignore_foreign_subscriptions(database, monkeypatch):
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_SCHEDULE_ENABLED', True)
    monkeypatch.setattr(settings, 'EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'EMAIL_PASSWORD', 'test')
    sent = []
    async def send(*args, **kwargs):
        sent.append((args, kwargs))
    monkeypatch.setattr(social_schedule, '_send_digest', send)
    # Even if GA attachments are configured, members must not receive them.
    monkeypatch.setattr(social_schedule, 'morning_enabled', lambda phase: True)
    async def forbidden(*args):
        raise AssertionError('Member requested administrator timetable')
    monkeypatch.setattr(social_schedule, 'morning_attachments', forbidden)
    async def run():
        async with client(sessions) as admin:
            await admin_login(admin)
            created = await add_member(admin)
        uid = UUID(created['id'])
        cutoff = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)
        now = cutoff + timedelta(minutes=1)
        async with sessions() as db:
            mine = NewsSource(owner_id=uid, name='Mine', type='RSS', url='https://example.org/mine')
            foreign = NewsSource(name='Private admin source', type='RSS', url='https://example.org/admin')
            db.add_all([mine, foreign])
            await db.flush()
            for source in (mine, foreign):
                db.add(NewsSourceEmailSubscription(source_id=source.id, user_id=uid, enabled_at=now-timedelta(days=1)))
                db.add(NewsItem(source_id=source.id, title=source.name, url=source.url+'/article', created_at=cutoff-timedelta(minutes=1), published_at=cutoff-timedelta(minutes=1), publication_time_verified=True))
            db.add(NewsDigestSettings(user_id=uid, times=['07:00', '21:00'], last_slot_at=now-timedelta(hours=1)))
            cycle = CollectionCycle(trial_date=now.date(), phase='scheduled-am', cutoff_at=cutoff, deadline_at=cutoff, state='finished', inventory_frozen=True)
            db.add(cycle)
            await db.commit()
            assert await social_schedule.process_scheduled_digest(db, cycle, uid, now) == 1
            assert len(sent) == 1 and sent[0][1]['recipient'] == created['email']
            assert 'Private admin source' not in sent[0][0][1]
            assert 'attachments' not in sent[0][1]
            account = await db.get(auth.MemberAccount, uid)
            account.active = False
            await db.commit()
            assert await social_schedule.process_scheduled_digest(db, cycle, uid, now) == 0
            assert len(sent) == 1
    asyncio.run(run())
