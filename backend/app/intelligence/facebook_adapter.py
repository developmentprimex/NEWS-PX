"""Bounded public Facebook posts using the existing asynchronous transport."""
from datetime import date,datetime,timedelta,timezone
from urllib.parse import parse_qs,urlsplit,urlunsplit
from app.intelligence.facebook_identity import identity_or_none,source_kind,HOSTS
from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter,LinkedInCollectionError,SNAPSHOT_ID
from app.intelligence.services import CollectedNewsItem,publication_has_precise_time

PAGE_DATASET='gd_lkaxegm826bjpoo9m5'
GROUP_DATASET='gd_lz11l67o2cb3r0lkj3'

def dataset_for(source):return GROUP_DATASET if source_kind(source.url)=='group' else PAGE_DATASET

def request_spec(source,start,end,limit,*,expanded=False):
    identity=identity_or_none(source.url)
    from app.intelligence.collection_exception import RECORD_LIMIT
    from app.config import settings
    ceiling = max(RECORD_LIMIT, settings.NEWS_SOCIAL_MAX_RECORD_LIMIT) if settings.NEWS_SOCIAL_ADAPTIVE_LIMITS else RECORD_LIMIT
    if not identity or not 1<=limit<=(ceiling if expanded else 6):raise LinkedInCollectionError('Unverified Facebook URL or invalid record limit.')
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    if first>last:raise LinkedInCollectionError('Invalid Facebook date interval.')
    # Ordinary daily executions retain their logical day in the ledger. The
    # provider's exclusive date filters enclose it; trial dates already enclose
    # their precise interval. Padding rows are filtered before storage.
    if first==last:first,last=first-timedelta(days=1),last+timedelta(days=1)
    body={'url':identity,'num_of_posts':limit,'start_date':first.strftime('%m-%d-%Y'),
          'end_date':last.strftime('%m-%d-%Y')}
    return {'dataset_id':dataset_for(source),'limit_per_input':limit,'include_errors':'true','notify':'false'},[body]

def post_url(value):
    if not isinstance(value,str):return None
    try:
        p=urlsplit(value)
        if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password or p.port:return None
        path=p.path
        parts=path.strip('/').split('/')
        if not (('/posts/' in path and len(parts)>=3) or path.startswith(('/reel/','/videos/'))
                or len(parts)==2 and parts[0]=='events' and parts[1].isdigit()
                or len(parts)>=3 and parts[1] in {'photos','videos'}
                or path in {'/permalink.php','/story.php','/photo.php'}):return None
        query=parse_qs(p.query)
        keep='&'.join(k+'='+query[k][0] for k in ('story_fbid','id','fbid') if k in query)
        if path.endswith('.php') and not any(k in query for k in ('story_fbid','fbid')):return None
        return urlunsplit(('https','www.facebook.com',path,keep,''))
    except ValueError:return None

def belongs(record,source):
    identity=identity_or_none(source.url)
    if source_kind(source.url)=='group':
        group=identity_or_none(record.get('group_url'))
        identifier=str(record.get('group_id') or '').casefold()
        expected=urlsplit(identity).path.strip('/').split('/')[-1]
        return group==identity or bool(identifier and identifier==expected)
    candidates=(record.get('page_url'),record.get('user_url'))
    if any(identity_or_none(v)==identity for v in candidates if v):return True
    expected=parse_qs(urlsplit(identity).query).get('id',[urlsplit(identity).path.strip('/')])[0]
    return bool(expected.isdigit() and str(record.get('profile_id') or '')==expected and record.get('is_page') is True)

def parse_posts(records,source):
    posts=[]
    for row in records:
        if not isinstance(row,dict) or row.get('error') or row.get('error_code'):continue
        url=post_url(row.get('url'))
        identifier=row.get('post_id')
        if not url or not identifier or not belongs(row,source):continue
        raw=row.get('date_posted')
        original=row.get('original_post')
        shared=bool(isinstance(original,dict) and any(k!='user_avatar_image' and v for k,v in original.items()))
        # Event records do not establish when the page announced the event.
        precise=publication_has_precise_time(raw) and not shared and '/events/' not in url
        try:
            stamp=datetime.fromisoformat(raw.replace('Z','+00:00')) if isinstance(raw,str) else None
            if stamp and not stamp.tzinfo:stamp=stamp.replace(tzinfo=timezone.utc)
        except ValueError:stamp=None
        content=row.get('content')
        content=content[:20000] if isinstance(content,str) else None
        title=(content[:140].strip() if content else '') or 'Facebook post - '+source.name
        # Facebook records describe posts, not a verified separate share event.
        posts.append(CollectedNewsItem(external_id='facebook:'+str(identifier)[:480],url=url,title=title,
            original_text=content,published_at=stamp,publication_time_verified=bool(stamp and precise)))
    if records and not posts:raise LinkedInCollectionError('Facebook returned no usable posts attributable to this source.')
    return posts

class BrightDataFacebookAdapter(BrightDataLinkedInAdapter):
    def __init__(self,token,dataset_id,*,expanded=False,exclude_posts=None):
        super().__init__(token,dataset_id)
        self.expanded=expanded
        self.exclude_posts=list(dict.fromkeys(exclude_posts or []))

    async def trigger(self,source,*,start_date,end_date,limit=3):
        query,body=request_spec(source,start_date,end_date,limit,expanded=self.expanded)
        if self.exclude_posts:
            body[0]['posts_to_not_include']=self.exclude_posts
        async with self._client() as client:response=await client.post('trigger',params=query,json=body)
        payload=self._json(response)
        snapshot=payload.get('snapshot_id') if isinstance(payload,dict) else None
        if not isinstance(snapshot,str) or not SNAPSHOT_ID.fullmatch(snapshot):raise LinkedInCollectionError('Provider did not return a valid snapshot ID.')
        return snapshot
