"""A single evening can exceed daily caps; expiry, billing and identity still hold."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import AppMetadata
from app.intelligence import collection, facebook_collection, social_schedule as schedule
from app.intelligence.collection_exception import applies, observe, SATURATION_PAUSE
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
from app.intelligence.facebook_adapter import request_spec
from app.intelligence.linkedin_adapter import LinkedInCollectionError
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsSource, NewsItem, NewsSourceEmailSubscription, NewsDigestSettings
from test_brightdata_postgres_integration import Provider, database, private_postgres, source

PM = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def experiment(monkeypatch):
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_SCHEDULE_ENABLED', True)
    monkeypatch.setattr(settings, 'NEWS_COLLECTION_EXCEPTION_DATE', date(2026, 10, 20))


@pytest.mark.parametrize('offset,phase,expected', [(-1,'scheduled-pm',False), (0,'scheduled-pm',True),
    (1799,'scheduled-pm',True), (1800,'scheduled-pm',False), (86400,'scheduled-pm',False),
    (0,'scheduled-am',False), (0,'evening',False)])
def test_exact_evening_only(monkeypatch, offset, phase, expected):
    monkeypatch.setattr(settings, 'NEWS_ENABLE_COLLECTION', True)
    cycle = NS(phase=phase, trial_date=PM.date(), cutoff_at=PM)
    assert applies(cycle, PM + timedelta(seconds=offset)) is expected
    monkeypatch.setattr(settings, 'NEWS_COLLECTION_EXCEPTION_DATE', None)
    assert not applies(cycle, PM)


def test_facebook_ordinary_adapter_stays_bounded():
    src = NS(url='https://www.facebook.com/example/')
    with pytest.raises(LinkedInCollectionError):
        request_spec(src, '2026-10-19', '2026-10-21', 80)
    query, body = request_spec(src, '2026-10-19', '2026-10-21', 80, expanded=True)
    assert query['limit_per_input'] == body[0]['num_of_posts'] == 80
    with pytest.raises(LinkedInCollectionError):
        request_spec(src, '2026-10-19', '2026-10-21', 81, expanded=True)


async def fresh(db, now, *, used=0, paused=None):
    account = await db.get(BrightDataAccount, 1)
    account.provider_month = now.strftime('%Y-%m')
    account.usage_as_of = account.verified_at = now - timedelta(minutes=1)
    account.provider_billable_records = used
    account.paused_reason = paused
    await db.commit()


def test_every_approved_source_gets_one_larger_request_and_restart_never_repeats(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    urls = ['https://www.facebook.com/example' + str(i) + '/' for i in range(5)]
    monkeypatch.setattr(settings, 'NEWS_FACEBOOK_FETCH_ENABLED', True)
    monkeypatch.setattr(settings, 'NEWS_FACEBOOK_APPROVED_SOURCE_URLS', urls)
    async def billing(*args):
        pass
    monkeypatch.setattr(facebook_collection, 'refresh_facebook_billing', billing)
    async def run():
        await source(sessions, linkedin_paused_reason=SATURATION_PAUSE)
        async with sessions() as db:
            for i, url in enumerate(urls):
                db.add(NewsSource(name='Facebook '+str(i), type='FACEBOOK', status='ACTIVE', url=url,
                    linkedin_paused_reason=SATURATION_PAUSE if i == 0 else None))
            db.add(NewsSource(name='Not approved', type='FACEBOOK', status='ACTIVE', url='https://facebook.com/excluded/'))
            db.add(NewsSource(name='Paused by owner', type='LINKEDIN', status='PAUSED', url='https://linkedin.com/company/paused/'))
            db.add(NewsSource(name='Invalid attribution', type='LINKEDIN', status='ACTIVE', url='https://linkedin.com/company/invalid/',
                linkedin_paused_reason='Snapshot contains invalid, unattributed or out-of-window activity; review coverage before resuming.'))
            await db.commit()
            await fresh(db, PM)
        for minute in (0, 1, 2, 3):
            await schedule.run_social_collection(PM + timedelta(minutes=minute))
        assert len(provider.triggers) == 6
        assert all(r.url.params['limit_per_input'] == '80' for r in provider.triggers)
        async with sessions() as db:
            rows = list(await db.scalars(select(BrightDataExecution)))
            assert len(rows) == 6 and sum(e.estimated_billable_usage for e in rows) == 480
            assert all(e.state == 'completed' for e in rows)
            cycle = await db.scalar(select(CollectionCycle))
            assert cycle.state == 'finished'
            windows = list(await db.scalars(select(CollectionCycleSource)))
            assert all(w.window_start == PM - timedelta(hours=14) for w in windows)
            assert sum(w.state == 'blocked' for w in windows) == 1
            assert sum(s.linkedin_paused_reason == SATURATION_PAUSE for s in await db.scalars(select(NewsSource))) == 0
        # Tomorrow the original 3-record Facebook requests and 12/day cap resume.
        # Isolate daily expiry from this test's 400 reserved Facebook records.
        monkeypatch.setattr(settings, 'NEWS_FACEBOOK_MONTHLY_BUDGET', 1000)
        async with sessions() as db:
            await fresh(db, PM + timedelta(days=1))
        await schedule.run_social_collection(PM + timedelta(days=1))
        new = provider.triggers[6:]
        assert sum('num_of_posts' in json.loads(r.content)[0] for r in new) == 4
        assert all(r.url.params['limit_per_input'] in {'3', '6'} for r in new)
    asyncio.run(run())


def test_reserved_snapshot_finishes_after_exception_expiry_without_a_second_post(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions, linkedin_paused_reason=SATURATION_PAUSE)
        async with sessions() as db:
            await fresh(db, PM)
        await schedule.run_social_collection(PM)
        await schedule.run_social_collection(PM+timedelta(minutes=31))
        assert len(provider.triggers) == 1
        async with sessions() as db:
            execution = await db.scalar(select(BrightDataExecution))
            assert execution.state == 'completed'
            assert (await db.scalar(select(CollectionCycle))).state == 'finished'
    asyncio.run(run())


@pytest.mark.parametrize('used,paused', [(3950,None),(0,'Account billing is paused.')])
def test_experiment_never_bypasses_monthly_ceiling_or_account_pause(database, monkeypatch, used, paused):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh(db, PM, used=used, paused=paused)
        await schedule.run_social_collection(PM)
        assert not provider.triggers
    asyncio.run(run())


def test_usage_baseline_and_delayed_rollup_are_distinct_from_reservations(database, monkeypatch):
    _, sessions = database
    from app.intelligence import trial_usage
    async def billing(db, now):
        await fresh(db, now)
        account = await db.get(BrightDataAccount, 1)
        account.provider_billable_records = 9
    monkeypatch.setattr(trial_usage, 'refresh_shared_billing', billing)
    async def run():
        async with sessions() as db:
            await fresh(db, PM, used=7)
            cycle = CollectionCycle(trial_date=PM.date(), phase='scheduled-pm', cutoff_at=PM,
                deadline_at=PM+timedelta(minutes=30), state='running', inventory_frozen=True)
            db.add(cycle)
            await db.commit()
            await observe(db, cycle, PM, before=True)
            await observe(db, cycle, PM+timedelta(seconds=1), before=True)
            cycle.state = 'finished'
            await db.commit()
            await observe(db, cycle, PM+timedelta(minutes=11))
            await observe(db, cycle, PM+timedelta(minutes=12))
            record = await db.scalar(select(AppMetadata).where(AppMetadata.key.like('collection-exception:%')))
            data = json.loads(record.value)
            assert len(data) == 2
            assert data[0]['providerRecords'] == 7 and data[1]['providerRecords'] == 9
            assert data[1]['providerDeltaRecords'] == 2
            assert data[1]['reservedRecords'] == 0
            assert data[1]['provisional'] is True
    asyncio.run(run())


def test_today_additionally_checks_websites_but_tomorrow_does_not(database, monkeypatch):
    _, sessions = database
    calls = []
    async def rss(adapter):
        calls.append(adapter.source_url)
        return []
    monkeypatch.setattr(collection.RSSAdapter, 'collect', rss)
    async def run():
        async with sessions() as db:
            db.add(NewsSource(name='Feed', type='RSS', status='ACTIVE', url='https://example.org/feed'))
            await db.commit()
        await schedule.run_social_collection(PM)
        await schedule.run_social_collection(PM+timedelta(minutes=1))
        assert calls == ['https://example.org/feed']
        await schedule.run_social_collection(PM+timedelta(days=1))
        assert len(calls) == 1
    asyncio.run(run())


def test_larger_linkedin_result_saves_only_the_last_exact_interval(database, monkeypatch):
    _, sessions = database
    times = ['2026-10-20T05:00:00Z', '2026-10-20T05:00:01Z', '2026-10-20T19:00:00Z',
        '2026-10-20T19:00:01Z'] + ['2026-10-19T12:00:00Z'] * 4
    rows = [{'id':str(500+i), 'url':f'https://www.linkedin.com/posts/example_activity-{500+i}-abc',
        'post_text':'Update', 'date_posted':stamp} for i,stamp in enumerate(times)]
    provider = Provider(monkeypatch, rows)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh(db, PM)
        await schedule.run_social_collection(PM)
        await schedule.run_social_collection(PM+timedelta(minutes=31))
        assert len(provider.triggers) == 1
        async with sessions() as db:
            assert {item.external_id for item in await db.scalars(select(NewsItem))} == {'501','502'}
            execution = await db.scalar(select(BrightDataExecution))
            assert execution.returned_records == 8 and execution.ignored_records == 6
            assert execution.state == 'completed'
    asyncio.run(run())


def test_evening_email_only_contains_confirmed_activity_in_the_last_slot_for_every_type(database, monkeypatch):
    from uuid import uuid4
    from app.intelligence.collection_cycles import ScheduledDigestReport
    _, sessions = database
    monkeypatch.setattr(settings, 'EMAIL_USER', 'sender@example.org')
    monkeypatch.setattr(settings, 'EMAIL_PASSWORD', 'test-only')
    sent = []
    async def smtp(subject, body, html):
        sent.append(body)
    monkeypatch.setattr(schedule, '_send_digest', smtp)
    async def run():
        user_id = uuid4()
        async with sessions() as db:
            await fresh(db, PM)
            db.add(NewsDigestSettings(user_id=user_id, times=['07:00','21:00'],
                last_slot_at=PM-timedelta(days=2)))
            keep = set()
            for kind in ('LINKEDIN','FACEBOOK','WEBSITE','RSS'):
                src = NewsSource(name=kind, type=kind, status='ACTIVE', url='https://example.org/'+kind)
                db.add(src)
                await db.flush()
                db.add(NewsSourceEmailSubscription(user_id=user_id, source_id=src.id, enabled_at=PM-timedelta(days=4)))
                for label, stamp, verified in [('old',PM-timedelta(days=1),True),
                    ('at-seven',PM-timedelta(hours=14),True),('inside',PM-timedelta(hours=1),True),
                    ('at-cutoff',PM,True),('after',PM+timedelta(seconds=1),True),('undated',None,False)]:
                    item=NewsItem(source_id=src.id,title=kind+' '+label,url='https://example.org/'+kind+'/'+label,
                        published_at=stamp,publication_time_verified=verified,created_at=PM+timedelta(minutes=1))
                    db.add(item)
                    await db.flush()
                    if label in {'inside','at-cutoff'}:keep.add(str(item.id))
                if kind=='LINKEDIN':
                    db.add(NewsItem(source_id=src.id,title='Unverified repost',url='https://example.org/repost',
                        published_at=PM-timedelta(hours=1),publication_time_verified=False,
                        linkedin_data={'kind':'REPOST'},created_at=PM+timedelta(minutes=1)))
            cycle=CollectionCycle(trial_date=PM.date(),phase='scheduled-pm',cutoff_at=PM,
                deadline_at=PM+timedelta(minutes=30),state='finished',inventory_frozen=True)
            db.add(cycle)
            await db.commit()
            assert await schedule.process_scheduled_digest(db,cycle,user_id,PM+timedelta(minutes=2))==8
            report=await db.scalar(select(ScheduledDigestReport))
            assert set(report.content['item_ids'])==keep
            assert len(sent)==1
            assert await schedule.process_scheduled_digest(db,cycle,user_id,PM+timedelta(minutes=3))==0
    asyncio.run(run())
