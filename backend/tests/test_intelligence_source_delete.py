"""Source deletion removes owned content without erasing provider accounting."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import require_admin
from app.intelligence import collection, collection_cycles
from app.intelligence.credit_control import budget_status
from app.intelligence.models import (
    BrightDataExecution, LinkedInSourceIdentity, NewsAnalysis, NewsItem,
    NewsSource, NewsSourceEmailSubscription, NewsUserState,
)
from test_brightdata_postgres_integration import Provider, database, private_postgres
from test_linkedin_source_identity import api


@pytest.mark.parametrize('kind,url', [
    ('WEBSITE', 'https://example.org/news'),
    ('RSS', 'https://example.org/feed.xml'),
    ('LINKEDIN', 'https://www.linkedin.com/company/delete-example/'),
    ('FACEBOOK', 'https://www.facebook.com/delete-example/'),
])
def test_delete_collected_source_removes_owned_content_and_keeps_budget(database, kind, url):
    _, sessions = database

    async def run():
        now = datetime.now(timezone.utc)
        async with sessions() as db:
            source = NewsSource(name='Delete example', type=kind, url=url,
                                last_started_at=now, last_successful_fetch_at=now)
            other = NewsSource(name='Keep example', type='WEBSITE', url='https://example.org/keep')
            db.add_all([source, other])
            await db.flush()
            item = NewsItem(source_id=source.id, title='Owned article', url=url + '/article')
            kept = NewsItem(source_id=other.id, title='Keep article', url=other.url + '/article')
            db.add_all([item, kept])
            await db.flush()
            db.add_all([
                NewsAnalysis(news_item_id=item.id, summary='Summary', category='BUSINESS',
                             importance_score=50, relevance_score=50),
                NewsUserState(user_id=uuid4(), news_item_id=item.id, saved_at=now, read_at=now),
                NewsSourceEmailSubscription(source_id=source.id, user_id=uuid4()),
            ])
            if kind == 'LINKEDIN':
                db.add(LinkedInSourceIdentity(canonical_url=url, source_id=source.id))
            execution = BrightDataExecution(source_id=source.id, started_at=now, completed_at=now,
                budget_month=now.strftime('%Y-%m'), trigger_reason='scheduled-day', endpoint='mock',
                dataset_id='mock', start_date=now.date(), end_date=now.date(), record_limit=6,
                estimated_billable_usage=6, state='completed')
            db.add(execution)
            await db.commit()
            before = await budget_status(db)
            sid, item_id, kept_id, other_id, execution_id = source.id, item.id, kept.id, other.id, execution.id
        async with api(sessions) as client:
            response = await client.delete('/intelligence/sources/' + str(sid))
            assert response.status_code == 204, response.text
            assert response.content == b''
            assert str(sid) not in {row['id'] for row in (await client.get('/intelligence/sources')).json()}
            assert (await client.delete('/intelligence/sources/' + str(sid))).status_code == 404
        async with sessions() as db:
            assert await db.get(NewsSource, sid) is None
            assert await db.get(NewsItem, item_id) is None
            assert await db.get(NewsAnalysis, item_id) is None
            assert not list(await db.scalars(select(NewsUserState)))
            assert await db.get(NewsSourceEmailSubscription, sid) is None
            assert not list(await db.scalars(select(LinkedInSourceIdentity)))
            assert await db.get(NewsSource, other_id) is not None
            assert await db.get(NewsItem, kept_id) is not None
            assert (await db.get(BrightDataExecution, execution_id)).estimated_billable_usage == 6
            after = await budget_status(db)
            assert after['effectiveRecords'] == before['effectiveRecords']
    asyncio.run(run())


@pytest.mark.parametrize('pending', ['execution', 'snapshot', 'ledger'])
def test_delete_waits_for_pending_collection(database, pending):
    _, sessions = database

    async def run():
        async with sessions() as db:
            source = NewsSource(name='Pending', type='LINKEDIN', url='https://linkedin.com/company/pending/')
            if pending == 'execution':
                source.pending_execution_id = uuid4()
            elif pending == 'snapshot':
                source.pending_snapshot_id = 'sd_pending'
            db.add(source)
            await db.flush()
            if pending == 'ledger':
                now = datetime.now(timezone.utc)
                db.add(BrightDataExecution(source_id=source.id, started_at=now,
                    budget_month=now.strftime('%Y-%m'), trigger_reason='scheduled-day', endpoint='mock',
                    dataset_id='mock', start_date=now.date(), end_date=now.date(), record_limit=6,
                    estimated_billable_usage=6, state='uncertain'))
            await db.commit()
            sid = source.id
        async with api(sessions) as client:
            response = await client.delete('/intelligence/sources/' + str(sid))
            assert response.status_code == 409
            assert 'collection' in response.json()['detail'].lower()
        async with sessions() as db:
            assert await db.get(NewsSource, sid) is not None
    asyncio.run(run())


def test_delete_requires_admin(database):
    _, sessions = database

    async def run():
        async with sessions() as db:
            source = NewsSource(name='Keep', type='WEBSITE', url='https://example.org/')
            db.add(source)
            await db.commit()
            sid = source.id
        async with api(sessions) as client:
            def deny_admin():
                raise HTTPException(status_code=403)
            client._transport.app.dependency_overrides[require_admin] = deny_admin
            assert (await client.delete('/intelligence/sources/' + str(sid))).status_code == 403
        async with sessions() as db:
            assert await db.get(NewsSource, sid) is not None
    asyncio.run(run())


@pytest.mark.parametrize('kind,url', [
    ('LINKEDIN', 'https://www.linkedin.com/company/history-example/'),
    ('FACEBOOK', 'https://www.facebook.com/history-example/'),
])
def test_readding_deleted_source_retains_collection_identity_and_limits(database, monkeypatch, kind, url):
    _, sessions = database
    provider = Provider(monkeypatch)

    async def run():
        now = datetime.now(timezone.utc)
        async with sessions() as db:
            source = NewsSource(name='Original', type=kind, url=url, last_started_at=now,
                last_successful_fetch_at=now, linkedin_paused_reason='Saturated window requires reconciliation.',
                empty_fetch_streak=2, last_new_posts=6)
            db.add(source)
            await db.flush()
            sid = source.id
            db.add(BrightDataExecution(source_id=sid, started_at=now, completed_at=now,
                budget_month=now.strftime('%Y-%m'), trigger_reason='scheduled-day', endpoint='mock',
                dataset_id='mock', start_date=now.date(), end_date=now.date(), record_limit=6,
                estimated_billable_usage=6, state='completed'))
            await db.commit()
        async with api(sessions) as client:
            assert (await client.delete('/intelligence/sources/' + str(sid))).status_code == 204
            temporary = (await client.post('/intelligence/sources', json={
                'name': 'Temporary', 'type': 'WEBSITE', 'url': 'https://example.org/temporary',
            })).json()
            converted = await client.put('/intelligence/sources/' + temporary['id'], json={
                'name': 'Bypass attempt', 'type': kind, 'url': url,
            })
            assert converted.status_code == 409
            assert 'Add source' in converted.json()['detail']
            result = await client.post('/intelligence/sources', json={'name': 'Re-added', 'type': kind, 'url': url})
            assert result.status_code == 201, result.text
            assert result.json()['id'] == str(sid)
        async with sessions() as db:
            restored = await db.get(NewsSource, sid)
            assert restored.last_started_at == now
            assert restored.last_successful_fetch_at == now
            assert restored.linkedin_paused_reason == 'Saturated window requires reconciliation.'
            assert restored.empty_fetch_streak == 2
            assert restored.last_new_posts == 6
            from app.intelligence.cycle_service import latest_verified_checkpoint
            assert await latest_verified_checkpoint(db, sid) is not None
            if kind == 'LINKEDIN':
                await collection.check_source(db, sid)
        assert provider.triggers == []
    asyncio.run(run())


def test_existing_duplicate_cannot_reset_deleted_owners_collection_history(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)

    async def run():
        url = 'https://www.linkedin.com/company/duplicate-history/'
        async with sessions() as db:
            owner = NewsSource(name='Collected owner', type='LINKEDIN', url=url,
                last_started_at=datetime.now(timezone.utc))
            duplicate = NewsSource(name='Historical duplicate', type='LINKEDIN', url=url)
            db.add_all([owner, duplicate])
            await db.flush()
            db.add(LinkedInSourceIdentity(canonical_url=url, source_id=owner.id))
            await db.commit()
            owner_id, duplicate_id = owner.id, duplicate.id
        async with api(sessions) as client:
            assert (await client.delete('/intelligence/sources/' + str(owner_id))).status_code == 204
        async with sessions() as db:
            assert await collection.check_source(db, duplicate_id) == 'paused'
            assert 'previous collection identity' in (await db.get(NewsSource, duplicate_id)).last_error
        async with api(sessions) as client:
            assert (await client.delete('/intelligence/sources/' + str(duplicate_id))).status_code == 204
            restored = await client.post('/intelligence/sources', json={'name': 'Restored', 'type': 'LINKEDIN', 'url': url})
            assert restored.status_code == 201
            assert restored.json()['id'] == str(owner_id)
        assert provider.triggers == []
    asyncio.run(run())
