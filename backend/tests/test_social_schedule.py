from app.auth import ADMIN_ID, User
"""Recurring real-ledger scheduling; provider HTTP, RSS and SMTP remain mocked."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.intelligence import collection, social_schedule as service
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, ScheduledDigestReport
from app.intelligence.models import (
    BrightDataAccount, BrightDataExecution, NewsDigestSettings, NewsItem,
    NewsSource, NewsSourceEmailSubscription,
)
from test_brightdata_postgres_integration import Provider, database, private_postgres, source

AM = datetime(2026, 10, 20, 5, tzinfo=timezone.utc)  # 07:00 Budapest, summer time.
PM = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def recurring(monkeypatch):
    monkeypatch.setattr(settings, 'NEWS_SOCIAL_SCHEDULE_ENABLED', True)
    monkeypatch.setattr(settings, 'EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'EMAIL_PASSWORD', 'test-only')


@pytest.mark.parametrize('stamp,phase', [
    ('2026-10-20T04:59:59+00:00', None),
    ('2026-10-20T05:00:00+00:00', 'scheduled-am'),
    ('2026-10-20T05:29:59+00:00', 'scheduled-am'),
    ('2026-10-20T05:30:00+00:00', None),
    ('2026-10-20T12:00:00+00:00', None),
    ('2026-10-20T19:00:00+00:00', 'scheduled-pm'),
    ('2026-10-25T06:00:00+00:00', 'scheduled-am'),
    ('2026-10-25T20:00:00+00:00', 'scheduled-pm'),
    ('2027-03-28T05:00:00+00:00', 'scheduled-am'),
    ('2027-03-28T19:00:00+00:00', 'scheduled-pm'),
])
def test_daily_slots_follow_local_clock_and_dst(stamp, phase):
    stamp = datetime.fromisoformat(stamp)
    slot = service.due_slot(stamp)
    assert (slot[0] if slot else None) == phase
    if slot:
        assert service.previous_slot(slot[1]) < slot[1]


async def fresh_account(db, now):
    account = await db.get(BrightDataAccount, 1)
    account.provider_month = now.strftime('%Y-%m')
    account.verified_at = account.usage_as_of = now - timedelta(minutes=1)
    await db.commit()


def test_two_social_fetches_per_day_repeat_daily_and_survive_restart(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions)
        for day in range(2):
            for cutoff in (AM + timedelta(days=day), PM + timedelta(days=day)):
                async with sessions() as db:
                    await fresh_account(db, cutoff)
                await service.run_social_collection(cutoff)
                await service.run_social_collection(cutoff + timedelta(minutes=1))
                await service.run_social_collection(cutoff + timedelta(minutes=2))
            await service.run_social_collection(AM + timedelta(days=day, hours=7))
        assert len(provider.triggers) == 4
        async with sessions() as db:
            cycles = list(await db.scalars(select(CollectionCycle).order_by(CollectionCycle.cutoff_at)))
            assert len(cycles) == 4 and all(c.state == 'finished' for c in cycles)
            rows = list(await db.scalars(select(CollectionCycleSource).order_by(CollectionCycleSource.window_end)))
            assert rows[0].window_start == service.previous_slot(AM)
            assert rows[1].window_start == AM
            assert len(list(await db.scalars(select(BrightDataExecution)))) == 4
    asyncio.run(run())


def test_expired_slot_and_direct_checks_cannot_start_extra_social_jobs(database, monkeypatch):
    from app.intelligence.cycle_service import dispatch_cycle_source
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
            cycle = await service.ensure_scheduled_cycle(db, AM)
            row = await db.scalar(select(CollectionCycleSource).where(CollectionCycleSource.cycle_id == cycle.id))
            assert await dispatch_cycle_source(db, row.id, AM + timedelta(minutes=30)) == 'blocked'
            assert await collection.check_source(db, sid, now=AM, force=False) == 'paused'
            assert await collection.check_source(db, sid, now=AM, force=True) == 'paused'
        await service.run_social_collection(AM + timedelta(hours=3))
        assert not provider.triggers
    asyncio.run(run())


def test_budget_denial_remains_enforced_for_scheduled_cycle(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
            account = await db.get(BrightDataAccount, 1)
            account.provider_billable_records = 4000
            await db.commit()
        await service.run_social_collection(AM)
        async with sessions() as db:
            row = (await db.scalars(select(CollectionCycleSource))).one()
            assert row.state == 'blocked'
        assert not provider.triggers
    asyncio.run(run())


def test_pending_snapshot_can_finish_after_slot_without_replacement_fetch(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
        await service.run_social_collection(AM)
        await service.run_social_collection(AM + timedelta(minutes=31))
        assert len(provider.triggers) == 1
        async with sessions() as db:
            row = (await db.scalars(select(CollectionCycleSource))).one()
            assert row.state == 'completed' and row.coverage_confirmed
    asyncio.run(run())


@pytest.mark.parametrize('billing_ok', [True, False])
def test_recurring_billing_refresh_does_not_require_a_trial_date(database, monkeypatch, billing_ok):
    from app.intelligence import trial_usage
    _, sessions = database
    provider = Provider(monkeypatch)
    monkeypatch.setattr(settings, 'NEWS_TRIAL_BILLING_BINDING_FILE', 'configured-private-binding')
    monkeypatch.setattr(trial_usage, 'load_binding', lambda: {'bound': True})
    reads = []
    async def evidence(token, binding, now):
        reads.append(now)
        if not billing_ok:
            raise ValueError('Billing cannot be verified')
        return {'account_ref': 'test-account', 'month': now.strftime('%Y-%m'), 'billable_records': 0,
                'usage_as_of': (now - timedelta(seconds=1)).isoformat(), 'balance': 0,
                'active': True, 'verified_monthly_allowance': 5000, 'evidence': 'Bound test account'}
    monkeypatch.setattr(trial_usage, 'read_billing_evidence', evidence)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
        for cutoff in (AM, PM):
            await service.run_social_collection(cutoff)
            await service.run_social_collection(cutoff + timedelta(minutes=1))
        assert len(reads) == (2 if billing_ok else 4)
        assert len(provider.triggers) == (2 if billing_ok else 0)
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            if billing_ok:
                assert account.verified_at == PM
                assert account.verified_monthly_allowance == 5000
    asyncio.run(run())


def test_clock_is_rechecked_after_reservation_before_provider_post(database, monkeypatch):
    from app.intelligence import linkedin_collection
    _, sessions = database
    provider = Provider(monkeypatch)
    current = {'now': AM}
    reserve = linkedin_collection.reserve_execution
    async def reserve_then_expire(*args, **kwargs):
        execution = await reserve(*args, **kwargs)
        current['now'] = AM + timedelta(minutes=30)
        return execution
    monkeypatch.setattr(linkedin_collection, 'reserve_execution', reserve_then_expire)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
        await service.run_social_collection(clock=lambda: current['now'])
        assert not provider.triggers
        async with sessions() as db:
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.state == 'cancelled' and execution.estimated_billable_usage == 6
    asyncio.run(run())


def test_concurrent_collection_workers_and_new_sources_wait_for_next_slot(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, delay=0.05)
    async def run():
        await source(sessions)
        async with sessions() as db:
            await fresh_account(db, AM)
        await asyncio.gather(service.run_social_collection(AM), service.run_social_collection(AM))
        await service.run_social_collection(AM + timedelta(minutes=1))
        assert len(provider.triggers) == 1
        await source(sessions, url='https://www.linkedin.com/company/added-later/')
        await service.run_social_collection(AM + timedelta(minutes=2))
        assert len(provider.triggers) == 1
        async with sessions() as db:
            await fresh_account(db, PM)
        await service.run_social_collection(PM)
        await service.run_social_collection(PM + timedelta(minutes=1))
        assert len(provider.triggers) == 3
    asyncio.run(run())


def test_reports_run_without_waiting_for_collection_worker_lock(database, monkeypatch):
    from sqlalchemy import text
    _, sessions = database
    sent = []
    async def send(*content, **kwargs):
        sent.append(content)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            await prepare_report(db, pending=True)
        async with collection.engine.connect() as worker:
            await worker.execute(text('SELECT pg_advisory_lock(7302192100)'))
            try:
                await asyncio.wait_for(service.run_social_reports(AM + timedelta(minutes=30)), timeout=5)
            finally:
                await worker.execute(text('SELECT pg_advisory_unlock(7302192100)'))
        assert len(sent) == 1
    asyncio.run(run())


def test_scheduled_rss_checks_and_periodic_checks_do_not_start_extra_facebook_jobs(database, monkeypatch):
    from app.intelligence import facebook_collection
    _, sessions = database
    provider = Provider(monkeypatch)
    url = 'https://www.facebook.com/example/'
    monkeypatch.setattr(settings, 'NEWS_FACEBOOK_FETCH_ENABLED', True)
    monkeypatch.setattr(settings, 'NEWS_FACEBOOK_APPROVED_SOURCE_URLS', [url])
    async def billing(*args):
        pass
    monkeypatch.setattr(facebook_collection, 'refresh_facebook_billing', billing)
    reads = []
    async def rss(adapter):
        reads.append(adapter.source_url)
        return []
    monkeypatch.setattr(collection.RSSAdapter, 'collect', rss)
    async def run():
        async with sessions() as db:
            db.add_all([NewsSource(name='Facebook', url=url, type='FACEBOOK', status='ACTIVE'),
                        NewsSource(name='News', url='https://example.com/feed', type='RSS', status='ACTIVE')])
            await db.commit()
        for cutoff in (AM, PM):
            async with sessions() as db:
                await fresh_account(db, cutoff)
            await service.run_social_collection(cutoff)
            await service.run_social_collection(cutoff + timedelta(minutes=1))
        assert len(provider.triggers) == 2
        assert reads == ['https://example.com/feed'] * 2
        await collection.check_due_sources()
        assert reads == ['https://example.com/feed'] * 3
        assert len(provider.triggers) == 2
        async with sessions() as db:
            assert set(await db.scalars(select(CollectionCycleSource.type))) == {'FACEBOOK', 'RSS'}
    asyncio.run(run())


async def prepare_report(db, *, pending=False):
    user = ADMIN_ID
    digest = NewsDigestSettings(user_id=user, times=['12:30'], last_slot_at=service.previous_slot(AM))
    active = NewsSource(name='Active & useful', type='LINKEDIN', url='https://linkedin.com/company/test/', status='ACTIVE')
    news = NewsSource(name='News', type='RSS', url='https://example.org/feed', status='ACTIVE')
    idle = NewsSource(name='Idle account', type='FACEBOOK', url='https://facebook.com/idle/', status='ACTIVE')
    db.add_all([digest, active, news, idle])
    await db.flush()
    for src in (active, news, idle):
        db.add(NewsSourceEmailSubscription(source_id=src.id, user_id=user, enabled_at=AM - timedelta(days=2)))
    cycle = CollectionCycle(trial_date=AM.date(), phase='scheduled-am', cutoff_at=AM,
        deadline_at=AM + timedelta(minutes=30), state='running' if pending else 'finished', inventory_frozen=True)
    db.add(cycle)
    await db.flush()
    db.add(CollectionCycleSource(cycle_id=cycle.id, source_id=active.id, name=active.name,
        type=active.type, canonical_url='https://www.linkedin.com/company/test/',
        window_start=service.previous_slot(AM), window_end=AM, verified_start=True,
        state='running' if pending else 'completed'))
    for src, title in ((active, 'New social post'), (news, 'New RSS news')):
        db.add(NewsItem(source_id=src.id, title=title, url='https://example.org/' + str(uuid4()),
            original_text=title, published_at=AM - timedelta(minutes=10), created_at=AM + timedelta(minutes=1),
            linkedin_data={'kind': 'POST'} if src == active else None, publication_time_verified=True))
    db.add(NewsItem(source_id=news.id, title='Future news', url='https://example.org/future',
        published_at=AM + timedelta(hours=1), created_at=AM + timedelta(minutes=1), publication_time_verified=True))
    db.add(NewsItem(source_id=active.id, title='Unverified social time', url='https://example.org/unknown',
        published_at=AM - timedelta(minutes=5), created_at=service.previous_slot(AM) - timedelta(minutes=1),
        linkedin_data={'kind': 'REPOST'}, publication_time_verified=False))
    await db.commit()
    return user, cycle.id


def test_combined_reports_wait_for_collection_and_send_once_per_slot(database, monkeypatch):
    _, sessions = database
    sent = []
    async def send(*content, **kwargs):
        sent.append(content)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db, pending=True)
            cycle = await db.get(CollectionCycle, cid)
            assert await service.process_scheduled_digest(db, cycle, user, AM + timedelta(minutes=2)) == 0
            assert not sent
            assert await service.process_scheduled_digest(db, cycle, user, AM + timedelta(minutes=30)) == 2
        assert len(sent) == 1
        for content in sent[0][1:]:
            assert 'New social post' in content and 'New RSS news' in content
            assert 'Future news' not in content and 'Idle account' not in content
            assert 'Unverified social time' not in content
            assert '0 repostime' not in content
        async with sessions() as db:
            assert await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=31)) == 0
            cycle = CollectionCycle(trial_date=PM.date(), phase='scheduled-pm', cutoff_at=PM,
                deadline_at=PM + timedelta(minutes=30), state='finished', inventory_frozen=True)
            db.add(cycle)
            await db.commit()
            assert await service.process_scheduled_digest(db, cycle, user, PM) == 1
            assert 'Future news' in sent[1][1] and 'New social post' not in sent[1][1]
            assert len(list(await db.scalars(select(ScheduledDigestReport)))) == 2
    asyncio.run(run())


@pytest.mark.parametrize('phase,cutoff', [('scheduled-am', AM), ('scheduled-pm', PM)])
def test_scheduled_report_separates_discovery_from_publication_interval(database, monkeypatch, phase, cutoff):
    from sqlalchemy import delete
    from unittest.mock import AsyncMock

    _, sessions = database
    send = AsyncMock()
    monkeypatch.setattr(service, '_send_digest', send)

    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            cycle = await db.get(CollectionCycle, cid)
            cycle.phase, cycle.cutoff_at = phase, cutoff
            cycle.deadline_at = cutoff + timedelta(minutes=30)
            start = service.previous_slot(cutoff)
            digest = await db.get(NewsDigestSettings, user)
            digest.last_slot_at = start - timedelta(days=1)
            news = await db.scalar(select(NewsSource).where(NewsSource.type == 'RSS'))
            news.type = 'WEBSITE'
            social = await db.scalar(select(NewsSource).where(NewsSource.type == 'LINKEDIN'))
            await db.execute(delete(NewsItem))
            repost_at = cutoff - timedelta(minutes=1)
            cases = [
                (news, 'Older article imported now', start - timedelta(hours=1), True, None),
                (news, 'Already in previous slot', start, True, None),
                (news, 'Unverified article', start + timedelta(hours=1), False, None),
                (news, 'Undated article', None, False, None),
                (news, 'After cutoff', cutoff + timedelta(seconds=1), True, None),
                (news, 'Article at cutoff', cutoff, True, None),
                (social, 'Recent verified repost', start - timedelta(days=2), False,
                 {'kind': 'REPOST', 'activityTimeEvidence': 'verified-repost-time', 'activityAt': repost_at.isoformat()}),
            ]
            for source, title, published, verified, data in cases:
                db.add(NewsItem(source_id=source.id, title=title, url=f'https://example.org/{uuid4()}',
                    published_at=published, publication_time_verified=verified, linkedin_data=data,
                    created_at=cutoff + timedelta(minutes=1)))
            await db.commit()

            assert await service.process_scheduled_digest(db, cycle, user, cutoff + timedelta(minutes=2)) == 2
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            titles = await db.scalars(select(NewsItem.title).where(
                NewsItem.id.in_([UUID(value) for value in report.content['item_ids']])))
            assert set(titles) == {'Article at cutoff', 'Recent verified repost'}
            assert len(report.content['archive_ids']) == 4
            assert 'Arkivi i importuar (4)' in report.content['body']
            assert 'Pas' not in report.content['subject']
            send.assert_awaited_once()

    asyncio.run(run())


def test_discovered_post_survives_report_claim_and_is_not_sent_again(database, monkeypatch):
    from unittest.mock import AsyncMock

    _, sessions = database
    send = AsyncMock()
    monkeypatch.setattr(service, '_send_digest', send)

    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            morning = await db.get(CollectionCycle, cid)
            assert await service.process_scheduled_digest(db, morning, user, AM + timedelta(minutes=2)) == 2
            source = await db.scalar(select(NewsSource).where(NewsSource.type == 'LINKEDIN'))
            unknown = NewsItem(source_id=source.id, title='Post discovered in the evening',
                url='https://www.linkedin.com/posts/new', created_at=PM + timedelta(minutes=8))
            late = NewsItem(source_id=source.id, title='Verified post from the missed morning interval',
                url='https://www.linkedin.com/posts/late', published_at=AM - timedelta(hours=1),
                created_at=AM + timedelta(minutes=3), publication_time_verified=True)
            evening = CollectionCycle(trial_date=PM.date(), phase='scheduled-pm', cutoff_at=PM,
                deadline_at=PM + timedelta(minutes=30), state='finished', inventory_frozen=True)
            db.add_all([unknown, late, evening])
            await db.commit()

            await service.process_scheduled_digest(db, evening, user, PM + timedelta(minutes=10))
            report = await db.scalar(select(ScheduledDigestReport).where(ScheduledDigestReport.cycle_id == evening.id))
            assert 'Zbuluar që nga raporti i fundit' in report.content['body']
            assert 'Post discovered in the evening' in report.content['body']
            assert 'Zbuluar me vonesë' in report.content['body']
            assert 'Verified post from the missed morning interval' in report.content['body']
            assert '1 aktivitet i ri' in report.content['subject']

            following = AM + timedelta(days=1)
            unknown.published_at = PM + timedelta(hours=1)
            unknown.publication_time_verified = True
            cycle = CollectionCycle(trial_date=following.date(), phase='scheduled-am', cutoff_at=following,
                deadline_at=following + timedelta(minutes=30), state='finished', inventory_frozen=True)
            db.add(cycle)
            await db.commit()
            assert await service.process_scheduled_digest(db, cycle, user, following + timedelta(minutes=2)) == 0
            assert 'Post discovered in the evening' not in send.await_args.args[1]
            assert 'Verified post from the missed morning interval' not in send.await_args.args[1]

    asyncio.run(run())


def test_confirmed_cross_platform_copies_share_a_delivery_key():
    message = 'A detailed announcement from the same organization about a specific public event, its venue and registration: https://example.org/events/open-day'
    facebook = NewsSource(name='Example - Facebook', type='FACEBOOK', url='https://www.facebook.com/example')
    linkedin = NewsSource(name='Example - LinkedIn', type='LINKEDIN', url='https://www.linkedin.com/company/example')
    first = NewsItem(url='https://www.facebook.com/example/posts/01', original_text=message)
    copy = NewsItem(url='https://www.linkedin.com/posts/example-02', original_text=message)
    assert service.digest_identities(first, facebook, {'example'}) & service.digest_identities(copy, linkedin, {'example'})
    copy.original_text += ' Additional activities are scheduled for Friday.'
    assert not service.digest_identities(first, facebook, {'example'}) & service.digest_identities(copy, linkedin, {'example'})


@pytest.mark.parametrize('smtp_error', [False, True])
def test_concurrent_report_workers_and_uncertain_smtp_never_resend(database, monkeypatch, smtp_error):
    _, sessions = database
    calls = []
    async def send(*content, **kwargs):
        calls.append(content)
        await asyncio.sleep(0.05)
        if smtp_error:
            raise ConnectionError('SMTP acceptance unknown')
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
        async def worker():
            async with sessions() as db:
                return await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=2))
        results = await asyncio.gather(worker(), worker())
        assert len(calls) == 1
        assert sum(results) == (0 if smtp_error else 2)
        assert await worker() == 0
        async with sessions() as db:
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state == ('uncertain' if smtp_error else 'sent')
    asyncio.run(run())


@pytest.mark.parametrize('empty', [False, True])
def test_morning_pdf_is_sent_once_even_without_news(database, monkeypatch, empty):
    from app.mail import EmailAttachment
    from unittest.mock import AsyncMock
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    attachment = EmailAttachment('GA-Time-Table-2026-10-20.pdf', b'%PDF-test')
    excel = EmailAttachment('GA-Time-Table-2026-10-20.xlsx', b'xlsx-test', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    export = AsyncMock(return_value=[attachment, excel])
    send = AsyncMock()
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    if empty:
        monkeypatch.setattr(service, 'scheduled_entries', AsyncMock(return_value=[]))
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
        for _ in range(2):
            async with sessions() as db:
                await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=2))
        assert send.await_count == export.await_count == 1
        assert send.await_args.kwargs['attachments'] == [attachment, excel]
        assert export.await_args.args == ('scheduled-am', AM)
        async with sessions() as db:
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state == 'sent'
            assert report.content['attachments'][0]['filename'] == attachment.filename
            assert report.content['attachments'][1]['filename'] == excel.filename
            assert len(report.content['item_ids']) == (0 if empty else 2)
            assert (await db.get(NewsDigestSettings, user)).last_sent_at is not None
    asyncio.run(run())


@pytest.mark.parametrize('phase,cutoff', [('scheduled-am', AM), ('scheduled-pm', PM)])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('smtp_error', [False, True])
def test_unavailable_timetable_sends_news_without_attachments(database, monkeypatch, phase, cutoff, empty, smtp_error):
    from sqlalchemy import delete
    from app.intelligence.models import NewsDigestDelivery
    from unittest.mock import AsyncMock
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    export = AsyncMock(side_effect=RuntimeError('private export failure details'))
    send = AsyncMock(side_effect=ConnectionError('SMTP acceptance unknown') if smtp_error else None)
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            cycle = await db.get(CollectionCycle, cid)
            cycle.phase, cycle.cutoff_at = phase, cutoff
            cycle.deadline_at = cutoff + timedelta(minutes=30)
            if empty:
                await db.execute(delete(NewsItem))
            else:
                future = await db.scalar(select(NewsItem).where(NewsItem.title == 'Future news'))
                future.published_at = cutoff + timedelta(hours=1)
                eligible = await db.scalars(select(NewsItem).where(NewsItem.title.in_(['New social post', 'New RSS news'])))
                for item in eligible:
                    item.published_at = cutoff - timedelta(minutes=10)
            await db.commit()
        now = cutoff + timedelta(minutes=2)
        for minute in (2, 3):
            async with sessions() as db:
                await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user,
                    cutoff + timedelta(minutes=minute))
        assert send.await_count == 1
        export.assert_awaited_once_with(phase, cutoff)
        assert 'attachments' not in send.await_args.kwargs
        async with sessions() as db:
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state == ('uncertain' if smtp_error else 'sent')
            assert len(report.content['item_ids']) == (0 if empty else 2)
            assert 'attachments' not in report.content
            warning = report.content['attachment_warning']
            assert warning in send.await_args.args[1] and warning in send.await_args.args[2]
            assert 'private export failure details' not in warning
            assert len(list(await db.scalars(select(NewsDigestDelivery)))) == (0 if empty or smtp_error else 4)
            digest = await db.get(NewsDigestSettings, user)
            assert digest.last_slot_at == (service.previous_slot(AM) if smtp_error else cutoff)
            assert digest.last_sent_at == (None if smtp_error else now)
    asyncio.run(run())


def test_uncertain_morning_smtp_retains_pdf_claim_and_never_resends(database, monkeypatch):
    from app.mail import EmailAttachment
    from unittest.mock import AsyncMock
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    export = AsyncMock(return_value=[EmailAttachment('GA.pdf', b'%PDF-test')])
    send = AsyncMock(side_effect=ConnectionError('SMTP acceptance unknown'))
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
        for minute in (2, 3):
            async with sessions() as db:
                await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=minute))
        assert send.await_count == export.await_count == 1
        async with sessions() as db:
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state == 'uncertain' and report.content['attachments'][0]['filename'] == 'GA.pdf'
    asyncio.run(run())


@pytest.mark.parametrize('empty',[False,True])
def test_evening_report_attaches_both_files_once(database, monkeypatch, empty):
    from unittest.mock import AsyncMock
    from app.mail import EmailAttachment
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    attachments=[EmailAttachment('GA.pdf',b'%PDF-test'),EmailAttachment('GA.xlsx',b'xlsx-test',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')]
    export, send = AsyncMock(return_value=attachments), AsyncMock()
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    if empty:
        monkeypatch.setattr(service,'scheduled_entries',AsyncMock(return_value=[]))
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            cycle = await db.get(CollectionCycle, cid)
            cycle.phase, cycle.cutoff_at = 'scheduled-pm', PM
            cycle.deadline_at = PM + timedelta(minutes=30)
            await db.commit()
            await service.process_scheduled_digest(db, cycle, user, PM + timedelta(minutes=2))
            await service.process_scheduled_digest(db, cycle, user, PM + timedelta(minutes=3))
        assert send.await_count == 1
        assert send.await_args.kwargs['attachments'] == attachments
        export.assert_awaited_once_with('scheduled-pm',PM)
        async with sessions() as db:
            report=(await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state=='sent' and len(report.content['attachments'])==2
            if empty:
                assert report.content['subject'].endswith('21:00')
    asyncio.run(run())


def test_empty_reports_and_unsubscribed_updates_do_not_send(database, monkeypatch):
    from sqlalchemy import delete
    _, sessions = database
    async def send(*content, **kwargs):
        pytest.fail('No email authorized without new subscribed updates')
    monkeypatch.setattr(service, '_send_digest', send)
    original = service.scheduled_entries
    async def select_then_unsubscribe(db, user, *args, **kwargs):
        entries = await original(db, user, *args, **kwargs)
        async with sessions() as other:
            await other.execute(delete(NewsSourceEmailSubscription))
            await other.commit()
        return entries
    monkeypatch.setattr(service, 'scheduled_entries', select_then_unsubscribe)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            assert await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=2)) == 0
            report = (await db.scalars(select(ScheduledDigestReport))).one()
            assert report.state == 'empty' and report.sent_at is None
    asyncio.run(run())


def test_email_settings_expose_fixed_times_and_block_arbitrary_schedule(monkeypatch):
    from app.intelligence import digest_router
    from fastapi import HTTPException
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.intelligence.schemas import DigestSettingsInput
    digest = SimpleNamespace(times=['12:30'], last_sent_at=None, last_error=None, last_slot_at=AM)
    assert digest_router._settings_out(digest, None)['times'] == ['07:00', '21:00']
    assert digest_router._settings_out(digest, None)['scheduleManaged'] is True
    monkeypatch.setattr(digest_router, 'ensure_digest_settings', AsyncMock(return_value=digest))
    with pytest.raises(HTTPException) as error:
        asyncio.run(digest_router.update_digest_settings(DigestSettingsInput(times=['12:30']),
            db=SimpleNamespace(), user=User(id=ADMIN_ID, email="test@example.com")))
    assert error.value.status_code == 422
    output = asyncio.run(digest_router.update_digest_settings(DigestSettingsInput(times=['07:00', '21:00']),
        db=SimpleNamespace(commit=AsyncMock()), user=User(id=ADMIN_ID, email="test@example.com")))
    assert output['times'] == ['07:00', '21:00'] and digest.last_slot_at == AM
