"""Canonical URL identity must reject races without removing historical rows."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.auth import get_current_user, require_admin
from app.db import get_db
from app.intelligence import collection
from app.intelligence.models import NewsSource
from app.intelligence.router import router
from app.config import settings
from app.intelligence.models import NewsItem
from app.intelligence.schemas import NewsSourceCreate
from test_brightdata_postgres_integration import Provider, database, private_postgres


@pytest.mark.parametrize('url', [
    'http://linkedin.com/in/Example/?trk=feed#about',
    'https://www.linkedin.com/in/example',
    'https://www.linkedin.com/in/example/recent-activity/all/',
])
def test_personal_source_normalizes_safe_equivalent_urls(url):
    source = NewsSourceCreate(name='Example', type='LINKEDIN', url=url)
    assert source.url == 'https://www.linkedin.com/in/example/'


@pytest.mark.parametrize('url', ['https://linkedin.com:444/in/example',
    'https://linkedin.com/in/a%2Fb', 'https://evil.com/company/example',
    'https://linkedin.com/company/example/jobs/'])
def test_ambiguous_or_unsafe_source_urls_are_rejected(url):
    with pytest.raises(ValueError):
        NewsSourceCreate(name='Example', type='LINKEDIN', url=url)


def api(sessions):
    app = FastAPI()
    app.include_router(router, prefix='/intelligence')
    async def db_dependency():
        async with sessions() as db:
            yield db
    user = SimpleNamespace(id=uuid4(), role='ADMIN')
    app.dependency_overrides.update({get_db: db_dependency, get_current_user: lambda: user, require_admin: lambda: user})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://offline')


def test_concurrent_source_creation_and_edit_cannot_duplicate(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        async with api(sessions) as client:
            payload = {'name': 'Example', 'type': 'LINKEDIN', 'url': 'https://linkedin.com/company/Example?trk=test'}
            responses = await asyncio.gather(*(client.post('/intelligence/sources', json=payload) for _ in range(3)))
            assert sorted(r.status_code for r in responses) == [201, 409, 409]
            created = next(r.json() for r in responses if r.status_code == 201)
            payload['url'] = 'https://www.linkedin.com/company/second/'
            second = await client.post('/intelligence/sources', json=payload)
            payload['url'] = 'https://linkedin.com/company/example/'
            duplicate = await client.put('/intelligence/sources/' + second.json()['id'], json=payload)
            assert duplicate.status_code == 409
            assert 'already exists' in duplicate.json()['detail']
            # An equivalent URL edit must preserve pending executions and interval state.
            result = await client.put('/intelligence/sources/' + created['id'], json=payload)
            assert result.status_code == 200
        async with sessions() as db:
            assert len(list(await db.scalars(select(NewsSource)))) == 2
        assert provider.requests == []
    asyncio.run(run())


def test_historical_duplicate_sources_never_schedule_two_jobs(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        async with sessions() as db:
            db.add_all([
                NewsSource(name='Old one', type='LINKEDIN', url='https://linkedin.com/company/example/?trk=x'),
                NewsSource(name='Old duplicate', type='LINKEDIN', url='https://www.linkedin.com/company/example/'),
            ])
            await db.commit()
        await collection.check_due_sources()
        assert len(provider.triggers) == 1
        async with sessions() as db:
            assert len(list(await db.scalars(select(NewsSource)))) == 2
    asyncio.run(run())


def test_global_pause_blocks_direct_collection_entrypoint(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    monkeypatch.setattr(settings, 'NEWS_ENABLE_COLLECTION', False)
    async def run():
        async with sessions() as db:
            row = NewsSource(name='Paused', type='LINKEDIN', url='https://linkedin.com/company/paused/')
            db.add(row)
            await db.commit()
            assert await collection.protected_linkedin_check(db, row, datetime.now(timezone.utc)) == 'paused'
        assert provider.requests == []
    asyncio.run(run())


def test_invalid_partial_snapshot_keeps_watermark_and_reports_coverage(database, monkeypatch):
    from datetime import datetime, timezone
    _, sessions = database
    provider = Provider(monkeypatch, [
        {'id':'90001', 'url':'https://linkedin.com/feed/update/urn:li:activity:90001', 'post_text':'Valid', 'date_posted':datetime.now(timezone.utc).isoformat()},
        {'post_text':'Missing post link and identity'},
    ])
    async def run():
        async with sessions() as db:
            row = NewsSource(name='Partial', type='LINKEDIN', url='https://linkedin.com/company/partial/')
            db.add(row)
            await db.commit()
            sid = row.id
        async with sessions() as db:
            await collection.check_source(db, sid)
        async with sessions() as db:
            await collection.check_source(db, sid)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.last_successful_fetch_at is None
            assert row.linkedin_paused_reason and 'invalid' in row.linkedin_paused_reason.lower()
            assert len(list(await db.scalars(select(NewsItem)))) == 1
        assert len(provider.triggers) == 1
    asyncio.run(run())


@pytest.mark.parametrize('rename', [False, True])
def test_source_edit_during_reservation_cannot_deadlock_or_change_trigger(database, monkeypatch, rename):
    from app.intelligence import linkedin_collection
    _, sessions = database
    provider = Provider(monkeypatch)
    original_reserve = linkedin_collection.reserve_execution
    async def run():
        reached, resume = asyncio.Event(), asyncio.Event()
        async def reserve(*args, **kwargs):
            reached.set()
            await resume.wait()
            return await original_reserve(*args, **kwargs)
        monkeypatch.setattr(linkedin_collection, 'reserve_execution', reserve)
        async with api(sessions) as client:
            payload = {'name':'Race', 'type':'LINKEDIN', 'url':'https://linkedin.com/company/race/'}
            sid = (await client.post('/intelligence/sources', json=payload)).json()['id']
            async def collect():
                from uuid import UUID
                async with sessions() as db:
                    return await collection.check_source(db, UUID(sid))
            collecting = asyncio.create_task(collect())
            await asyncio.wait_for(reached.wait(), 10)
            payload['url'] = 'https://linkedin.com/company/renamed/' if rename else 'https://linkedin.com/company/race/?trk=edit'
            editing = asyncio.create_task(client.put('/intelligence/sources/'+sid, json=payload))
            await asyncio.sleep(.1)
            resume.set()
            state, response = await asyncio.wait_for(asyncio.gather(collecting, editing), 10)
            assert state == 'started'
            assert response.status_code == (409 if rename else 200)
            assert len(provider.triggers) == 1
            import json
            assert json.loads(provider.triggers[0].content)[0]['url'] == 'https://www.linkedin.com/company/race/'
    asyncio.run(run())


def test_source_delete_removes_posts_and_saved_state(database, monkeypatch):
    from test_brightdata_postgres_integration import records
    from app.intelligence.models import NewsUserState
    from sqlalchemy import text
    engine, sessions = database
    Provider(monkeypatch, records())
    async def run():
        async with api(sessions) as client:
            payload = {'name':'Preserve', 'type':'LINKEDIN', 'url':'https://linkedin.com/company/preserve/'}
            sid = (await client.post('/intelligence/sources', json=payload)).json()['id']
            from uuid import UUID
            for _ in range(2):
                async with sessions() as db:
                    await collection.check_source(db, UUID(sid))
            async with sessions() as db:
                item = (await db.scalars(select(NewsItem))).one()
                db.add(NewsUserState(user_id=uuid4(), news_item_id=item.id, saved_at=datetime.now(timezone.utc)))
                await db.commit()
            response = await client.delete('/intelligence/sources/'+sid)
            assert response.status_code == 204
            async with engine.begin() as conn:
                from app.intelligence.credit_migration import migrate_credit_controls
                for _ in range(2): await migrate_credit_controls(conn)
                for table in ('intelligence_items','intelligence_analyses','intelligence_user_states','intelligence_sources'):
                    assert await conn.scalar(text('SELECT count(*) FROM '+table)) == 0
                assert await conn.scalar(text('SELECT count(*) FROM intelligence_brightdata_executions')) == 1
    asyncio.run(run())


def test_completed_empty_source_rejects_identity_change_but_allows_deletion(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        async with api(sessions) as client:
            payload = {'name':'Keep latch', 'type':'LINKEDIN', 'url':'https://linkedin.com/company/keep-latch/'}
            sid = (await client.post('/intelligence/sources', json=payload)).json()['id']
            from uuid import UUID
            for _ in range(2):
                async with sessions() as db:
                    await collection.check_source(db, UUID(sid))
            payload['url'] = 'https://linkedin.com/company/new-identity/'
            assert (await client.put('/intelligence/sources/'+sid, json=payload)).status_code == 409
            assert (await client.delete('/intelligence/sources/'+sid)).status_code == 204
            assert len(provider.triggers) == 1
    asyncio.run(run())


def test_feed_users_can_discover_monitoring_sources_without_admin_configuration(database):
    from fastapi import HTTPException
    _, sessions = database
    async def run():
        async with sessions() as db:
            db.add(NewsSource(name='Visible', type='LINKEDIN', url='https://linkedin.com/in/visible/', ai_instructions='private editor instructions'))
            await db.commit()
        async with api(sessions) as client:
            def deny_admin(): raise HTTPException(status_code=403)
            client._transport.app.dependency_overrides[require_admin] = deny_admin
            assert (await client.get('/intelligence/sources')).status_code == 403
            response = await client.get('/intelligence/linkedin-sources')
            assert response.status_code == 200
            assert response.json()[0]['name'] == 'Visible'
            assert 'private editor instructions' not in response.text
            assert 'ai_instructions' not in response.json()[0]
    asyncio.run(run())


def test_delete_revalidates_type_after_concurrent_linkedin_conversion(database, monkeypatch):
    import importlib
    module = importlib.import_module('app.intelligence.router')
    _, sessions = database
    original_get = module._get_source
    provider = Provider(monkeypatch)
    async def run():
        read, resume = asyncio.Event(), asyncio.Event()
        calls = 0
        async def get_source(*args):
            nonlocal calls
            row = await original_get(*args)
            calls += 1
            if calls == 1:
                read.set()
                await resume.wait()
            return row
        async with api(sessions) as client:
            payload = {'name':'Converted', 'type':'OTHER', 'url':'https://example.org/'}
            sid = (await client.post('/intelligence/sources', json=payload)).json()['id']
            monkeypatch.setattr(module, '_get_source', get_source)
            deleting = asyncio.create_task(client.delete('/intelligence/sources/'+sid))
            await asyncio.wait_for(read.wait(), 10)
            payload.update(type='LINKEDIN',url='https://linkedin.com/company/converted/')
            assert (await client.put('/intelligence/sources/'+sid,json=payload)).status_code == 200
            from uuid import UUID
            async with sessions() as db:
                assert await collection.check_source(db, UUID(sid)) == 'started'
            resume.set()
            assert (await asyncio.wait_for(deleting,10)).status_code == 409
            async with sessions() as db:
                row = await db.get(NewsSource, UUID(sid))
                assert row is not None and row.pending_execution_id
            assert len(provider.triggers) == 1
    asyncio.run(run())
