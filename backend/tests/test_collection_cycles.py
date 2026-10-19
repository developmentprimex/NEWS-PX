import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import AppMetadata
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
from app.intelligence.cycle_service import (
    TRIAL_KEY, ensure_cycle, latest_verified_checkpoint, trial_date_for_activation, trial_active,
)
from app.intelligence.models import BrightDataExecution
from test_brightdata_postgres_integration import database, private_postgres


def stamp(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize('value,want', [
    ('2026-10-19T20:59:00+02:00', date(2026, 10, 19)),
    ('2026-10-19T21:00:00+02:00', date(2026, 10, 20)),
    ('2026-03-29T20:59:00+02:00', date(2026, 3, 29)),
    ('2026-10-25T21:00:00+01:00', date(2026, 10, 26)),
])
def test_activation_date_uses_local_21_cutoff(value, want):
    assert trial_date_for_activation(stamp(value)) == want


def test_expiry_does_not_extend_trial():
    config = {'enabled': True, 'date': '2026-10-19'}
    assert trial_active(config, stamp('2026-10-19T23:59:59+02:00'))
    assert not trial_active(config, stamp('2026-10-20T00:00:00+02:00'))
    assert not trial_active({}, stamp('2026-10-19T08:00:00+02:00'))


def test_phase_idempotency_restart_and_fixed_evening_cutoff(database):
    _, sessions = database
    async def run():
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': '2026-10-19'})))
            await db.commit()
        async def accept():
            async with sessions() as db:
                cycle = await ensure_cycle(db, phase='manual', now=stamp('2026-10-19T08:00:00+02:00'))
                return cycle.id
        ids = await asyncio.gather(accept(), accept())
        assert ids[0] == ids[1]
        async with sessions() as db:
            again = await ensure_cycle(db, phase='manual', now=stamp('2026-10-19T09:00:00+02:00'))
            assert again.id == ids[0]
            assert again.cutoff_at == stamp('2026-10-19T06:00:00+00:00')
            evening = await ensure_cycle(db, phase='evening', now=stamp('2026-10-19T21:05:00+02:00'))
            assert evening.cutoff_at == stamp('2026-10-19T19:00:00+00:00')
            assert evening.deadline_at == evening.cutoff_at + timedelta(minutes=30)
        async with sessions() as db:
            assert len(list(await db.scalars(select(CollectionCycle)))) == 2
            with pytest.raises(ValueError):
                await ensure_cycle(db, phase='manual', now=stamp('2026-10-20T08:00:00+02:00'))
    asyncio.run(run())


def test_only_completed_daily_ledger_proves_legacy_coverage(database):
    _, sessions = database
    async def run():
        sid, now = uuid4(), stamp('2026-10-19T06:00:00+00:00')
        async with sessions() as db:
            for reason, state, day in [('scheduled-day', 'completed', 17), ('scheduled-day', 'partial', 18),
                                       ('scheduled', 'completed', 19)]:
                db.add(BrightDataExecution(source_id=sid, started_at=now, budget_month='2026-10',
                    endpoint='mock', dataset_id='mock', start_date=date(2026, 10, day), end_date=date(2026, 10, day),
                    trigger_reason=reason, state=state, record_limit=6, estimated_billable_usage=6))
            await db.commit()
            assert await latest_verified_checkpoint(db, sid) == stamp('2026-10-18T00:00:00+00:00')
    asyncio.run(run())


def test_checkpoint_rejects_partial_and_legacy_clock_guesses(database):
    _, sessions = database
    async def run():
        source = uuid4()
        cutoff = stamp('2026-10-19T06:00:00+00:00')
        async with sessions() as db:
            cycle = CollectionCycle(trial_date=date(2026, 10, 19), phase='manual', cutoff_at=cutoff,
                                    deadline_at=cutoff + timedelta(minutes=30))
            db.add(cycle)
            await db.flush()
            row = CollectionCycleSource(cycle_id=cycle.id, source_id=source, name='Test', type='RSS',
                canonical_url='https://example.com/feed', window_start=cutoff-timedelta(days=1),
                window_end=cutoff, verified_start=False, state='partial', coverage_confirmed=False)
            db.add(row)
            await db.commit()
            assert await latest_verified_checkpoint(db, source) is None
            row.state, row.coverage_confirmed = 'completed', True
            await db.commit()
            assert await latest_verified_checkpoint(db, source) == cutoff
            row.state = 'uncertain'
            await db.commit()
            assert await latest_verified_checkpoint(db, source) is None
    asyncio.run(run())


@pytest.mark.parametrize('now,utc_hour', [('2026-03-29T21:05:00+02:00', 19), ('2026-10-25T21:05:00+01:00', 20)])
def test_evening_slot_tracks_dst_and_additive_tables_survive_restart(database, now, utc_hour):
    from app.db import Base
    engine, sessions = database
    async def run():
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': now[:10]})))
            await db.commit()
            cycle = await ensure_cycle(db, phase='evening', now=stamp(now))
            cid = cycle.id
            assert cycle.cutoff_at.hour == utc_hour
        for _ in range(2):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            assert (await db.get(CollectionCycle, cid)).id == cid
            assert (await db.get(AppMetadata, TRIAL_KEY)).value
    asyncio.run(run())
