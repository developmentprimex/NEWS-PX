"""Real coordinator/ledger/storage/digest; only HTTP, RSS and SMTP are simulated."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.auth import ADMIN_ID
from app.config import settings
from app.intelligence import collection, cycle_service, cycle_digest, trial_usage
from app import collection_trial_admin
from app.intelligence.collection_cycles import CollectionCycleReport, TrialUsageObservation
from app.intelligence.models import NewsSource, NewsSourceEmailSubscription, BrightDataAccount
from app.intelligence.services import CollectedNewsItem
from test_brightdata_postgres_integration import Provider, database, private_postgres
from test_collection_trial_usage import fixture as billing_fixture


def test_morning_to_evening_fetch_storage_billing_and_single_report(database, monkeypatch, tmp_path):
    _, sessions = database
    morning = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
    clock = {'now': morning}
    provider = Provider(monkeypatch, records=[{'id': '42', 'url': 'https://www.linkedin.com/posts/example_activity-42-abc',
        'post_text': 'Morning item', 'date_posted': '2026-10-19T05:59:00Z'}])
    binding, _ = billing_fixture()
    binding_file = tmp_path/'binding.json'
    binding_file.write_text(json.dumps(binding))
    monkeypatch.setattr(settings, 'NEWS_TRIAL_BILLING_BINDING_FILE', str(binding_file))
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_USER', 'sender@example.com')
    monkeypatch.setattr(settings, 'NEWS_TRIAL_EMAIL_PASSWORD', 'test-only')
    async def billing(request):
        _, responses = billing_fixture()
        usage = responses['usage']['test-account']
        usage['last_update_ts'] = (clock['now']-timedelta(minutes=1)).isoformat()
        count = sum(1 if index == 0 else 2 for index, _ in enumerate(provider.triggers))
        usage['data']['test-resource']['rows_initial_billable'][-1] = count
        for totals in usage['sums']['test-resource'].values(): totals['rows_initial_billable'] = count
        name = {'/status': 'status', '/customer/balance': 'balance', '/pricing_plans': 'pricing', '/customer/bw': 'usage'}[request.url.path]
        assert request.method == 'GET'
        return httpx.Response(200, json=responses[name])
    monkeypatch.setattr(trial_usage, 'billing_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(billing)))
    async def rss(_):
        afternoon = clock['now'].hour >= 19
        return [CollectedNewsItem(external_id='rss-pm' if afternoon else 'rss-am', url='https://example.com/'+('pm' if afternoon else 'am'),
            title='Afternoon RSS' if afternoon else 'Morning RSS', original_text='Test news',
            published_at=morning+timedelta(hours=8) if afternoon else morning-timedelta(minutes=1))]
    monkeypatch.setattr(collection.RSSAdapter, 'collect', rss)
    sent = []
    async def send(*content): sent.append(content)
    monkeypatch.setattr(cycle_digest, '_send_trial_report', send)
    async def run():
        async with sessions() as db:
            rows = [NewsSource(name='Example', type='LINKEDIN', url='https://www.linkedin.com/company/example/', status='ACTIVE'),
                    NewsSource(name='RSS', type='RSS', url='https://example.com/feed', status='ACTIVE')]
            db.add_all(rows)
            await db.flush()
            db.add_all([NewsSourceEmailSubscription(source_id=s.id, user_id=ADMIN_ID, enabled_at=morning-timedelta(days=2)) for s in rows])
            account = await db.get(BrightDataAccount, 1)
            account.provider_month = '2026-10'
            account.verified_at = account.usage_as_of = morning-timedelta(minutes=2)
            await db.commit()
            await collection_trial_admin.activate_trial(db, approval_ref='offline-approved-trial', now=morning)
            await cycle_service.accept_manual_cycle(db, user_id=ADMIN_ID, now=morning)
        for minute in [0, 1]:
            clock['now'] = morning+timedelta(minutes=minute)
            await cycle_service.run_collection_trial_cycle(clock['now'])
        assert len(provider.triggers) == 1 and not sent
        provider.records = [
            {'id': '43', 'url': 'https://www.linkedin.com/posts/example_activity-43-abc', 'post_text': 'Afternoon LinkedIn', 'date_posted': '2026-10-19T15:00:00Z'},
            {'id': '44', 'url': 'https://www.linkedin.com/posts/example_activity-44-abc', 'post_text': 'Unknown repost time', 'post_type': 'repost', 'date_posted': '2026-10-19T16:00:00Z'}]
        for minute in [0, 2, 3]:
            clock['now'] = morning.replace(hour=19)+timedelta(minutes=minute)
            await cycle_service.run_collection_trial_cycle(clock['now'])
        assert len(provider.triggers) == 2
        assert len(sent) == 1
        assert 'Afternoon LinkedIn' in sent[0][1] and 'Afternoon RSS' in sent[0][1]
        assert 'Morning item' not in sent[0][1] and 'Morning RSS' not in sent[0][1]
        assert 'Unknown repost time' not in sent[0][1]
        async with sessions() as db:
            assert (await db.scalars(select(CollectionCycleReport))).one().state == 'sent'
            labels = set(await db.scalars(select(TrialUsageObservation.label)))
            assert {'baseline', 'before_manual', 'after_manual', 'before_evening', 'after_evening'} <= labels
            cost = await trial_usage.trial_usage_summary(db, morning.date())
            assert cost['reservedRecords'] == 12 and cost['postCount'] == 2
    asyncio.run(run())
