"""Reviewable repair of known collection pauses and account-bound historical cycles.

Only billing GETs are permitted. This command cannot trigger collection or SMTP.
The caller supplies the audited cycle UUID; unrelated/old-account reservations remain.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.db import AppMetadata, SessionLocal, engine
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
from app.intelligence.collection_exception import SATURATION_PAUSE
from app.intelligence.credit_control import budget_status, verified_scope
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsSource
from app.intelligence.trial_usage import refresh_shared_billing
from app.intelligence.provider_outcomes import retryable_read_error
from app.config import settings

GENERIC_WARNING = 'Snapshot contains invalid, unattributed or out-of-window activity; review coverage before resuming.'
NO_RESULTS = {'Posts for the specified period were not found', 'Activities are not found'}


async def repair_sources(db):
    changes=[]
    for source in await db.scalars(select(NewsSource).where(NewsSource.status=='ACTIVE',
            NewsSource.type.in_(['LINKEDIN','FACEBOOK'])).with_for_update()):
        if source.pending_execution_id or source.pending_snapshot_id:
            continue
        latest=await db.scalar(select(BrightDataExecution).where(BrightDataExecution.source_id==source.id)
            .order_by(BrightDataExecution.started_at.desc()).limit(1))
        if not latest or latest.state not in {'completed','partial'}:
            continue
        prior=source.linkedin_paused_reason
        if prior not in {SATURATION_PAUSE, GENERIC_WARNING}:
            continue
        if latest.api_errors and 'private group' in latest.api_errors.casefold():
            source.linkedin_paused_reason='This Facebook group is private; use an accessible public source.'
        elif latest.api_errors and retryable_read_error(latest.api_errors):
            source.linkedin_paused_reason=None
            source.last_error='Provider returned no confirmed results; interval coverage remains unconfirmed.'
        elif (prior==SATURATION_PAUSE and settings.NEWS_SOCIAL_ADAPTIVE_LIMITS
                and latest.returned_records==latest.record_limit
                and latest.record_limit<settings.NEWS_SOCIAL_MAX_RECORD_LIMIT
                and not latest.api_errors and not latest.coverage_warning):
            source.linkedin_paused_reason=None
            source.last_error='Record limit reached; interval remains unconfirmed. The next daily request will use a larger cap.'
        elif latest.state=='completed' and (latest.returned_records or 0)<latest.record_limit:
            proven=await db.scalar(select(CollectionCycleSource.id).where(
                CollectionCycleSource.execution_id==latest.id,CollectionCycleSource.coverage_confirmed.is_(True)))
            if not proven:
                continue
            source.linkedin_paused_reason=None
            source.last_error=latest.coverage_warning
        elif prior==GENERIC_WARNING and latest.newly_saved_posts>0 and not latest.api_errors:
            # Parser already rejected untrusted rows. Retain the unconfirmed interval;
            # a future bounded daily discovery may resolve it without a permanent latch.
            source.linkedin_paused_reason=None
            source.last_error=GENERIC_WARNING
        else:
            continue
        changes.append({'source_id':str(source.id),'name':source.name,'prior_pause':prior,
            'pause':source.linkedin_paused_reason,'coverage_checkpoint_changed':False})
    return changes


async def run(args):
    now=datetime.now(timezone.utc)
    if args.apply and not args.approval_ref:
        raise ValueError('Applying the repair requires the owner approval reference.')
    key='collection-repair:'+args.approval_ref if args.approval_ref else None
    async with SessionLocal() as db:
        if key and (prior:=await db.get(AppMetadata,key)):
            return {'state':'already-applied','result':json.loads(prior.value)}
        await refresh_shared_billing(db,now)
        account=await db.get(BrightDataAccount,1)
        scope=verified_scope(account,now)
        if not scope:
            raise ValueError('A complete fresh dataset/account billing proof is required.')
        bound=[]
        if args.bind_cycle:
            if args.account_ref != scope['account_ref']:
                raise ValueError('The reviewed historical account reference does not match the fresh proof.')
            cycle=await db.get(CollectionCycle,args.bind_cycle)
            if not cycle or cycle.state!='finished':
                raise ValueError('The audited cycle is missing or unfinished.')
            executions=list(await db.scalars(select(BrightDataExecution).join(CollectionCycleSource,
                CollectionCycleSource.execution_id==BrightDataExecution.id).where(
                    CollectionCycleSource.cycle_id==cycle.id).with_for_update()))
            for execution in executions:
                if (not execution.snapshot_id or execution.state not in {'completed','partial'}
                        or not execution.completed_at or execution.completed_at>=account.usage_as_of
                        or execution.dataset_id not in scope['datasets']
                        or execution.budget_month!=scope['month']
                        or not cycle.cutoff_at<=execution.started_at<cycle.deadline_at
                        or execution.billing_account_ref not in {None,scope['account_ref']}):
                    raise ValueError('Historical execution is outside the reviewed settled account/cycle boundary.')
            for execution in executions:
                execution.billing_account_ref=scope['account_ref']
                bound.append(str(execution.id))
        changes=await repair_sources(db)
        await db.flush()
        result={'state':'applied' if args.apply else 'preview','capturedUtc':now.isoformat(),
            'boundExecutions':bound,'sourceChanges':changes,'budget':await budget_status(db),
            'providerTriggers':0,'smtpSends':0,'historicalReservationsRetained':True}
        if args.apply:
            db.add(AppMetadata(key=key,value=json.dumps(result,default=str)))
            await db.commit()
        else:
            await db.rollback()
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind-cycle',type=UUID)
    parser.add_argument('--account-ref')
    parser.add_argument('--approval-ref',default='')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    async def invoke():
        try:print(json.dumps(await run(args),default=str,indent=2))
        finally:await engine.dispose()
    asyncio.run(invoke())


if __name__=='__main__':main()
