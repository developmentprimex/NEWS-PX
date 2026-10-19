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
