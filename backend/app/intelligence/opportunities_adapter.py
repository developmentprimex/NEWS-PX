"""Paginated public OFY announcements, independent of social scraper credits."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from app.intelligence.rss_adapter import _check_public_dns, _plain_text
from app.intelligence.services import CollectedNewsItem

HOST = 'opportunitiesforyouth.org'
ENDPOINT = 'https://' + HOST + '/wp-json/wp/v2/posts'
MAX_BYTES = 4_000_000
PAGE_SIZE = 25
MAX_PAGES = 40
OVERLAP = timedelta(hours=2)


class OpportunitiesCollectionError(RuntimeError):
    pass


def supports(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname in {HOST, 'www.' + HOST}
                and not p.username and not p.password and not p.port and p.path in {'', '/'} and not p.query)
    except (ValueError, TypeError):
        return False


def gmt(value):
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError):
        raise OpportunitiesCollectionError('The official announcement has an invalid GMT timestamp.') from None


def parse_announcement(row):
    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
        raise OpportunitiesCollectionError('The official announcement has no stable identity.')
    link = row.get('link')
    try:
        p = urlsplit(link)
        valid = p.scheme == 'https' and p.hostname in {HOST, 'www.' + HOST} and not p.username and not p.password and not p.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise OpportunitiesCollectionError('An official announcement links outside the monitored website.')
    if any(not isinstance(row.get(field), dict) for field in ['title', 'content', 'excerpt']):
        raise OpportunitiesCollectionError('The official announcement has malformed content fields.')
    title = _plain_text(str(row['title'].get('rendered') or ''))[:500]
    content = _plain_text(str(row['content'].get('rendered') or row['excerpt'].get('rendered') or ''))
    if not title or not content:
        raise OpportunitiesCollectionError('The official announcement is missing its title or content.')
    published, modified = gmt(row.get('date_gmt')), gmt(row.get('modified_gmt'))
    return CollectedNewsItem(external_id='ofy:' + str(row['id']), url=link, title=title,
        original_text=content[:20_000], published_at=published, publication_time_verified=True), modified


class OpportunitiesWebsiteAdapter:
    def __init__(self, url):
        if not supports(url):
            raise OpportunitiesCollectionError('This connector is restricted to the official OFY website.')

    def _client(self):
        return httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False,
            headers={'User-Agent': 'News-Intelligence/1.0', 'Accept': 'application/json'})

    async def _page(self, client, params):
        for attempt in range(3):
            try:
                await _check_public_dns(ENDPOINT)
                async with client.stream('GET', ENDPOINT, params=params) as response:
                    if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                        raise httpx.HTTPStatusError('Temporary official feed failure', request=response.request, response=response)
                    if response.is_redirect:
                        raise OpportunitiesCollectionError('The official API redirected; its identity must be reviewed.')
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise OpportunitiesCollectionError('The official feed page exceeds its safe size; coverage remains incomplete.')
                    try:
                        rows = json.loads(body)
                        total = int(response.headers['x-wp-total'])
                        pages = int(response.headers['x-wp-totalpages'])
                    except (ValueError, KeyError):
                        raise OpportunitiesCollectionError('The official API did not provide valid data and pagination evidence.') from None
                    if (not isinstance(rows, list) or total < 0 or pages < 0 or pages > MAX_PAGES
                            or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE):
                        raise OpportunitiesCollectionError('The official API pagination is incomplete or exceeds the recovery bound.')
                    return rows, total, pages
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                transient = isinstance(exc, httpx.TransportError) or exc.response.status_code in {429, 500, 502, 503, 504}
                if not transient or attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)

    async def collect(self, *, since, until):
        if not since.tzinfo or not until.tzinfo or not since < until:
            raise OpportunitiesCollectionError('A complete timezone-aware collection interval is required.')
        lower = since.astimezone(timezone.utc) - OVERLAP
        upper = until.astimezone(timezone.utc)
        params = {'per_page': PAGE_SIZE, 'page': 1, 'orderby': 'modified', 'order': 'asc',
            'modified_after': lower.isoformat(), 'modified_before': upper.isoformat(),
            '_fields': 'id,link,date_gmt,modified_gmt,title,excerpt,content'}
        seen = set(); posts = []
        async with self._client() as client:
            rows, total, pages = await self._page(client, params)
            for page in range(1, max(1, pages) + 1):
                if page > 1:
                    rows, count, count_pages = await self._page(client, {**params, 'page': page})
                    if (count, count_pages) != (total, pages):
                        raise OpportunitiesCollectionError('The official listing changed during pagination; retry the retained interval.')
                expected = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
                if len(rows) != expected:
                    raise OpportunitiesCollectionError('The official API returned a truncated page; interval remains incomplete.')
                for row in rows:
                    post, modified = parse_announcement(row)
                    if row['id'] in seen:
                        raise OpportunitiesCollectionError('The official API repeated a page or announcement; interval remains incomplete.')
                    if not lower <= modified <= upper:
                        raise OpportunitiesCollectionError('The official API ignored the requested update interval.')
                    seen.add(row['id']); posts.append(post)
            if len(seen) != total:
                raise OpportunitiesCollectionError('Not all announced results were read; interval remains incomplete.')
        return posts
