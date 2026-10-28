"""Recurring real-ledger scheduling; provider HTTP, RSS and SMTP remain mocked."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

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
    async def send(*content):
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


def test_facebook_also_runs_only_in_two_slots_and_websites_keep_periodic_checks(database, monkeypatch):
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
        assert not reads  # Social slots do not force extra website/RSS checks.
        await collection.check_due_sources()
        assert reads == ['https://example.com/feed']
        assert len(provider.triggers) == 2
        async with sessions() as db:
            assert set(await db.scalars(select(CollectionCycleSource.type))) == {'FACEBOOK'}
    asyncio.run(run())


async def prepare_report(db, *, pending=False):
    user = uuid4()
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
        published_at=AM + timedelta(hours=1), created_at=AM + timedelta(minutes=1)))
    db.add(NewsItem(source_id=active.id, title='Unverified social time', url='https://example.org/unknown',
        published_at=AM - timedelta(minutes=5), created_at=AM + timedelta(minutes=1),
        linkedin_data={'kind': 'REPOST'}, publication_time_verified=False))
    await db.commit()
    return user, cycle.id


def test_combined_reports_wait_for_collection_and_send_once_per_slot(database, monkeypatch):
    _, sessions = database
    sent = []
    async def send(*content):
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


@pytest.mark.parametrize('smtp_error', [False, True])
def test_concurrent_report_workers_and_uncertain_smtp_never_resend(database, monkeypatch, smtp_error):
    _, sessions = database
    calls = []
    async def send(*content):
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


def test_failed_pdf_retries_before_claim_without_losing_news(database, monkeypatch):
    from app.mail import EmailAttachment
    from unittest.mock import AsyncMock
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    export = AsyncMock(side_effect=[RuntimeError('PrimeFlow unavailable'), [EmailAttachment('GA.pdf', b'%PDF-test')]])
    send = AsyncMock()
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=2))
            assert send.await_count == 0
            assert list(await db.scalars(select(ScheduledDigestReport))) == []
            digest = await db.get(NewsDigestSettings, user)
            assert digest.last_slot_at == service.previous_slot(AM)
            assert 'waiting' in digest.last_error
        async with sessions() as db:
            assert await service.process_scheduled_digest(db, await db.get(CollectionCycle, cid), user, AM + timedelta(minutes=3)) == 2
        assert send.await_count == 1
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


def test_evening_report_does_not_export_pdf_when_morning_feature_enabled(database, monkeypatch):
    from unittest.mock import AsyncMock
    _, sessions = database
    monkeypatch.setattr(settings, 'NEWS_GA_TIMETABLE_BINDING_FILE', 'private-binding')
    export, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, 'morning_attachments', export)
    monkeypatch.setattr(service, '_send_digest', send)
    async def run():
        async with sessions() as db:
            user, cid = await prepare_report(db)
            cycle = await db.get(CollectionCycle, cid)
            cycle.phase, cycle.cutoff_at = 'scheduled-pm', PM
            cycle.deadline_at = PM + timedelta(minutes=30)
            await db.commit()
            await service.process_scheduled_digest(db, cycle, user, PM + timedelta(minutes=2))
        assert send.await_count == 1
        assert send.await_args.kwargs == {}
        export.assert_not_awaited()
    asyncio.run(run())


def test_empty_reports_and_unsubscribed_updates_do_not_send(database, monkeypatch):
    from sqlalchemy import delete
    _, sessions = database
    async def send(*content):
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
            db=SimpleNamespace(), user=SimpleNamespace(id=uuid4())))
    assert error.value.status_code == 422
    output = asyncio.run(digest_router.update_digest_settings(DigestSettingsInput(times=['07:00', '21:00']),
        db=SimpleNamespace(commit=AsyncMock()), user=SimpleNamespace(id=uuid4())))
    assert output['times'] == ['07:00', '21:00'] and digest.last_slot_at == AM
