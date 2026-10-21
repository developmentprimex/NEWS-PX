import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import httpx
from sqlalchemy import select

from app.config import settings
from app.intelligence import credit_control as credit, social_schedule as schedule, trial_usage
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsSource
from app.intelligence.collection_cycles import CollectionCycleSource
from test_brightdata_credit_control import account, execution, NOW
from test_brightdata_postgres_integration import database, private_postgres, Provider, source
from test_social_schedule import fresh_account
from test_collection_trial_usage import fixture, NOW as PROOF_NOW


def scoped_account():
    a = account()
    a.provider_billable_records = 10
    a.billing_scope = {'complete': True, 'account_ref': 'current', 'month': '2026-10',
        'usage_as_of': NOW.isoformat(), 'datasets': {'test': {'month': 10, 'daily': [0] * 13 + [10]}}}
    return a


def test_settled_same_account_usage_replaces_reservations_but_preserves_history():
    a = scoped_account()
    settled = execution(80, dataset_id='test', billing_account_ref='current',
        started_at=NOW-timedelta(hours=2), completed_at=NOW-timedelta(hours=1))
    pending = execution(6, dataset_id='test', billing_account_ref='current', state='uncertain')
    old = execution(6, dataset_id='test', billing_account_ref='old',
        started_at=NOW-timedelta(hours=2), completed_at=NOW-timedelta(hours=1))
    totals = credit.usage_totals(a, [settled, pending, old], NOW)
    assert totals['estimatedRecords'] == 92
    assert totals['providerRecords'] == 10
    assert totals['effectiveRecords'] == 22
    assert totals['dailyRecords'] == 22
    assert credit.dataset_usage_totals(a, [settled, pending, old], NOW, {'test'}) == {'month': 22, 'daily': 22}
    assert settled.estimated_billable_usage == 80


def test_stale_incomplete_or_inconsistent_proofs_cannot_release_reservations():
    for change in ['stale', 'total', 'partial']:
        a = scoped_account()
        if change == 'stale':a.verified_at = NOW-timedelta(days=2)
        elif change == 'total':a.provider_billable_records = 0
        else:a.billing_scope['complete'] = False
        run = execution(80, dataset_id='test', billing_account_ref='current',
            started_at=NOW-timedelta(hours=2), completed_at=NOW-timedelta(hours=1))
        assert credit.usage_totals(a, [run], NOW)['effectiveRecords'] >= 80


def test_daily_closed_utc_slot_is_exactly_24_hours_even_across_dst(monkeypatch):
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_DAILY_FETCH', True)
    for day in ['2026-10-25', '2027-03-28']:
        now = datetime.fromisoformat(day+'T00:05:00+00:00')
        phase, cutoff = schedule.due_daily_slot(now)
        assert phase == 'scheduled-day' and cutoff.hour == 0
        assert cutoff - schedule.previous_fetch_slot(cutoff) == timedelta(hours=24)
        assert schedule.due_daily_slot(cutoff+timedelta(minutes=4)) is None
        assert schedule.due_daily_slot(cutoff+timedelta(minutes=30)) is None


def test_one_daily_social_fetch_and_two_website_checks(database, monkeypatch):
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_DAILY_FETCH', True)
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_SCHEDULE_ENABLED', True)
    provider = Provider(monkeypatch)
    day = datetime(2026,10,21,tzinfo=timezone.utc)
    web_checks = []
    from app.intelligence import collection
    async def website(db, sid, **kwargs):
        web_checks.append(sid)
        return 'completed'
    monkeypatch.setattr(collection, 'check_source', website)
    async def run():
        await source(sessions)
        async with sessions() as db:
            db.add(NewsSource(name='Website', type='WEBSITE', url='https://example.com/news'))
            await fresh_account(db, day)
        for now in [day+timedelta(minutes=6), day+timedelta(minutes=7),
                    day+timedelta(hours=5), day+timedelta(hours=19)]:
            await schedule.run_social_collection(now)
        assert len(provider.triggers) == 1
        assert len(web_checks) == 2
        async with sessions() as db:
            rows = list(await db.scalars(select(CollectionCycleSource).where(CollectionCycleSource.type=='LINKEDIN')))
            assert len(rows) == 1 and rows[0].window_start == day-timedelta(days=1)
            assert rows[0].window_end == day and rows[0].coverage_confirmed
    asyncio.run(run())


def test_no_results_retries_next_cycle_without_permanent_pause_or_checkpoint(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, records=[{'error':'Activities are not found'}])
    from app.intelligence import collection
    now=datetime.now(timezone.utc)
    async def run():
        sid=await source(sessions)
        for _ in range(2):
            async with sessions() as db:await collection.check_source(db,sid,now=now)
        async with sessions() as db:
            s=await db.get(NewsSource,sid)
            e=(await db.scalars(select(BrightDataExecution))).one()
            assert s.linkedin_paused_reason is None and s.last_successful_fetch_at is None
            assert e.state=='partial' and e.coverage_warning.startswith('Provider returned no results')
        assert len(provider.triggers)==1
    asyncio.run(run())


def test_only_verified_transient_billing_pause_recovers(database, monkeypatch):
    _,sessions=database
    binding,responses=fixture()
    binding['datasetUsageResources']={'test-dataset':'test-resource'}
    proof=trial_usage.parse_billing_proof({'credentialSha256':binding['credentialSha256'],
        'responses':{k:{'status':200,'data':v} for k,v in responses.items()}},binding,PROOF_NOW)
    monkeypatch.setattr(trial_usage,'load_binding',lambda:binding)
    async def evidence(*args):return proof
    monkeypatch.setattr(trial_usage,'read_billing_evidence',evidence)
    async def run():
        for reason,expected in [('Billing observer stopped; operator review required: URLError',None),
                                ('Provider exceeded the reserved record limit. Verify billing before resuming.', 'keep')]:
            async with sessions() as db:
                a=await db.get(BrightDataAccount,1)
                a.provider_month='2026-10';a.provider_billable_records=0
                a.usage_as_of=PROOF_NOW-timedelta(seconds=1);a.paused_reason=reason
                await db.commit()
                await trial_usage.refresh_shared_billing(db,PROOF_NOW)
                assert a.paused_reason==(reason if expected=='keep' else None)
    asyncio.run(run())


def test_billing_retries_transport_reads_without_retrying_post(monkeypatch):
    binding,responses=fixture();calls=[]
    async def respond(request):
        calls.append(request)
        if len(calls)==1:raise httpx.ConnectError('temporary',request=request)
        names={'/status':'status','/customer/balance':'balance','/pricing_plans':'pricing','/customer/bw':'usage'}
        return httpx.Response(200,json=responses[names[request.url.path]])
    async def no_sleep(*args):pass
    monkeypatch.setattr(trial_usage.asyncio,'sleep',no_sleep)
    monkeypatch.setattr(trial_usage,'billing_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    result=asyncio.run(trial_usage.read_billing_evidence('test-token',binding,PROOF_NOW))
    assert result['billable_records']==1 and len(calls)==5
    assert all(r.method=='GET' for r in calls)


def test_repair_preserves_owner_pauses_and_never_advances_coverage(database):
    from app.collection_repair_admin import repair_sources, GENERIC_WARNING
    _,sessions=database
    now=datetime.now(timezone.utc)
    async def run():
        async with sessions() as db:
            active=NewsSource(name='Empty',type='LINKEDIN',url='https://www.linkedin.com/company/empty/',linkedin_paused_reason=GENERIC_WARNING)
            paused=NewsSource(name='Diaspora',type='FACEBOOK',url='https://www.facebook.com/diaspora/',status='PAUSED',linkedin_paused_reason=GENERIC_WARNING)
            db.add_all([active,paused]);await db.flush()
            for s in [active,paused]:
                db.add(BrightDataExecution(source_id=s.id,started_at=now,completed_at=now,budget_month=now.strftime('%Y-%m'),
                    trigger_reason='test',endpoint='mock',dataset_id='test',start_date=now.date(),end_date=now.date(),
                    record_limit=6,estimated_billable_usage=6,state='partial',api_errors='Activities are not found'))
            await db.commit()
            changes=await repair_sources(db)
            assert len(changes)==1 and changes[0]['name']=='Empty'
            assert active.linkedin_paused_reason is None and active.last_successful_fetch_at is None
            assert paused.status=='PAUSED' and paused.linkedin_paused_reason==GENERIC_WARNING
    asyncio.run(run())


def test_recovery_preview_is_inert_and_apply_is_one_use_with_expiring_permit(database,monkeypatch):
    from app import collection_recovery_admin as recovery
    from app.intelligence.collection_cycles import CollectionCycle
    from app.intelligence.cycle_service import cycle_authorized
    _,sessions=database
    monkeypatch.setattr(recovery,'SessionLocal',sessions)
    monkeypatch.setattr(settings,'NEWS_SOCIAL_SCHEDULE_ENABLED',True)
    provider=Provider(monkeypatch)
    async def fresh(*args):return None
    monkeypatch.setattr(recovery,'refresh_shared_billing',fresh)
    now=datetime.now(timezone.utc);cutoff=now.replace(hour=0,minute=0,second=0,microsecond=0)
    args=NS(through=cutoff.isoformat(),apply=False,approval_ref='test-owner',max_reserved=30)
    async def run():
        await source(sessions)
        result=await recovery.run(args)
        assert result['state']=='preview' and result['sourceCount']==1
        async with sessions() as db:
            assert not list(await db.scalars(select(CollectionCycle)))
            assert not list(await db.scalars(select(BrightDataExecution)))
        args.apply=True
        result=await recovery.run(args)
        assert result['state']=='authorized'
        again=await recovery.run(args)
        assert again['state']=='already-authorized' and again['cycle_id']==result['cycle_id']
        async with sessions() as db:
            cycle=(await db.scalars(select(CollectionCycle))).one()
            assert await cycle_authorized(db,cycle,datetime.now(timezone.utc))
            assert not await cycle_authorized(db,cycle,datetime.now(timezone.utc)+timedelta(hours=1))
            assert not list(await db.scalars(select(BrightDataExecution)))
        await schedule.run_social_collection(datetime.now(timezone.utc))
        await schedule.run_social_collection(datetime.now(timezone.utc)+timedelta(seconds=1))
        await schedule.run_social_collection(datetime.now(timezone.utc)+timedelta(seconds=2))
        assert len(provider.triggers)==1
    asyncio.run(run())
