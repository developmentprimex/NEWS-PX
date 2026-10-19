"""Operator-only one-day activation. No command triggers scraping or sends mail."""
import argparse
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select, text

from app.config import settings
from app.db import AppMetadata, SessionLocal
from app.intelligence.cycle_service import TRIAL_KEY, trial_configuration, trial_date_for_activation, cycle_status
from app.intelligence.cycle_digest import recipient_fingerprint
from app.intelligence.credit_control import locked_account, denial_reason, executions_for_budget, usage_totals
from app.intelligence.models import NewsSource, NewsSourceEmailSubscription
from app.intelligence.trial_usage import capture_trial_usage, record_usage_observation, projected_reservation, trial_usage_summary


async def activate_trial(db, *, approval_ref, now):
    from app.auth import ADMIN_ID
    from app.intelligence import collection
    if not approval_ref or len(approval_ref) > 200:
        raise ValueError('An explicit one-day trial approval reference is required.')
    if not settings.NEWS_ENABLE_COLLECTION or not settings.NEWS_LINKEDIN_FETCH_ENABLED or settings.NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED:
        raise ValueError('Protected normal collection must be available; canary mode must be off.')
    if not settings.NEWS_TRIAL_EMAIL_USER or not settings.NEWS_TRIAL_EMAIL_PASSWORD or '@' not in settings.NEWS_EMAIL_RECIPIENT:
        raise ValueError('Private trial sender and recipient must be configured before activation.')
    day = trial_date_for_activation(now)
    async with collection.engine.connect() as lock:
        await lock.execute(text('SELECT pg_advisory_lock(7302192100)'))
        try:
            config = await trial_configuration(db)
            if config.get('date') and config['date'] != day.isoformat():
                raise ValueError('A previous trial exists; review it before authorizing a different date.')
            if config.get('enabled'):
                return {'enabled': True, 'date': config['date'], 'alreadyActivated': True}
            subscriptions = list(await db.scalars(select(NewsSourceEmailSubscription.source_id).join(NewsSource,
                NewsSource.id == NewsSourceEmailSubscription.source_id).where(
                NewsSourceEmailSubscription.user_id == ADMIN_ID, NewsSource.status == 'ACTIVE')))
            if not subscriptions:
                raise ValueError('No active sources have Email on for the configured user.')
            record = await db.get(AppMetadata, TRIAL_KEY)
            if record is None:
                record = AppMetadata(key=TRIAL_KEY)
                db.add(record)
            config = {'enabled': False, 'date': day.isoformat(), 'user_id': str(ADMIN_ID),
                'recipient_fingerprint': recipient_fingerprint(settings.NEWS_EMAIL_RECIPIENT), 'approval_ref': approval_ref}
            record.value = json.dumps(config)
            await db.commit()
            await capture_trial_usage(db, label='baseline', now=now)
            account = await locked_account(db)
            totals = usage_totals(account, await executions_for_budget(db), now)
            reason = denial_reason(account, totals, now, settings.NEWS_LINKEDIN_RECORD_LIMIT)
            profiles = len(list(await db.scalars(select(NewsSource.id).where(NewsSource.type == 'LINKEDIN', NewsSource.status == 'ACTIVE'))))
            required = projected_reservation(profiles)
            if (reason or totals['dailyRecords'] + required > settings.NEWS_LINKEDIN_DAILY_TARGET
                    or totals['effectiveRecords'] + required > totals['operationalBudget']):
                await db.rollback()
                raise ValueError(reason or 'The full two-phase inventory does not fit the remaining trial budget.')
            config['enabled'] = True
            record.value = json.dumps(config)
            await db.commit()
            return {'enabled': True, 'date': day.isoformat(), 'profiles': profiles, 'projectedReservedRecords': required}
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(7302192100)'))


async def stop_trial(db):
    record = await db.get(AppMetadata, TRIAL_KEY)
    if record:
        config = json.loads(record.value)
        config['enabled'] = False
        record.value = json.dumps(config)
        await db.commit()
    return {'enabled': False, 'reservationsRetained': True}


async def run(args):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        if args.command == 'activate':
            result = await activate_trial(db, approval_ref=args.approval_ref, now=now)
        elif args.command == 'stop':
            result = await stop_trial(db)
        elif args.command == 'usage-import':
            evidence = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            await record_usage_observation(db, label=args.label, evidence=evidence, now=now)
            result = {'imported': True, 'providerTriggers': 0}
        else:
            config = await trial_configuration(db)
            result = {'enabled': config.get('enabled', False), 'date': config.get('date'), 'cycle': await cycle_status(db)}
            if config.get('date'):
                result['usage'] = await trial_usage_summary(db, date.fromisoformat(config['date']))
        print(json.dumps(result, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    activate = commands.add_parser('activate')
    activate.add_argument('--approval-ref', required=True)
    commands.add_parser('status')
    commands.add_parser('stop')
    usage = commands.add_parser('usage-import')
    usage.add_argument('--label', required=True)
    usage.add_argument('--file', required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:
        # HTTP exceptions and malformed input can carry private headers/contents.
        parser.exit(1, 'Trial operation failed; review the private configuration and billing evidence.\n')


if __name__ == '__main__':
    main()
