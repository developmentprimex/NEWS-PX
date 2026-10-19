import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import AppMetadata
from app.intelligence import cycle_digest as service
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, CollectionCycleReport
from app.intelligence.cycle_service import TRIAL_KEY
from app.intelligence.models import NewsItem, NewsSource, NewsSourceEmailSubscription
from test_brightdata_postgres_integration import database, private_postgres

START = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)  # 08:00 local
END = datetime(2026, 10, 19, 19, tzinfo=timezone.utc)


async def prepare(db, *, state='finished'):
    user = uuid4()
    config = {'enabled': True, 'date': '2026-10-19', 'user_id': str(user),
              'recipient_fingerprint': service.recipient_fingerprint(settings.NEWS_EMAIL_RECIPIENT)}
    db.add(AppMetadata(key=TRIAL_KEY, value=json.dumps(config)))
    src = NewsSource(name='Selected & trusted', type='LINKEDIN', url='https://linkedin.com/company/test/', status='ACTIVE')
    cycle = CollectionCycle(trial_date=date(2026, 10, 19), phase='evening', cutoff_at=END,
                            deadline_at=END+timedelta(minutes=30), state=state, inventory_frozen=True)
    db.add_all([src, cycle])
    await db.flush()
    db.add(CollectionCycleSource(cycle_id=cycle.id, source_id=src.id, name=src.name, type=src.type,
        canonical_url='https://www.linkedin.com/company/test/', window_start=START, window_end=END,
        verified_start=True, state='completed' if state == 'finished' else 'running',
        coverage_confirmed=state == 'finished'))
    db.add(NewsSourceEmailSubscription(source_id=src.id, user_id=user, enabled_at=START-timedelta(days=1)))
    def item(title, published, created=END+timedelta(minutes=2), kind='POST'):
        db.add(NewsItem(source_id=src.id, title=title, url='https://example.com/'+str(uuid4()),
            published_at=published, created_at=created, linkedin_data={'kind': kind},
            publication_time_verified=kind != 'REPOST'))
    item('Morning post', START)
    item('Afternoon post', START+timedelta(seconds=1))
    item('At 21:00, stored 21:02', END)
    item('Future post', END+timedelta(seconds=1))
    item('Old downloaded post', START-timedelta(days=1))
    item('Unknown repost time', END-timedelta(hours=1), kind='REPOST')
    await db.commit()
    return cycle.id, user, src.id


def credentials(monkeypatch):
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_PASSWORD', 'test-only')


def test_evening_selection_uses_activity_time_and_allows_late_insert(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    sent = []
    async def send(*content):
        sent.append(content)
    monkeypatch.setattr(service, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            cid, user, _ = await prepare(db)
            entries = await service.trial_digest_entries(db, cid, user)
            assert {entry['item'].title for entry in entries} == {'Afternoon post', 'At 21:00, stored 21:02'}
        async with sessions() as db:
            assert await service.process_trial_digest(db, cid, END+timedelta(minutes=2)) == 2
        async with sessions() as db:
            assert await service.process_trial_digest(db, cid, END+timedelta(days=1)) == 0
        assert len(sent) == 1
        assert 'Morning post' not in sent[0][1]
        assert 'Selected &amp; trusted' in sent[0][2]
    asyncio.run(run())


def test_report_waits_for_collection_but_deadline_reports_pending(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    sent = []
    async def send(*content): sent.append(content)
    monkeypatch.setattr(service, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            cid, _, _ = await prepare(db, state='running')
            assert await service.process_trial_digest(db, cid, END+timedelta(minutes=2)) == 0
            assert not sent
        async with sessions() as db:
            await service.process_trial_digest(db, cid, END+timedelta(minutes=30))
        assert len(sent) == 1
        assert 'paplot' in sent[0][1] or 'proces' in sent[0][1]
    asyncio.run(run())


def test_uncertain_smtp_is_never_sent_again(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    calls = []
    async def crash(*content):
        calls.append(content)
        raise SystemExit('simulated crash after SMTP acceptance')
    monkeypatch.setattr(service, '_send_trial_report', crash)
    async def run():
        async with sessions() as db:
            cid, _, _ = await prepare(db)
        with pytest.raises(SystemExit):
            async with sessions() as db:
                await service.process_trial_digest(db, cid, END+timedelta(minutes=2))
        async with sessions() as db:
            assert await service.process_trial_digest(db, cid, END+timedelta(minutes=3)) == 0
            report = (await db.scalars(select(CollectionCycleReport))).one()
            assert report.state in {'sending', 'uncertain'}
        assert len(calls) == 1
    asyncio.run(run())


def test_removed_subscription_and_recipient_change_block_report(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    async def fail_send(*content): raise AssertionError('No SMTP authorized')
    monkeypatch.setattr(service, '_send_trial_report', fail_send)
    async def run():
        async with sessions() as db:
            cid, user, sid = await prepare(db)
            await db.delete(await db.get(NewsSourceEmailSubscription, sid))
            await db.commit()
            assert await service.trial_digest_entries(db, cid, user) == []
            monkeypatch.setattr(settings, 'NEWS_EMAIL_RECIPIENT', 'changed@example.com')
            assert await service.process_trial_digest(db, cid, END+timedelta(minutes=2)) == 0
    asyncio.run(run())


def test_two_report_workers_send_once_and_honor_subscription_start(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    sent = []
    async def send(*content):
        sent.append(content)
        await asyncio.sleep(0.05)
    monkeypatch.setattr(service, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            cid, user, sid = await prepare(db)
            subscription = await db.get(NewsSourceEmailSubscription, sid)
            subscription.enabled_at = START+timedelta(hours=2)
            await db.commit()
        async def process():
            async with sessions() as db:
                return await service.process_trial_digest(db, cid, END+timedelta(minutes=2))
        assert sum(await asyncio.gather(process(), process())) == 1
        assert len(sent) == 1
        assert 'Afternoon post' not in sent[0][1]
        assert 'At 21:00, stored 21:02' in sent[0][1]
    asyncio.run(run())


def test_trial_credentials_do_not_enable_legacy_digest(monkeypatch):
    from app.intelligence import digest_service
    credentials(monkeypatch)
    monkeypatch.setattr(settings, 'EMAIL_USER', 'legacy@example.com')
    monkeypatch.setattr(settings, 'EMAIL_PASSWORD', 'legacy-test')
    def fail(): raise AssertionError('Legacy digest must remain disabled')
    monkeypatch.setattr(digest_service, 'SessionLocal', fail)
    asyncio.run(digest_service.run_digest_cycle())


def test_unsubscribe_during_selection_revokes_send(database, monkeypatch):
    _, sessions = database
    credentials(monkeypatch)
    selected = service.trial_digest_entries
    sent = []
    async def send(*content): sent.append(content)
    monkeypatch.setattr(service, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            cid, user, sid = await prepare(db)
        async def select_then_remove(db, cycle_id, user_id):
            entries = await selected(db, cycle_id, user_id)
            async with sessions() as other:
                await other.delete(await other.get(NewsSourceEmailSubscription, sid))
                await other.commit()
            return entries
        monkeypatch.setattr(service, 'trial_digest_entries', select_then_remove)
        async with sessions() as db:
            assert await service.process_trial_digest(db, cid, END+timedelta(minutes=2)) == 0
        assert not sent
    asyncio.run(run())


def test_report_deadline_runs_while_collection_worker_lock_is_held(database, monkeypatch):
    from app.intelligence import collection, cycle_service
    from sqlalchemy import text
    _, sessions = database
    credentials(monkeypatch)
    sent = []
    async def send(*content): sent.append(content)
    monkeypatch.setattr(service, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            cid, _, _ = await prepare(db, state='running')
        async with collection.engine.connect() as worker_lock:
            await worker_lock.execute(text('SELECT pg_advisory_lock(7302192100)'))
            try:
                await asyncio.wait_for(cycle_service.run_collection_trial_reports(END+timedelta(minutes=30)), timeout=5)
            finally:
                await worker_lock.execute(text('SELECT pg_advisory_unlock(7302192100)'))
        assert len(sent) == 1
    asyncio.run(run())
