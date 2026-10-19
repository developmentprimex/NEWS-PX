"""Bounded Facebook pilot operator. Never sends email or clears uncertainty."""
import argparse,asyncio,hashlib,json
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,text
from app.config import settings
from app.db import AppMetadata,SessionLocal,engine
from app.intelligence.facebook_identity import identity_or_none,source_kind,source_approved
from app.intelligence.models import NewsSource,BrightDataExecution
from app.intelligence.collection import protected_facebook_check

async def pilot(db,url,*,approval_ref,now):
    identity=identity_or_none(url)
    if not identity or not approval_ref or len(approval_ref)>160:raise ValueError('Direct source and owner pilot approval are required.')
    approved={identity_or_none(v) for v in settings.NEWS_FACEBOOK_APPROVED_SOURCE_URLS}
    if len(approved)>2 or identity not in approved:raise ValueError('Pilot is limited to two explicitly configured sources.')
    source=await db.scalar(select(NewsSource).where(NewsSource.type=='FACEBOOK',NewsSource.url==identity))
    if not source or not source_approved(source):raise ValueError('Verified pilot source is missing.')
    key='facebook_pilot_'+hashlib.sha256(identity.encode()).hexdigest()[:48]
    # Claim commits before the potentially billable path, including a crash before
    # reservation. Repeating the operator may only inspect/poll the same snapshot.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],'big',signed=True)})
    claim=await db.get(AppMetadata,key)
    if claim:
        await db.commit()
        if source.pending_snapshot_id:
            state=await protected_facebook_check(db,source,now,reason='trial-poll')
        else:state='already-consumed'
    else:
        claim=AppMetadata(key=key,value=json.dumps({'url':identity,'approval_ref':approval_ref,'claimed_at':now.isoformat()}))
        db.add(claim);await db.commit()
        day=now.astimezone(timezone.utc).date()
        state=await protected_facebook_check(db,source,now,reason='facebook-pilot:'+approval_ref,
            backfill=(day-timedelta(days=1),day+timedelta(days=1),1))
    await db.refresh(source)
    executions=list(await db.scalars(select(BrightDataExecution).where(BrightDataExecution.source_id==source.id).order_by(BrightDataExecution.started_at)))
    return {'source':source.name,'url':identity,'kind':source_kind(identity),'state':state,
        'pending':bool(source.pending_snapshot_id),'paused':bool(source.linkedin_paused_reason),
        'executions':[{k:getattr(e,k) for k in ('id','state','record_limit','returned_records','newly_saved_posts','estimated_billable_usage','snapshot_id')} for e in executions]}

async def run(args):
    async with SessionLocal() as db:
        result=await pilot(db,args.source_url,approval_ref=args.approval_ref,now=datetime.now(timezone.utc))
        print(json.dumps(result,default=str))
    await engine.dispose()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-url',required=True);parser.add_argument('--approval-ref',required=True)
    try:asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({'status':'PILOT_STOPPED','errorType':type(exc).__name__}));raise SystemExit(1)
