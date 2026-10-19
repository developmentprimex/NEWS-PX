"""Public Facebook identity, budgets and durable provider lifecycle regressions."""
import asyncio,copy,json
from datetime import datetime,timedelta,timezone,date
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import select
from app.config import settings
from app.db import AppMetadata
from app.intelligence import collection
from app.intelligence.facebook_adapter import BrightDataFacebookAdapter,parse_posts,request_spec,PAGE_DATASET,GROUP_DATASET
from app.intelligence.facebook_identity import canonical_facebook_url,identity_or_none
from app.intelligence.models import NewsSource,NewsItem,BrightDataExecution,BrightDataAccount
from app.intelligence.collection_cycles import CollectionCycle,CollectionCycleSource
from app.intelligence.cycle_service import TRIAL_KEY,freeze_inventory,dispatch_cycle_source
from app.intelligence.billing_evidence import parse_billing_proof
from test_brightdata_postgres_integration import database,private_postgres
from test_collection_trial_usage import fixture,NOW

@pytest.mark.parametrize('url,want',[
 ('https://m.facebook.com/EULEX.KOSOVO/?ref=share','https://www.facebook.com/eulex.kosovo/'),
 ('https://www.facebook.com/groups/525320131563682/about/','https://www.facebook.com/groups/525320131563682/'),
 ('https://www.facebook.com/people/Test/100083750132785/','https://www.facebook.com/profile.php?id=100083750132785'),
 ('https://www.facebook.com/p/Test-100083750132785/','https://www.facebook.com/profile.php?id=100083750132785'),
])
def test_source_aliases_share_one_monitoring_identity(url,want):assert canonical_facebook_url(url)==want

@pytest.mark.parametrize('url',['https://www.facebook.com/share/abc/','https://www.facebook.com.evil.test/test/',
 'https://user:pass@facebook.com/test/','https://facebook.com:443/test/','https://facebook.com/login/',
 'https://facebook.com/groups/123/posts/456/'])
def test_share_login_and_foreign_urls_cannot_become_collection_identity(url):assert identity_or_none(url) is None

def source(url='https://www.facebook.com/example/'):
 return SimpleNamespace(id=uuid4(),url=url,type='FACEBOOK',name='Example')

def record(**values):
 return {'post_id':'111','url':'https://www.facebook.com/example/posts/111/?tracking=1',
         'page_url':'https://www.facebook.com/example/','is_page':True,'content':'New grant call',
         'date_posted':'2026-10-19T12:00:00Z',**values}

def test_post_parser_rejects_other_source_and_preserves_precise_time_and_stable_id():
 posts=parse_posts([record(),record(post_id='222',page_url='https://www.facebook.com/other/')],source())
 assert len(posts)==1 and posts[0].external_id=='facebook:111'
 assert posts[0].url=='https://www.facebook.com/example/posts/111/'
 assert posts[0].publication_time_verified and posts[0].published_at.hour==12

def test_group_posts_are_attributed_to_group_not_individual_author():
 posts=parse_posts([record(url='https://www.facebook.com/groups/123/posts/111/',group_id='123',
    group_url='https://www.facebook.com/groups/123/',user_url='https://www.facebook.com/author/',is_page=False)],source('https://www.facebook.com/groups/123/'))
 assert len(posts)==1

def test_date_only_and_shared_original_timestamp_cannot_certify_exact_activity():
 assert not parse_posts([record(date_posted='2026-10-19')],source())[0].publication_time_verified
 assert not parse_posts([record(original_post={'url':'https://www.facebook.com/other/posts/9/'})],source())[0].publication_time_verified

def test_request_bounds_page_and_group_by_exact_dataset_date_and_limit():
 query,body=request_spec(source(),'2026-10-18','2026-10-20',3)
 assert query['dataset_id']==PAGE_DATASET and query['limit_per_input']==3
 assert body==[{'url':'https://www.facebook.com/example/','num_of_posts':3,'start_date':'10-18-2026','end_date':'10-20-2026'}]
 assert request_spec(source('https://www.facebook.com/groups/123/'),'2026-10-18','2026-10-20',1)[0]['dataset_id']==GROUP_DATASET

def test_three_account_meters_are_summed_once_and_unknown_consumption_is_rejected():
 binding,responses=fixture()
 binding['usageResources']=['test-resource','facebook-page','facebook-group']
 usage=responses['usage']['test-account']
 for resource,value in [('facebook-page',2),('facebook-group',3)]:
  usage['data'][resource]={'rows_initial_billable':[0]*18+[value]}
  usage['sums'][resource]={k:{'rows_initial_billable':value} for k in ('back_m0','custom')}
 proof={'credentialSha256':binding['credentialSha256'],'responses':{k:{'status':200,'data':v} for k,v in responses.items()}}
 assert parse_billing_proof(proof,binding,NOW)['billable_records']==6
 usage['sums']['facebook-page']['custom']['rows_initial_billable']=1
 with pytest.raises(ValueError):parse_billing_proof(proof,binding,NOW)

def test_pilot_posts_persist_once_and_cap_blocks_automatic_retrigger(database,monkeypatch):
 _,sessions=database
 now=datetime.now(timezone.utc);day=(now-timedelta(days=1)).date()
 requests=[]
 async def respond(request):
  requests.append(request)
  if request.method=='POST':return httpx.Response(200,json={'snapshot_id':'sd_facebook123'})
  if '/progress/' in request.url.path:return httpx.Response(200,json={'status':'ready'})
  return httpx.Response(200,json=[record(post_id=str(i),url=f'https://www.facebook.com/example/posts/{i}/',date_posted=f'{day}T12:00:00Z') for i in range(3)])
 monkeypatch.setattr(BrightDataFacebookAdapter,'_client',lambda self:httpx.AsyncClient(base_url='https://api.brightdata.com/datasets/v3/',transport=httpx.MockTransport(respond)))
 from app.intelligence import facebook_collection
 async def fresh(*args):pass
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',fresh)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 async def run():
  async with sessions() as db:
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK',categories=[],fetch_interval_minutes=1440)
   db.add(s);await db.commit();sid=s.id
  for expected in ('started','completed','paused'):
   async with sessions() as db:assert await collection.check_source(db,sid,now=now)==expected
  async with sessions() as db:
   assert len(list(await db.scalars(select(NewsItem))))==3
   e=(await db.scalars(select(BrightDataExecution))).one()
   assert e.dataset_id==PAGE_DATASET and e.estimated_billable_usage==3 and e.state=='partial'
   assert (await db.get(NewsSource,sid)).last_successful_fetch_at is None
  assert sum(r.method=='POST' for r in requests)==1
 asyncio.run(run())

def test_nonpilot_is_excluded_from_cycle_and_never_spends(database,monkeypatch):
 _,sessions=database
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 now=datetime.now(timezone.utc)
 async def run():
  async with sessions() as db:
   db.add_all([NewsSource(name='Pilot',url='https://www.facebook.com/example/',type='FACEBOOK'),
               NewsSource(name='Other',url='https://www.facebook.com/other/',type='FACEBOOK')])
   c=CollectionCycle(trial_date=now.date(),phase='manual',cutoff_at=now,deadline_at=now+timedelta(minutes=30))
   db.add(c);await db.commit();await freeze_inventory(db,c)
   rows=list(await db.scalars(select(CollectionCycleSource)))
   assert len(rows)==1 and rows[0].name=='Pilot'
   other=await db.scalar(select(NewsSource).where(NewsSource.name=='Other'))
   assert await collection.check_source(db,other.id,now=now)=='paused'
   assert not list(await db.scalars(select(BrightDataExecution)))
 asyncio.run(run())

def test_shared_daily_target_blocks_facebook_before_post(database,monkeypatch):
 from app.intelligence.credit_control import reserve_execution
 _,sessions=database
 now=datetime.now(timezone.utc)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 async def run():
  async with sessions() as db:
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK')
   db.add(s)
   db.add(BrightDataExecution(source_id=uuid4(),started_at=now,budget_month=now.strftime('%Y-%m'),endpoint='test',dataset_id='linkedin',start_date=now.date(),end_date=now.date(),trigger_reason='test',state='completed',record_limit=6,estimated_billable_usage=129))
   await db.commit()
   assert await reserve_execution(db,s,now,now.date(),now.date(),limit=3,dataset_id=PAGE_DATASET) is None
   assert 'Daily' in s.last_error and s.pending_execution_id is None
 asyncio.run(run())

def test_facebook_timeout_retains_reservation_without_automatic_repost(database,monkeypatch):
 _,sessions=database;requests=[]
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 from app.intelligence import facebook_collection
 async def fresh(*args):pass
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',fresh)
 async def timeout(req):
  requests.append(req);raise httpx.ReadTimeout('ambiguous POST',request=req)
 monkeypatch.setattr(BrightDataFacebookAdapter,'_client',lambda self:httpx.AsyncClient(base_url='https://api.brightdata.com/datasets/v3/',transport=httpx.MockTransport(timeout)))
 async def run():
  async with sessions() as db:
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK');db.add(s);await db.commit();sid=s.id
  async with sessions() as db:assert await collection.check_source(db,sid)=='error'
  async with sessions() as db:assert await collection.check_source(db,sid)=='paused'
  async with sessions() as db:
   e=(await db.scalars(select(BrightDataExecution))).one()
   assert e.state=='uncertain' and e.estimated_billable_usage==3 and not e.snapshot_id
  assert len(requests)==1
 asyncio.run(run())

def test_failed_fresh_billing_never_reserves_facebook(database,monkeypatch):
 _,sessions=database
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 from app.intelligence import facebook_collection
 async def failure(*args):raise ValueError('billing mismatch')
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',failure)
 async def run():
  async with sessions() as db:
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK');db.add(s);await db.commit()
   assert await collection.check_source(db,s.id)=='paused'
   assert not list(await db.scalars(select(BrightDataExecution)))
 asyncio.run(run())

def test_facebook_completed_cycle_stores_only_exact_window_and_deduplicates(database,monkeypatch):
 _,sessions=database
 now=datetime.now(timezone.utc);start=now-timedelta(hours=3)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_RECORD_LIMIT',6)
 from app.intelligence import facebook_collection
 async def fresh(*args):pass
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',fresh)
 requests=[]
 async def respond(req):
  requests.append(req)
  if req.method=='POST':return httpx.Response(200,json={'snapshot_id':'sd_window123'})
  if '/progress/' in req.url.path:return httpx.Response(200,json={'status':'ready'})
  return httpx.Response(200,json=[record(date_posted=(start+timedelta(seconds=1)).isoformat()),
      record(date_posted=(start+timedelta(seconds=1)).isoformat()),
      record(post_id='333',url='https://www.facebook.com/example/posts/333/',date_posted=start.isoformat())])
 monkeypatch.setattr(BrightDataFacebookAdapter,'_client',lambda self:httpx.AsyncClient(base_url='https://api.brightdata.com/datasets/v3/',transport=httpx.MockTransport(respond)))
 async def run():
  async with sessions() as db:
   db.add(AppMetadata(key=TRIAL_KEY,value=json.dumps({'enabled':True,'date':now.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Budapest')).date().isoformat()})))
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK');db.add(s)
   c=CollectionCycle(trial_date=now.date(),phase='manual',cutoff_at=now,deadline_at=now+timedelta(minutes=30));db.add(c);await db.flush()
   row=CollectionCycleSource(cycle_id=c.id,source_id=s.id,name=s.name,type=s.type,canonical_url=s.url,window_start=start,window_end=now,verified_start=True)
   db.add(row);await db.commit();rid=row.id
   assert await dispatch_cycle_source(db,rid,now)=='running'
  async with sessions() as db:assert await dispatch_cycle_source(db,rid,now)=='completed'
  async with sessions() as db:
   assert len(list(await db.scalars(select(NewsItem))))==1
   row=await db.get(CollectionCycleSource,rid);assert row.coverage_confirmed
   e=(await db.scalars(select(BrightDataExecution))).one()
   assert e.returned_records==3 and e.duplicate_posts==1 and e.ignored_records==1 and e.newly_saved_posts==1
   assert await dispatch_cycle_source(db,rid,now)=='completed'
  assert sum(r.method=='POST' for r in requests)==1
 asyncio.run(run())

def test_one_use_pilot_cannot_create_second_execution_after_restart(database,monkeypatch):
 from app import facebook_admin
 _,sessions=database
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 from app.intelligence import facebook_collection
 async def fresh(*args):pass
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',fresh)
 requests=[]
 async def respond(req):
  requests.append(req)
  if req.method=='POST':return httpx.Response(200,json={'snapshot_id':'sd_once123'})
  if '/progress/' in req.url.path:return httpx.Response(200,json={'status':'ready'})
  return httpx.Response(200,json=[])
 monkeypatch.setattr(BrightDataFacebookAdapter,'_client',lambda self:httpx.AsyncClient(base_url='https://api.brightdata.com/datasets/v3/',transport=httpx.MockTransport(respond)))
 async def run():
  async with sessions() as db:
   db.add(NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK'));await db.commit()
  for expected in ('started','completed','already-consumed'):
   async with sessions() as db:
    result=await facebook_admin.pilot(db,'https://www.facebook.com/example/',approval_ref='test-owner',now=datetime.now(timezone.utc))
    assert result['state']==expected
  async with sessions() as db:
   e=(await db.scalars(select(BrightDataExecution))).one()
   assert e.record_limit==1 and e.estimated_billable_usage==1
  assert sum(r.method=='POST' for r in requests)==1
 asyncio.run(run())

@pytest.mark.parametrize('path',['example/videos/111/','example/photos/111/','example/photos/a.99/111/'])
def test_ordinary_photo_video_posts_remain_usable(path):
 assert len(parse_posts([record(url='https://www.facebook.com/'+path)],source()))==1

@pytest.mark.parametrize('mode',['padding','failed','date-only','naive','shared','video','photo'])
def test_facebook_daily_coverage_failure_and_permalink_contract(database,monkeypatch,mode):
 from app.intelligence.cycle_service import latest_verified_checkpoint
 _,sessions=database;requests=[]
 now=datetime.now(timezone.utc);day=(now-timedelta(days=1)).date()
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_FETCH_ENABLED',True)
 monkeypatch.setattr(settings,'NEWS_FACEBOOK_APPROVED_SOURCE_URLS',['https://www.facebook.com/example/'])
 from app.intelligence import facebook_collection
 async def fresh(*args):pass
 monkeypatch.setattr(facebook_collection,'refresh_facebook_billing',fresh)
 inside=record(date_posted=f'{day}T12:00:00Z')
 if mode=='date-only':inside['date_posted']=day.isoformat()
 if mode=='naive':inside['date_posted']=f'{day}T12:00:00'
 if mode=='shared':inside['original_post']={'url':'https://www.facebook.com/other/posts/222/'}
 if mode in {'video','photo'}:inside['url']='https://www.facebook.com/example/'+('videos' if mode=='video' else 'photos')+'/111/'
 rows=[inside]
 if mode=='padding':rows.append(record(post_id='222',url='https://www.facebook.com/example/posts/222/',date_posted=f'{day-timedelta(days=1)}T12:00:00Z'))
 async def respond(req):
  requests.append(req)
  if req.method=='POST':return httpx.Response(200,json={'snapshot_id':'sd_dailyfb123'})
  if '/progress/' in req.url.path:return httpx.Response(200,json={'status':'failed' if mode=='failed' else 'ready'})
  return httpx.Response(200,json=rows)
 monkeypatch.setattr(BrightDataFacebookAdapter,'_client',lambda self:httpx.AsyncClient(base_url='https://api.brightdata.com/datasets/v3/',transport=httpx.MockTransport(respond)))
 async def run():
  async with sessions() as db:
   s=NewsSource(name='Example',url='https://www.facebook.com/example/',type='FACEBOOK');db.add(s);await db.commit();sid=s.id
   assert await collection.check_source(db,sid,now=now)=='started'
  async with sessions() as db:await collection.check_source(db,sid,now=now)
  async with sessions() as db:
   e=(await db.scalars(select(BrightDataExecution))).one()
   s=await db.get(NewsSource,sid)
   if mode=='failed':
    assert e.state=='failed' and s.linkedin_paused_reason
    assert await collection.protected_facebook_check(db,s,now+timedelta(days=2))=='paused'
    assert await collection.protected_facebook_check(db,s,now+timedelta(hours=3),reason='trial-poll')=='paused'
   elif mode in {'date-only','naive','shared'}:
    assert e.state=='partial' and s.last_successful_fetch_at is None
    assert await latest_verified_checkpoint(db,sid) is None
    assert len(list(await db.scalars(select(NewsItem))))==1
   else:
    assert e.state=='completed' and await latest_verified_checkpoint(db,sid)==datetime.combine(day+timedelta(days=1),datetime.min.time(),timezone.utc)
    assert len(list(await db.scalars(select(NewsItem))))==1
    payload=json.loads(next(r for r in requests if r.method=='POST').content)[0]
    assert payload['start_date']==(day-timedelta(days=1)).strftime('%m-%d-%Y')
    assert payload['end_date']==(day+timedelta(days=1)).strftime('%m-%d-%Y')
   assert sum(r.method=='POST' for r in requests)==1
 asyncio.run(run())
