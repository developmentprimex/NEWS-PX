import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db import AppMetadata
from app.intelligence import collection
from app.intelligence.models import NewsItem, NewsSource
from app.intelligence.opportunities_adapter import (
    OpportunitiesWebsiteAdapter, OpportunitiesCollectionError, parse_announcement,
)
from test_brightdata_postgres_integration import database, private_postgres

NOW = datetime(2026, 10, 21, 14, tzinfo=timezone.utc)


def article(n=1, **changes):
    return {'id': n, 'link': f'https://opportunitiesforyouth.org/2026/10/21/call-{n}/',
        'date_gmt': '2026-10-21T10:00:00', 'modified_gmt': '2026-10-21T12:00:00',
        'title': {'rendered': 'New &amp; updated opportunity'},
        'content': {'rendered': '<p>Apply for this opportunity.</p><script>hidden()</script>'},
        'excerpt': {'rendered': ''}, **changes}


def install(monkeypatch, respond):
    monkeypatch.setattr('app.intelligence.opportunities_adapter._check_public_dns', AsyncMock())
    monkeypatch.setattr(OpportunitiesWebsiteAdapter, '_client', lambda self:
        httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False))


def response(rows, total=None):
    total = len(rows) if total is None else total
    return httpx.Response(200, json=rows, headers={'x-wp-total': str(total),
        'x-wp-totalpages': str((total + 24) // 25)})


def test_public_article_uses_gmt_and_rejects_external_identity():
    post, modified = parse_announcement(article())
    assert post.publication_time_verified and post.published_at.hour == 10 and modified.hour == 12
    assert post.title == 'New & updated opportunity' and 'hidden' not in post.original_text
    with pytest.raises(OpportunitiesCollectionError):parse_announcement(article(link='http://127.0.0.1/private'))
    with pytest.raises(OpportunitiesCollectionError):parse_announcement(article(modified_gmt=None))


def test_all_pages_are_read_without_social_provider_requests(monkeypatch):
    requests=[]
    def respond(req):
        requests.append(req)
        assert req.method=='GET' and req.url.host=='opportunitiesforyouth.org'
        assert req.url.params['modified_before']==NOW.isoformat()
        return response([article(n) for n in (range(1,26) if req.url.params['page']=='1' else [26])],26)
    install(monkeypatch,respond)
    posts=asyncio.run(OpportunitiesWebsiteAdapter('https://opportunitiesforyouth.org/').collect(since=NOW-timedelta(days=1),until=NOW))
    assert len(posts)==26 and len(requests)==2 and len({p.url for p in posts})==26


@pytest.mark.parametrize('mode',['repeat','truncated','changed','outside'])
def test_incomplete_or_ignored_pagination_never_reports_success(monkeypatch,mode):
    def respond(req):
        if mode=='outside':return response([article(modified_gmt='2026-09-01T12:00:00')])
        if req.url.params['page']=='1':return response([article(n) for n in range(1,26)],26)
        return response([] if mode=='truncated' else [article(1 if mode=='repeat' else 26)],27 if mode=='changed' else 26)
    install(monkeypatch,respond)
    with pytest.raises(OpportunitiesCollectionError):
        asyncio.run(OpportunitiesWebsiteAdapter('https://opportunitiesforyouth.org/').collect(since=NOW-timedelta(days=1),until=NOW))


def test_empty_interval_and_transient_reads_are_bounded(monkeypatch):
    requests=[]
    def respond(req):
        requests.append(req)
        return httpx.Response(503) if len(requests)==1 else response([])
    install(monkeypatch,respond)
    monkeypatch.setattr('app.intelligence.opportunities_adapter.asyncio.sleep',AsyncMock())
    assert asyncio.run(OpportunitiesWebsiteAdapter('https://opportunitiesforyouth.org/').collect(since=NOW-timedelta(days=1),until=NOW))==[]
    assert len(requests)==2


def analysis():
    return NS(summary='An update',category='NEWS',importanceScore=1,relevanceScore=1,whyItMatters=None,
        deadline=None,fundingAmount=None,eligibility=None,opportunityType=None,tags=[])


def test_checkpoint_only_advances_after_complete_batch_and_updates_existing_item(database,monkeypatch):
    _,sessions=database
    monkeypatch.setattr(collection,'analyze_news_item',AsyncMock(return_value=analysis()))
    install(monkeypatch,lambda req:response([article()]))
    async def run():
        async with sessions() as db:
            s=NewsSource(name='OFY',type='WEBSITE',url='https://opportunitiesforyouth.org/',categories=['Grants'])
            db.add(s);await db.commit();sid=s.id
            assert await collection.check_source(db,sid,now=NOW)=='completed'
            point=await db.get(AppMetadata,'ofy-website-checkpoint:'+str(sid));before=point.value
            item=(await db.scalars(select(NewsItem))).one();iid=item.id
            assert item.title=='New & updated opportunity'
        # Failure preserves the committed checkpoint and does not fabricate a successful scan.
        install(monkeypatch,lambda req:httpx.Response(403))
        async with sessions() as db:
            assert await collection.check_source(db,sid,now=NOW+timedelta(days=1))=='error'
            assert (await db.get(AppMetadata,'ofy-website-checkpoint:'+str(sid))).value==before
        install(monkeypatch,lambda req:response([article(content={'rendered':'<p>Updated content with a new deadline.</p>'})]))
        async with sessions() as db:
            assert await collection.check_source(db,sid,now=NOW+timedelta(days=1,hours=2))=='completed'
            rows=list(await db.scalars(select(NewsItem)));assert len(rows)==1 and rows[0].id==iid
            assert 'Updated content' in rows[0].original_text
    asyncio.run(run())


@pytest.mark.parametrize('kind',['FACEBOOK','LINKEDIN'])
def test_social_activity_is_retained_even_when_category_filter_does_not_match(database,monkeypatch,kind):
    _,sessions=database
    monkeypatch.setattr(collection,'analyze_news_item',AsyncMock(return_value=analysis()))
    async def run():
        async with sessions() as db:
            s=NewsSource(name='Tracked',type=kind,url='https://example.com/',categories=['Grants'])
            db.add(s);await db.flush()
            post,_=parse_announcement(article())
            assert await collection._store_post(db,s,post)
            assert len(list(await db.scalars(select(NewsItem))))==1
    asyncio.run(run())


def test_large_unsaturated_pilot_limit_does_not_inflate_quiet_source(database,monkeypatch):
    from app.config import settings
    from app.intelligence.models import BrightDataExecution
    from app.intelligence.social_limits import record_limit_for
    _,sessions=database
    monkeypatch.setattr(settings,'NEWS_SOCIAL_ADAPTIVE_LIMITS',True)
    async def run():
        async with sessions() as db:
            s=NewsSource(name='Quiet',type='LINKEDIN',url='https://www.linkedin.com/company/quiet/')
            db.add(s);await db.flush()
            for limit,returned in [(1,1),(80,4)]:
                db.add(BrightDataExecution(source_id=s.id,started_at=NOW,completed_at=NOW,budget_month='2026-10',
                    trigger_reason='test',endpoint='mock',dataset_id='mock',start_date=NOW.date(),end_date=NOW.date(),
                    record_limit=limit,estimated_billable_usage=limit,returned_records=returned,state='completed'))
            await db.flush();assert await record_limit_for(db,s)==7
    asyncio.run(run())
