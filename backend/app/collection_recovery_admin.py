"""Preview or authorize one bounded recovery of a missed closed UTC day.

Creates a durable inventory and a 30-minute permit. The existing coordinator alone
submits reserved jobs; repeating this command never creates replacement requests.
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select

from app.config import settings
from app.db import AppMetadata, SessionLocal, engine
from app.intelligence.collection_cycles import CollectionCycle, CollectionCycleSource
from app.intelligence.credit_control import budget_status
from app.intelligence.cycle_service import latest_verified_checkpoint, source_identity
from app.intelligence.facebook_identity import source_approved
from app.intelligence.models import NewsSource
from app.intelligence.social_limits import record_limit_for
from app.intelligence.trial_usage import refresh_shared_billing


async def run(args):
    now=datetime.now(timezone.utc)
    cutoff=datetime.fromisoformat(args.through.replace('Z','+00:00'))
    if (not cutoff.tzinfo or cutoff.utcoffset()!=timedelta(0) or cutoff.time().isoformat()!='00:00:00'
            or not cutoff<=now<cutoff+timedelta(days=7)):
        raise ValueError('Recovery requires a recent closed UTC-day cutoff.')
    if args.apply and not args.approval_ref:
        raise ValueError('Recovery requires explicit owner authorization.')
    async with SessionLocal() as db:
        previous=await db.scalar(select(CollectionCycle).where(CollectionCycle.trial_date==cutoff.date(),
            CollectionCycle.phase=='recovery-day'))
        if previous:
            return {'state':'already-authorized','cycle_id':str(previous.id),'providerTriggers':0}
        await refresh_shared_billing(db,now)
        budget=await budget_status(db)
        if budget.get('pausedReason'):
            raise ValueError('Resolve account billing before recovery: '+budget['pausedReason'])
        rows=[];reserved=0;source_limits={}
        sources=list(await db.scalars(select(NewsSource).where(NewsSource.status=='ACTIVE',
            NewsSource.type.in_(['LINKEDIN','FACEBOOK'])).order_by(NewsSource.id)))
        for source in sources:
            if (source.type=='FACEBOOK' and (not settings.NEWS_FACEBOOK_FETCH_ENABLED or not source_approved(source))
                    or source.pending_execution_id or source.pending_snapshot_id):
                continue
            if source.linkedin_paused_reason and not source.linkedin_paused_reason.startswith('Record limit reached;'):
                continue
            point=await latest_verified_checkpoint(db,source.id)
            if point and point>=cutoff:
                continue
            earliest=await db.scalar(select(func.min(CollectionCycleSource.window_start)).where(
                CollectionCycleSource.source_id==source.id,CollectionCycleSource.window_end<=cutoff,
                CollectionCycleSource.coverage_confirmed.is_(False)))
            start=point or earliest or cutoff-timedelta(days=1)
            if start<cutoff-timedelta(days=7):
                raise ValueError('An older unresolved source requires a separately reviewed historical interval.')
            limit=await record_limit_for(db,source)
            reserved+=limit
            source_limits[str(source.id)]=limit
            rows.append(CollectionCycleSource(source_id=source.id,name=source.name,type=source.type,
                canonical_url=source_identity(source),window_start=start,window_end=cutoff,
                verified_start=point is not None))
        if reserved>args.max_reserved or budget['effectiveRecords']+reserved>budget['operationalBudget']:
            raise ValueError('Recovery reservations exceed the reviewed or monthly cap.')
        cycle_id=uuid4()
        result={'state':'authorized' if args.apply else 'preview','cycle_id':str(cycle_id),
            'cutoff_at':cutoff.isoformat(),'sourceCount':len(rows),'maxReservedRecords':reserved,
            'budgetBefore':budget,'sources':[{'name':r.name,'type':r.type,
                'start':r.window_start.isoformat(),'end':r.window_end.isoformat()} for r in rows],
            'providerTriggers':0,'smtpSends':0}
        if args.apply:
            expiry=now+timedelta(minutes=30)
            db.add(CollectionCycle(id=cycle_id,trial_date=cutoff.date(),phase='recovery-day',
                cutoff_at=cutoff,deadline_at=expiry,state='running',inventory_frozen=True))
            for row in rows:
                row.cycle_id=cycle_id;db.add(row)
            permit={'approval_ref':args.approval_ref,'created_at':now.isoformat(),
                'expires_at':expiry.isoformat(),'cutoff_at':cutoff.isoformat(),
                'source_ids':[str(row.source_id) for row in rows],'max_reserved':reserved}
            permit['source_limits']=source_limits
            db.add(AppMetadata(key='collection-recovery:'+str(cycle_id),value=json.dumps(permit)))
            await db.commit()
        else:
            await db.rollback()
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through',required=True)
    parser.add_argument('--max-reserved',type=int,default=800)
    parser.add_argument('--approval-ref',default='')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    async def invoke():
        try:print(json.dumps(await run(args),default=str,indent=2))
        finally:await engine.dispose()
    asyncio.run(invoke())


if __name__=='__main__':main()
