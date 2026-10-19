import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import AppMetadata
from app.intelligence import trial_usage as service
from app.intelligence.cycle_service import TRIAL_KEY
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
from app.intelligence.models import BrightDataExecution
from test_brightdata_postgres_integration import database, private_postgres

NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)


def fixture():
    binding = {'accountId': 'test-account', 'monthlyAllowance': 5000, 'pricingPlanId': 'test-plan',
               'usageResource': 'test-resource', 'credentialSha256': hashlib.sha256(b'test-token').hexdigest(),
               'consumersIsolated': True}
    daily = [0]*18+[1]
    usage = {'customer_id': 'test-account', 'from': '2026-10-01T00:00:00.000Z', 'to': '2026-10-20T00:00:00.000Z',
             'last_update_ts': NOW.isoformat(), 'data': {'test-resource': {'rows_initial_billable': daily}},
             'sums': {'test-resource': {k: {'rows_initial_billable': 1} for k in ['back_m0', 'custom']}}, 'snapshots': {}}
    responses = {'status': {'customer': 'test-account', 'status': 'active'},
        'balance': {'balance': 2, 'pending_costs': 0},
        'pricing': {'plans': [{'is_current': True, 'id': 'test-plan',
                             'product_prices': {'data_api': {'price_groups': [{'prices': {'record': 1.5}}]}}}]},
        'usage': {'test-account': usage}}
    return binding, responses


def test_billing_reads_only_gets_and_never_sums_overlapping_totals(monkeypatch):
    binding, responses = fixture()
    requests = []
    async def respond(request):
        requests.append(request)
        name = {'/status': 'status', '/customer/balance': 'balance', '/pricing_plans': 'pricing', '/customer/bw': 'usage'}[request.url.path]
        return httpx.Response(200, json=responses[name])
    monkeypatch.setattr(service, 'billing_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    evidence = asyncio.run(service.read_billing_evidence('test-token', binding, NOW))
    assert evidence['billable_records'] == 1
    assert len(requests) == 4 and all(r.method == 'GET' for r in requests)


@pytest.mark.parametrize('change', ['stale', 'identity', 'plan', 'pending', 'shared', 'totals', 'live', 'boolean', 'key'])
def test_billing_mismatches_fail_closed(change):
    binding, responses = fixture()
    proof = {'credentialSha256': binding['credentialSha256'],
             'responses': {k: {'status': 200, 'data': v} for k, v in responses.items()}}
    usage = responses['usage']['test-account']
    if change == 'stale': usage['last_update_ts'] = (NOW-timedelta(hours=3)).isoformat()
    elif change == 'identity': responses['status']['customer'] = 'other'
    elif change == 'plan': responses['pricing']['plans'][0]['id'] = 'changed'
    elif change == 'pending': responses['balance']['pending_costs'] = 0.01
    elif change == 'shared': usage['data']['other-resource'] = {'rows_initial_billable': [0]*18+[1]}
    elif change == 'totals': usage['sums']['test-resource']['custom']['rows_initial_billable'] = 0
    elif change == 'live': usage['snapshots']['test-resource'] = {'rows_initial_billable': [[1, 2]]}
    elif change == 'boolean': usage['data']['test-resource']['rows_initial_billable'][-1] = True
    else: proof['credentialSha256'] = 'wrong'
    with pytest.raises(ValueError): service.parse_billing_proof(proof, binding, NOW)


def test_delayed_rollup_separates_actual_usage_and_reservations(database):
    _, sessions = database
    async def run():
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'enabled': True, 'date': NOW.date().isoformat()})))
            cycle = CollectionCycle(trial_date=NOW.date(), phase='manual', cutoff_at=NOW, deadline_at=NOW+timedelta(minutes=30))
            db.add(cycle)
            await db.flush()
            for _ in range(16):
                sid, eid = uuid4(), uuid4()
                db.add(BrightDataExecution(id=eid, source_id=sid, started_at=NOW, completed_at=NOW+timedelta(minutes=1),
                    budget_month='2026-10', trigger_reason='trial:manual:test', endpoint='mock', dataset_id='mock',
                    start_date=NOW.date(), end_date=NOW.date(), record_limit=6, estimated_billable_usage=6,
                    snapshot_id='sd_test', returned_records=1, state='completed', newly_saved_posts=1))
                db.add(CollectionCycleSource(cycle_id=cycle.id, source_id=sid, name='Test', type='LINKEDIN',
                    canonical_url='https://www.linkedin.com/company/test/', window_start=NOW-timedelta(days=1),
                    window_end=NOW, execution_id=eid))
            await db.commit()
            binding, responses = fixture()
            evidence = service.parse_billing_proof({'credentialSha256': binding['credentialSha256'],
                'responses': {k: {'status': 200, 'data': v} for k, v in responses.items()}}, binding, NOW)
            await service.record_usage_observation(db, label='baseline', evidence=evidence, now=NOW)
            await service.record_usage_observation(db, label='after_evening', evidence=evidence, now=NOW+timedelta(minutes=2))
            result = await service.trial_usage_summary(db, NOW.date())
            assert result['postCount'] == 16 and result['reservedRecords'] == 96
            assert result['returnedRecords'] == 16 and result['provisional'] is True
            assert service.projected_reservation(20) == 240
            assert service.projected_reservation(8) == 96
            bad = {**evidence, 'api_token': 'must-not-store'}
            with pytest.raises(ValueError):
                await service.record_usage_observation(db, label='settled', evidence=bad, now=NOW)
    asyncio.run(run())


@pytest.mark.parametrize('profiles,prior,allowed', [(8, 25, True), (8, 40, False), (20, 0, False)])
def test_activation_enforces_full_inventory_headroom_without_raising_caps(database, monkeypatch, profiles, prior, allowed):
    from app import collection_trial_admin as admin
    from app.auth import ADMIN_ID
    from app.intelligence.models import NewsSource, NewsSourceEmailSubscription, BrightDataAccount
    _, sessions = database
    now = datetime.now(timezone.utc).replace(hour=6)
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_PASSWORD', 'test-only')
    async def capture(db, **kwargs):
        account = await db.get(BrightDataAccount, 1)
        account.verified_at = account.usage_as_of = now-timedelta(minutes=1)
        await db.commit()
    monkeypatch.setattr(admin, 'capture_trial_usage', capture)
    async def run():
        async with sessions() as db:
            sources = [NewsSource(name=f'Profile {i}', type='LINKEDIN', url=f'https://www.linkedin.com/company/test-{i}/', status='ACTIVE') for i in range(profiles)]
            db.add_all(sources)
            await db.flush()
            db.add(NewsSourceEmailSubscription(source_id=sources[0].id, user_id=ADMIN_ID, enabled_at=now))
            if prior:
                db.add(BrightDataExecution(source_id=uuid4(), started_at=now, budget_month=now.strftime('%Y-%m'),
                    endpoint='mock', dataset_id='mock', start_date=now.date(), end_date=now.date(),
                    trigger_reason='prior', state='completed', record_limit=6, estimated_billable_usage=prior))
            await db.commit()
            if allowed:
                result = await admin.activate_trial(db, approval_ref='test-one-day-approval', now=now)
                assert result['projectedReservedRecords'] == 96
                again = await admin.activate_trial(db, approval_ref='test-one-day-approval', now=now)
                assert again['alreadyActivated']
            else:
                with pytest.raises(ValueError):
                    await admin.activate_trial(db, approval_ref='test-one-day-approval', now=now)
            await admin.stop_trial(db)
            config = json.loads((await db.get(AppMetadata, TRIAL_KEY)).value)
            assert not config['enabled']
            assert len(list(await db.scalars(select(BrightDataExecution)))) == int(prior > 0)
    asyncio.run(run())


def test_month_rollover_does_not_subtract_incomparable_usage(database):
    _, sessions = database
    async def run():
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'date': '2026-10-19'})))
            await db.commit()
            binding, responses = fixture()
            evidence = service.parse_billing_proof({'credentialSha256': binding['credentialSha256'],
                'responses': {k: {'status': 200, 'data': v} for k, v in responses.items()}}, binding, NOW)
            await service.record_usage_observation(db, label='baseline', evidence=evidence, now=NOW)
            november = {**evidence, 'month': '2026-11', 'usage_as_of': '2026-11-01T08:00:00+00:00', 'billable_records': 0}
            await service.record_usage_observation(db, label='settled', evidence=november, now=datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
            summary = await service.trial_usage_summary(db, NOW.date())
            assert summary['providerDeltaRecords'] is None
            assert summary['attribution'] == 'incomparable' and summary['provisional']
    asyncio.run(run())


def test_normal_next_day_usage_cannot_be_final_trial_cost(database):
    from app.intelligence.collection_cycles import TrialUsageObservation
    _, sessions = database
    async def run():
        async with sessions() as db:
            db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps({'date': '2026-10-19'})))
            cycle = CollectionCycle(trial_date=NOW.date(), phase='evening', cutoff_at=NOW,
                deadline_at=NOW+timedelta(minutes=30), state='finished')
            db.add(cycle)
            await db.commit()
            binding, responses = fixture()
            evidence = service.parse_billing_proof({'credentialSha256': binding['credentialSha256'],
                'responses': {k: {'status': 200, 'data': v} for k, v in responses.items()}}, binding, NOW)
            await service.record_usage_observation(db, label='baseline', evidence=evidence, now=NOW)
            db.add(BrightDataExecution(source_id=uuid4(), started_at=NOW+timedelta(days=1),
                completed_at=NOW+timedelta(days=1, minutes=1), budget_month='2026-10', trigger_reason='scheduled-day',
                endpoint='mock', dataset_id='mock', start_date=NOW.date(), end_date=NOW.date(), record_limit=6,
                estimated_billable_usage=6, state='completed', snapshot_id='sd_nextday'))
            await db.commit()
            nextday = NOW+timedelta(days=1, hours=1)
            last = {**evidence, 'usage_as_of': nextday.isoformat(), 'billable_records': 7}
            await service.record_usage_observation(db, label='after_evening', evidence=last, now=nextday)
            summary = await service.trial_usage_summary(db, NOW.date())
            assert summary['attribution'] == 'uncertain'
            assert summary['provisional'] is True
    asyncio.run(run())
