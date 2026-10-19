"""GET-only usage capture and conservative, explicitly provisional trial costs."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import settings
from app.intelligence.billing_evidence import parse_billing_proof
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource, TrialUsageObservation
from app.intelligence.credit_control import locked_account
from app.intelligence.cycle_service import trial_configuration
from app.intelligence.models import BrightDataExecution

LABELS = {'baseline', 'before_manual', 'after_manual', 'before_evening', 'after_evening', 'settled'}
EVIDENCE_KEYS = {'account_ref', 'credential_sha256', 'consumers_isolated', 'month', 'usage_as_of',
                 'billable_records', 'verified_monthly_allowance', 'balance', 'active', 'evidence'}


def projected_reservation(profiles, phases=2):
    return profiles * phases * settings.NEWS_LINKEDIN_RECORD_LIMIT


def billing_client():
    return httpx.AsyncClient(timeout=20, follow_redirects=False)


def load_binding():
    if not settings.NEWS_TRIAL_BILLING_BINDING_FILE:
        raise ValueError('Private billing binding is required.')
    binding = json.loads(Path(settings.NEWS_TRIAL_BILLING_BINDING_FILE).read_text(encoding='utf-8-sig'))
    fingerprint = hashlib.sha256((settings.BRIGHTDATA_API_TOKEN or '').encode()).hexdigest()
    if binding.get('credentialSha256') != fingerprint:
        raise ValueError('Installed provider credential does not match the private binding.')
    return binding


async def read_billing_evidence(token, binding, now):
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    if fingerprint != binding['credentialSha256']:
        raise ValueError('Credential binding changed.')
    proof = {'credentialSha256': fingerprint, 'responses': {}}
    start, end = now.strftime('%Y-%m-01'), (now+timedelta(days=1)).strftime('%Y-%m-%d')
    paths = [('status', '/status'), ('balance', '/customer/balance'), ('pricing', '/pricing_plans'),
             ('usage', f'/customer/bw?from={start}&to={end}')]
    async with billing_client() as client:
        for name, path in paths:
            response = await client.get('https://api.brightdata.com'+path,
                headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json'})
            if response.status_code != 200:
                raise ValueError('Provider billing read failed: '+name)
            proof['responses'][name] = {'status': 200, 'data': response.json()}
    return parse_billing_proof(proof, binding, now)


async def trial_executions(db, day):
    return list(await db.scalars(select(BrightDataExecution).join(CollectionCycleSource,
        CollectionCycleSource.execution_id == BrightDataExecution.id).join(CollectionCycle,
        CollectionCycle.id == CollectionCycleSource.cycle_id).where(CollectionCycle.trial_date == day)))


def execution_counters(executions):
    return {'postCount': sum(e.snapshot_id is not None for e in executions),
        'uncertainPostCount': sum(e.snapshot_id is None for e in executions),
        'reservedRecords': sum(e.estimated_billable_usage for e in executions),
        'returnedRecords': sum(e.returned_records or 0 for e in executions),
        'duplicateRecords': sum(e.duplicate_posts for e in executions),
        'newItems': sum(e.newly_saved_posts for e in executions)}


async def record_usage_observation(db, *, label, evidence, now):
    from app.brightdata_admin import parse_evidence
    from datetime import date
    if label not in LABELS or set(evidence) - EVIDENCE_KEYS:
        raise ValueError('Unsupported observation label or private fields in billing evidence.')
    token = settings.BRIGHTDATA_API_TOKEN or ''
    if token and token in json.dumps(evidence):
        raise ValueError('Billing evidence must not contain credentials.')
    parse_evidence(evidence, now)
    config = await trial_configuration(db)
    if not config.get('date'):
        raise ValueError('Trial date is not configured.')
    day = date.fromisoformat(config['date'])
    record = TrialUsageObservation(trial_date=day, label=label, observed_at=now,
        evidence={**evidence, 'counters': execution_counters(await trial_executions(db, day))})
    db.add(record)
    await db.commit()
    return record.id


async def capture_trial_usage(db, *, label, now):
    binding = load_binding()
    evidence = await read_billing_evidence(settings.BRIGHTDATA_API_TOKEN or '', binding, now)
    cutoff = datetime.fromisoformat(evidence['usage_as_of'])
    account = await locked_account(db)
    if account.usage_as_of and cutoff < account.usage_as_of:
        raise ValueError('Cannot replace billing evidence with an older cutoff.')
    if account.provider_month == evidence['month'] and (account.provider_billable_records or 0) > evidence['billable_records']:
        raise ValueError('Provider usage cannot decrease within a month.')
    account.provider_month, account.provider_billable_records = evidence['month'], evidence['billable_records']
    account.provider_balance, account.provider_active = evidence['balance'], evidence['active']
    account.verified_monthly_allowance = evidence['verified_monthly_allowance']
    account.usage_as_of, account.verified_at, account.evidence = cutoff, now, evidence['evidence']
    # Existing pause latches are intentionally retained.
    return await record_usage_observation(db, label=label, evidence=evidence, now=now)


async def trial_usage_summary(db, trial_date):
    executions = await trial_executions(db, trial_date)
    observations = list(await db.scalars(select(TrialUsageObservation).where(
        TrialUsageObservation.trial_date == trial_date).order_by(TrialUsageObservation.observed_at, TrialUsageObservation.id)))
    result = {**execution_counters(executions), 'providerDeltaRecords': None, 'provisional': True,
              'attribution': 'uncertain', 'providerMonth': None, 'usageAsOf': None,
              'postCountBasis': 'confirmed snapshots; ambiguous reservations shown separately'}
    baseline = next((r for r in observations if r.label == 'baseline'), None)
    if not baseline or not observations:
        return result
    first, last = baseline.evidence, observations[-1].evidence
    result.update(providerMonth=last['month'], usageAsOf=last['usage_as_of'])
    if any(first.get(k) != last.get(k) for k in ('month', 'account_ref', 'credential_sha256')):
        result['attribution'] = 'incomparable'
        return result
    delta = last['billable_records'] - first['billable_records']
    if delta < 0:
        result['attribution'] = 'incomparable'
        return result
    result['providerDeltaRecords'] = delta
    isolated = first.get('consumers_isolated') is True and last.get('consumers_isolated') is True
    result['attribution'] = 'isolated' if isolated else 'uncertain'
    cutoff = datetime.fromisoformat(last['usage_as_of'])
    cycles = list(await db.scalars(select(CollectionCycle).where(CollectionCycle.trial_date == trial_date)))
    evening = next((cycle for cycle in cycles if cycle.phase == 'evening'), None)
    # A morning-only observation is never a completed day's cost.
    result['provisional'] = not (isolated and evening and evening.state == 'finished'
        and all(e.completed_at and e.completed_at < cutoff for e in executions)
        and any(r.label in {'after_evening', 'settled'} for r in observations)
        and not result['uncertainPostCount'])
    return result


async def record_settled_usage(db, now):
    """Retain fresh evidence already imported by the existing hourly observer."""
    from datetime import date
    from app.intelligence.models import BrightDataAccount
    config = await trial_configuration(db)
    if not config.get('date') or not settings.NEWS_TRIAL_BILLING_BINDING_FILE:
        return
    day = date.fromisoformat(config['date'])
    observations = list(await db.scalars(select(TrialUsageObservation).where(
        TrialUsageObservation.trial_date == day).order_by(TrialUsageObservation.observed_at)))
    if not observations or not any(r.label == 'after_evening' for r in observations):
        return
    account = await db.get(BrightDataAccount, 1)
    if not account or not account.usage_as_of or account.usage_as_of <= datetime.fromisoformat(observations[-1].evidence['usage_as_of']):
        return
    binding = load_binding()
    if not (account.evidence or '').startswith('Bound account '+binding['accountId']+';'):
        return
    if account.provider_billable_records is None or not account.provider_month or not account.verified_at or now-account.verified_at > timedelta(hours=24):
        return
    evidence = {'account_ref': binding['accountId'], 'credential_sha256': binding['credentialSha256'],
        'consumers_isolated': binding.get('consumersIsolated') is True, 'month': account.provider_month,
        'usage_as_of': account.usage_as_of.isoformat(), 'billable_records': account.provider_billable_records,
        'verified_monthly_allowance': account.verified_monthly_allowance, 'balance': float(account.provider_balance),
        'active': account.provider_active, 'evidence': account.evidence}
    await record_usage_observation(db, label='settled', evidence=evidence, now=now)
