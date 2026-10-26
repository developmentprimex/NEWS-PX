import asyncio,json
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.config import settings
from app.intelligence import collection,social_schedule as schedule
from app.intelligence.collection_cycles import CollectionCycle,CollectionCycleSource
from app.intelligence.cycle_service import freeze_inventory
from app.intelligence.models import NewsSource,NewsItem,BrightDataAccount
from test_brightdata_postgres_integration import database,private_postgres,Provider,source

PM=datetime(2026,10,26,20,tzinfo=timezone.utc)
AM=datetime(2026,10,27,6,tzinfo=timezone.utc)

@pytest.fixture(autouse=True)
def twice_daily(monkeypatch):
    monkeypatch.setattr(settings,'NEWS_SOCIAL_SCHEDULE_ENABLED',True)
    monkeypatch.setattr(settings,'NEWS_SOCIAL_DAILY_FETCH',False)
    monkeypatch.setattr(settings,'NEWS_SOCIAL_ADAPTIVE_LIMITS',True)

async def account(db,now):
    row=await db.get(BrightDataAccount,1)
    row.provider_month=now.strftime('%Y-%m');row.usage_as_of=row.verified_at=now-timedelta(seconds=1)
    await db.commit()

def test_switch_preserves_last_daily_checkpoint_then_uses_exact_night_window(database,monkeypatch):
    _,sessions=database
    provider=Provider(monkeypatch)
    midnight=PM.replace(hour=0)
    async def run():
        sid=await source(sessions)
        async with sessions() as db:
            old=CollectionCycle(trial_date=midnight.date(),phase='scheduled-day',cutoff_at=midnight,
                deadline_at=midnight+timedelta(minutes=30),state='finished',inventory_frozen=True)
            db.add(old);await db.flush()
            db.add(CollectionCycleSource(cycle_id=old.id,source_id=sid,name='Example',type='LINKEDIN',
                canonical_url='https://www.linkedin.com/company/example/',window_start=midnight-timedelta(days=1),
                window_end=midnight,verified_start=True,state='completed',coverage_confirmed=True))
            await db.commit();await account(db,PM)
        for now in (PM,PM+timedelta(minutes=1),AM,AM+timedelta(minutes=1)):
            async with sessions() as db:await account(db,now)
            await schedule.run_social_collection(now)
        async with sessions() as db:
            rows=list(await db.scalars(select(CollectionCycleSource).where(CollectionCycleSource.source_id==sid).order_by(CollectionCycleSource.window_end)))
            assert rows[-2].window_start==midnight and rows[-2].window_end==PM
            assert rows[-1].window_start==PM and rows[-1].window_end==AM
            assert rows[-1].coverage_confirmed
        assert len(provider.triggers)==2
        await schedule.run_social_collection(midnight+timedelta(days=1,minutes=5))
        assert len(provider.triggers)==2
    asyncio.run(run())

def test_switch_retains_unresolved_anchor_without_a_successful_checkpoint(database):
    _,sessions=database
    anchor=PM-timedelta(days=3)
    async def run():
        sid=await source(sessions)
        async with sessions() as db:
            old=CollectionCycle(trial_date=anchor.date(),phase='scheduled-day',cutoff_at=anchor+timedelta(days=1),
                deadline_at=anchor+timedelta(days=1,minutes=30),state='finished',inventory_frozen=True)
            db.add(old);await db.flush()
            db.add(CollectionCycleSource(cycle_id=old.id,source_id=sid,name='Example',type='LINKEDIN',
                canonical_url='https://www.linkedin.com/company/example/',window_start=anchor,
                window_end=old.cutoff_at,verified_start=False,state='partial',coverage_confirmed=False))
            await db.commit()
            current=await schedule.ensure_scheduled_cycle(db,PM)
            row=await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.cycle_id==current.id))
            assert row.window_start==anchor and not row.verified_start
    asyncio.run(run())

def test_facebook_known_post_exclusions_work_in_both_slots(database,monkeypatch):
    from app.intelligence import facebook_collection
    _,sessions=database
    provider=Provider(monkeypatch)
    url='https://www.facebook.com/example/'
    monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
    monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',[url])
    async def billing(*args):pass
    monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',billing)
    async def run():
        async with sessions() as db:
            s=NewsSource(name='Example',url=url,type='FACEBOOK',status='ACTIVE',categories=[])
            db.add(s);await db.flush()
            db.add(NewsItem(source_id=s.id,external_id='facebook:111',url=url+'posts/111/',title='Already stored',
                published_at=PM-timedelta(hours=1),publication_time_verified=True))
            await db.commit()
        for now in (PM,PM+timedelta(minutes=1),AM,AM+timedelta(minutes=1)):
            async with sessions() as db:await account(db,now)
            await schedule.run_social_collection(now)
        assert len(provider.triggers)==2
        for request in provider.triggers:
            assert json.loads(request.content)[0]['posts_to_not_include']==['111']
    asyncio.run(run())

def test_morning_retains_only_new_activity_after_evening_and_preserves_post_ids(database,monkeypatch):
    _,sessions=database
    before={'id':'111','url':'https://www.linkedin.com/posts/example_activity-111-abc',
            'post_text':'Before evening','date_posted':(PM-timedelta(minutes=1)).isoformat()}
    boundary={'id':'222','url':'https://www.linkedin.com/posts/example_activity-222-abc',
            'post_text':'Exactly at evening cutoff','date_posted':PM.isoformat()}
    after={'id':'333','url':'https://www.linkedin.com/posts/example_activity-333-abc',
            'post_text':'After evening cutoff','date_posted':(PM+timedelta(minutes=1)).isoformat()}
    provider=Provider(monkeypatch,records=[before,boundary])
    async def run():
        sid=await source(sessions)
        for now in (PM,PM+timedelta(minutes=1)):
            async with sessions() as db:await account(db,now)
            await schedule.run_social_collection(now)
        async with sessions() as db:
            prior={i.external_id:i.id for i in await db.scalars(select(NewsItem).where(NewsItem.source_id==sid))}
            assert set(prior)=={'111','222'}
        provider.records=[before,boundary,after]
        for now in (AM,AM+timedelta(minutes=1)):
            async with sessions() as db:await account(db,now)
            await schedule.run_social_collection(now)
        async with sessions() as db:
            stored={i.external_id:i.id for i in await db.scalars(select(NewsItem).where(NewsItem.source_id==sid))}
            assert set(stored)=={'111','222','333'}
            assert all(stored[k]==value for k,value in prior.items())
            row=await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.source_id==sid,CollectionCycleSource.window_end==AM))
            from app.intelligence.models import BrightDataExecution
            execution=await db.get(BrightDataExecution,row.execution_id)
            assert execution.newly_saved_posts==1 and execution.ignored_records==2
    asyncio.run(run())
