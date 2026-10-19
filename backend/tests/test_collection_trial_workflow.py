import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.db import AppMetadata
from app.intelligence import collection
from app.intelligence.collection_cycles import CollectionCycleSource, TemporalWindow, provider_dates, confirmed_activity_time
from app.intelligence.cycle_service import TRIAL_KEY, ensure_cycle
from app.intelligence.models import NewsSource, BrightDataExecution, BrightDataAccount
from app.intelligence.credit_control import reserve_execution
from test_brightdata_postgres_integration import Provider, database, private_postgres, source, records


def test_provider_dates_enclose_precise_utc_interval():
    now = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    assert tuple(str(d) for d in provider_dates(TemporalWindow(now, now+timedelta(hours=13), True))) == ('2026-10-18', '2026-10-20')
    assert confirmed_activity_time(SimpleNamespace(published_at=now, linkedin_data={'kind': 'REPOST'})) is None
    assert confirmed_activity_time(SimpleNamespace(published_at=now, linkedin_data={'kind': 'POST'})) == now


async def prepare(sessions, now, phase='manual'):
    sid = await source(sessions, last_started_at=now)
    async with sessions() as db:
        db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now.date().isoformat()})))
        await db.commit()
        cycle = await ensure_cycle(db, phase=phase, now=now)
        row = CollectionCycleSource(cycle_id=cycle.id, source_id=sid, name='Example', type='LINKEDIN',
            canonical_url='https://www.linkedin.com/company/example/', window_start=now-timedelta(hours=12),
            window_end=now, verified_start=True)
        db.add(row)
        await db.commit()
        return sid, row.id


def test_trial_reservation_is_bound_before_post_and_survives_repeated_calls(database, monkeypatch):
    _, sessions = database
    now = datetime.now(timezone.utc)
    provider = Provider(monkeypatch, records=records())
    async def run():
        sid, row_id = await prepare(sessions, now)
        async with sessions() as db:
            src = await db.get(NewsSource, sid)
            assert await collection.protected_linkedin_check(db, src, now, cycle_source_id=row_id) == 'started'
        async with sessions() as db:
            row = await db.get(CollectionCycleSource, row_id)
            assert row.execution_id is not None
            execution = await db.get(BrightDataExecution, row.execution_id)
            assert execution.trigger_reason.startswith('trial:manual:')
            src = await db.get(NewsSource, sid)
            assert await collection.protected_linkedin_check(db, src, now, cycle_source_id=row_id) == 'completed'
        async with sessions() as db:
            src = await db.get(NewsSource, sid)
            await collection.protected_linkedin_check(db, src, now, cycle_source_id=row_id)
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_expired_trial_cannot_start_a_billable_request(database, monkeypatch):
    _, sessions = database
    now = datetime.now(timezone.utc)
    provider = Provider(monkeypatch)
    async def run():
        sid, row_id = await prepare(sessions, now-timedelta(days=1))
        async with sessions() as db:
            src = await db.get(NewsSource, sid)
            assert await collection.protected_linkedin_check(db, src, now, cycle_source_id=row_id) == 'paused'
        assert not provider.triggers
    asyncio.run(run())


def test_crash_after_reservation_never_reposts(database, monkeypatch):
    _, sessions = database
    now, provider = datetime.now(timezone.utc), Provider(monkeypatch)
    async def run():
        sid, row_id = await prepare(sessions, now)
        async with sessions() as db:
            src = await db.get(NewsSource, sid)
            await reserve_execution(db, src, now, now.date()-timedelta(days=1), now.date()+timedelta(days=1),
                                    reason='trial:manual:test-crash', cycle_source_id=row_id)
        async with sessions() as db:
            assert await collection.protected_linkedin_check(db, await db.get(NewsSource, sid), now,
                                                             cycle_source_id=row_id) == 'paused'
            execution = await db.get(BrightDataExecution, (await db.get(CollectionCycleSource, row_id)).execution_id)
            assert execution.state == 'uncertain'
        assert not provider.triggers
    asyncio.run(run())


@pytest.mark.parametrize('count', [6, 7])
def test_trial_saturation_never_confirms_coverage(database, monkeypatch, count):
    _, sessions = database
    now, provider = datetime.now(timezone.utc), Provider(monkeypatch, records=records(count))
    async def run():
        sid, row_id = await prepare(sessions, now)
        for _ in range(3):
            async with sessions() as db:
                await collection.protected_linkedin_check(db, await db.get(NewsSource, sid), now, cycle_source_id=row_id)
        async with sessions() as db:
            row = await db.get(CollectionCycleSource, row_id)
            assert row.state == 'partial' and not row.coverage_confirmed
            assert (await db.get(NewsSource, sid)).linkedin_paused_reason
        assert len(provider.triggers) == 1
    asyncio.run(run())

