"""Keep website navigation and unrelated global content out of news collection."""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import String, cast, exists, literal, select, func
from app.db import AppMetadata

EXCLUSION_PREFIX = 'news-quality-excluded:'
STATIC_PATH = re.compile(
    r'/(?:privacy(?:[-_/]|$)|terms(?:[-_/]|$)|contact(?:[-_/]|$)|about(?:[-_/]|$)|'
    r'login(?:[./-]|$)|signin|register|search|tag|author|datenschutz|barrierefreiheit|'
    r'rreth-?nesh|policies|hello-world|use-cases|platform|company|country-results|'
    r'outcome-detail|output-detail|legalizime|sherbime-konsulore|services(?:/|$)|expertise(?:/|$))(?:[^?]*)', re.I)
STATIC_TITLE = re.compile(
    r'^(?:privacy policy(?:-en)?|terms (?:of (?:use|service)|and (?:conditions|policies))|'
    r'hello world!?|rreth nesh|folder:.*|use cases|legalizime|newsroom|'
    r'financing: receive funding|social development|peace and security|services|'
    r'digital transformation center|willkommen auf den seiten des auswärtigen amts|'
    r'mirë se vini në faqen e internetit të ministrisë së jashtme federale)$', re.I)

def canonical_article_url(value):
    p = urlsplit(value)
    query = urlencode([(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
                       if not k.lower().startswith('utm_') and k.lower() not in {'ref','fbclid','gclid'}])
    return urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/') or '/',query,''))

def website_content_reason(source_url, url, title, body=''):
    """Return a reason only for content outside this source's news remit."""
    source, page = urlsplit(source_url), urlsplit(url)
    path=page.path.casefold()
    heading=unicodedata.normalize('NFKC',title or '').strip()
    if STATIC_PATH.search(path) or STATIC_TITLE.fullmatch(heading):
        return 'Navigation, policy, service or test page; not a news article.'
    if path.endswith('.pdf') and not re.search(r'grant|call|tender|thirr|aplik|procure|ftes|njoft|vacan|job|ausschreib|gesucht',heading,re.I):
        return 'General reference PDF; not an announcement or call.'
    host=(source.hostname or '').removeprefix('www.')
    if (page.hostname or '').removeprefix('www.') != host:
        return 'Article is outside the configured website.'
    text=unicodedata.normalize('NFKC',heading+' '+(body or '')[:2500]).casefold()
    if host=='women-in-tech.org' and source.path.rstrip('/')=='/ko' and not re.search(r'\bkosov[oëa]\b',text):
        return 'Global chapter content without a Kosovo connection.'
    if host=='startupgrind.com' and source.path.rstrip('/')=='/tirana' and not re.search(r'\btirana|\balbania|\bshqip',text):
        return 'Global content outside the Tirana chapter.'
    if host=='giz.de':
        if not re.search(r'/newsroom/.+|/news/.+',path) or not re.search(r'\bkosov[oëa]\b',text):
            return 'General service or global content outside Kosovo news.'
    if host=='eca.unwomen.org' and not re.search(r'\bkosov[oëa]\b',text):
        return 'Regional content without a Kosovo connection.'
    if host=='bmeia.gv.at' and not (path.startswith('/oeb-pristina/') or '/pristina/' in path):
        return 'General ministry content outside the Pristina embassy.'
    if host=='gov.uk' and 'british-embassy-pristina' in source.path:
        if not re.search(r'^/government/(?:news|speeches|world-location-news)/.+',path) or not re.search(r'\b(?:kosovo|pristina)\b',text):
            return 'General government content outside Pristina embassy news.'
    return None

def stale_initial_import(item, source, *, now=None):
    """Do not relabel old imports as fresh updates; genuine collection history stays."""
    published=getattr(item,'published_at',None)
    created=getattr(source,'created_at',None)
    return bool(published and created and published < created-timedelta(days=90))

def excluded_item_query(item_id):
    return exists(select(AppMetadata.key).where(
        AppMetadata.key == literal(EXCLUSION_PREFIX)+func.replace(cast(item_id,String),'-','')))

def announcement_signal(html_soup):
    return bool(html_soup.select_one("meta[property='article:published_time'],meta[name='date'],time[datetime],"
                                     "meta[itemprop='datePublished']") or any(
        'NewsArticle' in node.get_text() or 'BlogPosting' in node.get_text()
        for node in html_soup.select('script[type="application/ld+json"]')))
