"""New-source API -> protected polling -> private DB -> stored feed, HTTP mocked."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.auth import get_current_user, require_admin
from app.config import settings
from app.db import get_db
from app.intelligence import collection
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsAnalysis, NewsItem, NewsSource
from app.intelligence.router import router
from test_brightdata_postgres_integration import Provider, database, private_postgres


@pytest.mark.parametrize('path,discovery', [('in', 'profile_url'), ('company', 'company_url')])
def test_new_source_to_persistent_feed_with_budget_and_daily_schedule(database, monkeypatch, path, discovery):
    _, sessions = database
    now = datetime.now(timezone.utc)
    published = (now - timedelta(days=1)).isoformat()
    url = f'https://www.linkedin.com/{path}/workflow-example/'
    rows = [
        {'id': '91001', 'url': 'https://www.linkedin.com/feed/update/urn:li:activity:91001',
         'user_id': 'workflow-example', 'post_text': 'Original workflow post', 'date_posted': published},
        {'id': '91002', 'url': 'https://www.linkedin.com/posts/original_activity-91002-x',
         'user_id': 'original', 'reposted_by': url, 'post_type': 'repost',
         'post_text': 'Shared workflow post', 'date_posted': published},
    ]
    provider = Provider(monkeypatch, [*rows, rows[0]])
    app = FastAPI()  # Router only: never imports/starts production lifespan.
    app.include_router(router, prefix='/api/intelligence')
    user = SimpleNamespace(id=uuid4(), role='ADMIN')

    async def db_dependency():
        async with sessions() as db:
            yield db
    app.dependency_overrides.update({get_db: db_dependency, get_current_user: lambda: user, require_admin: lambda: user})

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://offline') as client:
            response = await client.post('/api/intelligence/sources', json={
                'name': f'Workflow {path}', 'type': 'LINKEDIN', 'url': url, 'fetch_interval_minutes': 5})
            assert response.status_code == 201
            assert response.json()['fetch_interval_minutes'] == 1440
            sid = UUID(response.json()['id'])
            assert provider.requests == []  # Creating a source is not a scraping trigger.
            assert (await client.get('/api/intelligence/items', params={'source_id': str(sid)})).json()['items'] == []
            await collection.check_due_sources()  # First cycle reserves and triggers one mock job.
            assert len(provider.triggers) == 1
            trigger = provider.triggers[0]
            assert trigger.url.params['discover_by'] == discovery
            assert trigger.url.params['limit_per_input'] == '6'
            expected_input = {'url': url, 'start_date': (now - timedelta(days=1)).date().isoformat(), 'end_date': (now - timedelta(days=1)).date().isoformat()}
            if path == 'in': expected_input['only_authored_posts'] = False
            assert json.loads(trigger.content) == [expected_input]
            await collection.check_due_sources()  # Known snapshot is polled and persisted.
            await collection.check_due_sources()  # Daily interval blocks a new trigger.
            assert len(provider.triggers) == 1
            async with sessions() as db:
                source = await db.get(NewsSource, sid)
                assert source.last_successful_fetch_at is not None
                items = list((await db.scalars(select(NewsItem))).all())
                analyses = list((await db.scalars(select(NewsAnalysis))).all())
                execution = (await db.scalars(select(BrightDataExecution))).one()
                assert len(items) == len(analyses) == 2
                assert execution.returned_records == 3 and execution.newly_saved_posts == 2
                assert execution.duplicate_posts == 1 and execution.estimated_billable_usage == 6
                source.last_started_at -= timedelta(days=2)
                account = await db.get(BrightDataAccount, 1)
                account.provider_billable_records = 4000
                account.usage_as_of = account.verified_at = datetime.now(timezone.utc)
                await db.commit()
            await collection.check_due_sources()  # The completed date is not purchased again.
            assert len(provider.triggers) == 1
            monkeypatch.setattr(settings, 'NEWS_LINKEDIN_FETCH_ENABLED', False)
            for _ in range(3):
                feed = await client.get('/api/intelligence/items', params={'source_id': str(sid)})
                assert feed.status_code == 200 and len(feed.json()['items']) == 2
                assert {item['linkedin']['kind'] for item in feed.json()['items']} == {'POST', 'REPOST'}
            fixture = Path(__file__).resolve().parents[2] / 'tmp' / f'linkedin-source-feed-{path}.json'
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps(feed.json(), indent=2), encoding='utf-8')
            reposts = await client.get('/api/intelligence/items', params={'source_id': str(sid), 'activity_kind': 'REPOST'})
            assert len(reposts.json()['items']) == 1
            assert (await client.post(f'/api/intelligence/sources/{sid}/check')).status_code == 409
            assert len(provider.triggers) == 1
    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
def test_app_registers_collection_job_only_when_enabled(monkeypatch, enabled):
    from app import main
    jobs = []

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(run_sync=AsyncMock(), execute=AsyncMock())

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    class Scheduler:
        running = False
        def __init__(self, **kwargs):
            assert kwargs['timezone'] == 'UTC'
        def add_job(self, function, trigger, **kwargs):
            jobs.append((function, trigger, kwargs))
        def start(self): self.running = True
        def shutdown(self, **kwargs): self.running = False

    monkeypatch.setattr(main, 'engine', SimpleNamespace(begin=transaction, dispose=AsyncMock()))
    monkeypatch.setattr(main, 'SessionLocal', session)
    monkeypatch.setattr(main, 'AsyncIOScheduler', Scheduler)
    monkeypatch.setattr(main, 'ensure_company_sources', AsyncMock())
    monkeypatch.setattr(main, 'ensure_personal_activity_source', AsyncMock())
    monkeypatch.setattr(main, 'migrate_credit_controls', AsyncMock())
    monkeypatch.setattr(settings, 'NEWS_ENABLE_COLLECTION', enabled)
    monkeypatch.setattr(settings, 'NEWS_AUTO_IMPORT_SNAPSHOT', False)

    async def run():
        async with main.lifespan(main.app):
            collectors = [job for job in jobs if job[2].get('id') == 'source-collection']
            assert len(collectors) == int(enabled)
            if collectors:
                function, trigger, options = collectors[0]
                assert function is main.check_due_sources and trigger == 'interval'
                assert options['minutes'] == 5 and options['max_instances'] == 1 and options['coalesce']
    asyncio.run(run())
