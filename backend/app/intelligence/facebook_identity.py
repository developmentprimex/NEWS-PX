"""Explicit public Facebook source identities. Share URLs are never guessed."""
import re
from urllib.parse import parse_qs, unquote, urlsplit

HOSTS = {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com'}
RESERVED = {'share','login','login.php','watch','reel','reels','posts','photo','photo.php','story.php','permalink.php','events','marketplace','help','privacy','settings','dialog','recover','checkpoint'}

def canonical_facebook_url(value):
    p=urlsplit(value.strip())
    if p.scheme not in {'http','https'} or p.hostname not in HOSTS or p.username or p.password or p.port:
        raise ValueError('Use a direct Facebook page or public group URL.')
    parts=unquote(p.path).strip('/').split('/')
    if len(parts)==3 and parts[0].casefold()=='groups' and parts[2].casefold()=='about':parts=parts[:2]
    if len(parts)==3 and parts[0] in {'people','pages'} and parts[2].isdigit():
        return 'https://www.facebook.com/profile.php?id='+parts[2]
    if len(parts)==2 and parts[0]=='p' and parts[1].rsplit('-',1)[-1].isdigit():
        return 'https://www.facebook.com/profile.php?id='+parts[1].rsplit('-',1)[-1]
    if len(parts)==2 and parts[0].casefold()=='groups' and re.fullmatch(r'[A-Za-z0-9._-]+',parts[1]):
        return 'https://www.facebook.com/groups/'+parts[1].casefold()+'/'
    if len(parts)==1 and parts[0]=='profile.php':
        values=parse_qs(p.query).get('id',[])
        if len(values)==1 and values[0].isdigit():return 'https://www.facebook.com/profile.php?id='+values[0]
    if len(parts)==1 and re.fullmatch(r'[A-Za-z0-9._-]+',parts[0]) and parts[0].casefold() not in RESERVED|{'groups','people','pages','profile.php'}:
        return 'https://www.facebook.com/'+parts[0].casefold()+'/'
    raise ValueError('Resolve the share link to a direct Facebook page or public group URL first.')

def identity_or_none(value):
    try:return canonical_facebook_url(value)
    except (ValueError,TypeError,AttributeError):return None

def source_kind(value):
    identity=identity_or_none(value)
    return 'group' if identity and '/groups/' in identity else 'page' if identity else None

def source_approved(source):
    from app.config import settings
    identity=identity_or_none(source.url)
    approved={identity_or_none(v) for v in settings.NEWS_FACEBOOK_APPROVED_SOURCE_URLS}
    return bool(identity and identity in approved)

def setup_note(source):
    from app.config import settings
    if not identity_or_none(source.url):return 'Resolve this Facebook share link to a direct public page or group URL.'
    if not source_approved(source):return 'Saved. Facebook collection is limited to the approved pilot sources.'
    if not settings.NEWS_FACEBOOK_FETCH_ENABLED:return 'Facebook collection is not activated yet.'
    return None

async def lock_identity_change(db,old_url,new_url):
    from sqlalchemy import text
    from app.intelligence.linkedin_identity import lock_key
    for key in sorted({lock_key(i) for v in (old_url,new_url) if (i:=identity_or_none(v))}):
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':key})

async def matching_sources(db,identity):
    from sqlalchemy import select
    from app.intelligence.models import NewsSource
    rows=list(await db.scalars(select(NewsSource).where(NewsSource.type=='FACEBOOK')))
    return [s for s in rows if identity_or_none(s.url)==identity]
