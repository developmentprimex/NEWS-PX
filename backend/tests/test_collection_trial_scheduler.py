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


@pytest.fixture(autouse=True)
def mocked_billing_boundary(monkeypatch):
    from app.intelligence import cycle_service
    async def observed(*args): return True
    monkeypatch.setattr(cycle_service, 'capture_phase_usage', observed)


def test_manual_phase_and_evening_worker_reuse_checkpoints_across_restart(database, monkeypatch):
    from app.intelligence import cycle_service as service
    from app.intelligence.collection_cycles import CollectionCycle
    _, sessions = database
    provider = Provider(monkeypatch)
    morning = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    user = uuid4()
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': morning.date().isoformat(), 'user_id': str(user)})))
            account = await db.get(BrightDataAccount, 1)
            account.verified_at = account.usage_as_of = morning-timedelta(minutes=1)
            await db.commit()
            a = await service.accept_manual_cycle(db, user_id=user, now=morning)
        async with sessions() as db:
            b = await service.accept_manual_cycle(db, user_id=user, now=morning+timedelta(hours=1))
            assert a['cycle_id'] == b['cycle_id']
        await service.run_collection_trial_cycle(morning)
        await service.run_collection_trial_cycle(morning+timedelta(minutes=1))
        await service.run_collection_trial_cycle(morning+timedelta(minutes=2))
        assert len(provider.triggers) == 1
        evening = morning.replace(hour=20)  # At/after 21:00 in either DST offset.
        await service.run_collection_trial_cycle(evening)
        await service.run_collection_trial_cycle(evening+timedelta(minutes=1))
        async with sessions() as db:
            phases = list(await db.scalars(select(CollectionCycle)))
            assert {p.phase for p in phases} == {'manual', 'evening'}
            cycle = next(p for p in phases if p.phase == 'evening')
            row = await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.cycle_id == cycle.id))
            assert row.window_start == morning and row.coverage_confirmed
            assert await service.latest_verified_checkpoint(db, sid) == cycle.cutoff_at
        assert len(provider.triggers) == 2
        await service.run_collection_trial_cycle(evening+timedelta(days=1))
        assert len(provider.triggers) == 2
    asyncio.run(run())


@pytest.mark.parametrize('change', ['pause', 'identity', 'delete'])
def test_frozen_source_cannot_change_target_before_worker(database, monkeypatch, change):
    from app.intelligence import cycle_service as service
    _, sessions = database
    provider, user = Provider(monkeypatch), uuid4()
    now = datetime.now(timezone.utc).replace(hour=6)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now.date().isoformat(), 'user_id': str(user)})))
            await db.commit()
            await service.accept_manual_cycle(db, user_id=user, now=now)
            src = await db.get(NewsSource, sid)
            if change == 'pause':
                src.status = 'PAUSED'
            elif change == 'identity':
                src.url = 'https://www.linkedin.com/company/different/'
            else:
                await db.delete(src)
            await db.commit()
        await service.run_collection_trial_cycle(now)
        async with sessions() as db:
            row = (await db.scalars(select(CollectionCycleSource))).one()
            assert row.state == 'blocked' and not row.coverage_confirmed
        assert not provider.triggers
    asyncio.run(run())


def test_evening_poll_cannot_lose_completed_morning_checkpoint(database, monkeypatch):
    from app.intelligence import cycle_service as service
    from app.intelligence.collection_cycles import CollectionCycle
    from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter
    _, sessions = database
    provider, user = Provider(monkeypatch), uuid4()
    now = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    statuses = iter(['running', 'ready'])
    async def progress(_adapter, _snapshot): return next(statuses, 'ready')
    monkeypatch.setattr(BrightDataLinkedInAdapter, 'progress', progress)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now.date().isoformat(), 'user_id': str(user)})))
            account = await db.get(BrightDataAccount, 1)
            account.verified_at = account.usage_as_of = now-timedelta(minutes=1)
            await db.commit()
            await service.accept_manual_cycle(db, user_id=user, now=now)
        await service.run_collection_trial_cycle(now)  # Morning reserved/running.
        evening = now.replace(hour=20)
        await service.run_collection_trial_cycle(evening)  # Morning pending, evening's extra poll completes it.
        await service.run_collection_trial_cycle(evening+timedelta(minutes=1))
        async with sessions() as db:
            manual = await db.scalar(select(CollectionCycle).where(CollectionCycle.phase == 'manual'))
            morning_row = await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.cycle_id == manual.id))
            assert morning_row.coverage_confirmed and morning_row.completed_at
            pm = await db.scalar(select(CollectionCycle).where(CollectionCycle.phase == 'evening'))
            pm_row = await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.cycle_id == pm.id))
            assert pm_row.window_start == now
        assert len(provider.triggers) == 2
    asyncio.run(run())


def test_failed_before_phase_billing_does_not_disable_pending_poll_or_deadline_report(database, monkeypatch):
    from app.intelligence import cycle_service as service, cycle_digest
    _, sessions = database
    provider, user = Provider(monkeypatch), uuid4()
    now = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    ready = {'value': True}
    async def billing(*args): return ready['value']
    monkeypatch.setattr(service, 'capture_phase_usage', billing)
    reports = []
    async def report(db, cid, stamp): reports.append((cid, stamp)); return 0
    monkeypatch.setattr(cycle_digest, 'process_trial_digest', report)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now.date().isoformat(), 'user_id': str(user)})))
            account = await db.get(BrightDataAccount, 1)
            account.verified_at = account.usage_as_of = now-timedelta(minutes=1)
            await db.commit()
            await service.accept_manual_cycle(db, user_id=user, now=now)
        await service.run_collection_trial_cycle(now)
        ready['value'] = False
        evening = now.replace(hour=20)
        await service.run_collection_trial_cycle(evening)
        async with sessions() as db:
            assert (await db.scalars(select(BrightDataExecution))).one().state == 'completed'
        assert reports, 'Billing failure must not suppress the evening report/deadline evaluation'
        assert len(provider.triggers) == 1, 'No fresh billable POST without phase billing evidence'
    asyncio.run(run())


def test_actual_clock_is_rechecked_after_worker_crosses_midnight(database, monkeypatch):
    from app.intelligence import cycle_service as service
    _, sessions = database
    provider, user = Provider(monkeypatch), uuid4()
    now = datetime.now(timezone.utc).replace(hour=21, minute=59, second=0, microsecond=0)
    # The local date ends one minute later during summer; use the zone explicitly.
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo('Europe/Budapest')).replace(hour=23, minute=59)
    now = local.astimezone(timezone.utc)
    current = {'value': now}
    original = service.dispatch_cycle_source
    async def dispatch(db, row_id, stamp, **kwargs):
        current['value'] = now+timedelta(minutes=2)
        return await original(db, row_id, stamp, **kwargs)
    monkeypatch.setattr(service, 'dispatch_cycle_source', dispatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': local.date().isoformat(), 'user_id': str(user)})))
            account = await db.get(BrightDataAccount, 1)
            account.verified_at = account.usage_as_of = now-timedelta(minutes=1)
            await db.commit()
        await service.run_collection_trial_cycle(now, clock=lambda: current['value'])
        assert not provider.triggers
    asyncio.run(run())


def test_non_linkedin_check_revalidates_frozen_url_under_lock(database, monkeypatch):
    from app.intelligence import cycle_service as service
    _, sessions = database
    user, now = uuid4(), datetime.now(timezone.utc).replace(hour=6)
    checked = []
    async def collect(adapter): checked.append(adapter.source_url); return []
    monkeypatch.setattr(collection.RSSAdapter, 'collect', collect)
    original = collection.check_source
    async def edit_then_check(db, sid, **kwargs):
        async with sessions() as other:
            src = await other.get(NewsSource, sid)
            src.url = 'https://example.com/changed-feed'
            await other.commit()
        return await original(db, sid, **kwargs)
    monkeypatch.setattr(collection, 'check_source', edit_then_check)
    async def run():
        async with sessions() as db:
            src = NewsSource(name='RSS', url='https://example.com/feed', type='RSS', status='ACTIVE')
            db.add_all([src, AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now.date().isoformat(), 'user_id': str(user)}))])
            await db.commit()
            await service.accept_manual_cycle(db, user_id=user, now=now)
        await service.run_collection_trial_cycle(now)
        assert not checked
        async with sessions() as db:
            row = (await db.scalars(select(CollectionCycleSource))).one()
            assert row.state == 'blocked' and not row.coverage_confirmed
    asyncio.run(run())
