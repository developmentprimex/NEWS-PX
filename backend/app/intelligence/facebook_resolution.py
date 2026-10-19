"""Resolve saved Facebook share URLs using public GETs, without provider credits."""
from urllib.parse import urljoin,urlsplit
import httpx
from bs4 import BeautifulSoup
from app.intelligence.facebook_identity import HOSTS,identity_or_none
from app.intelligence.rss_adapter import _check_public_dns

async def resolve_facebook_url(value):
    current=value
    async with httpx.AsyncClient(timeout=20,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'}) as client:
        for _ in range(6):
            p=urlsplit(current)
            if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password or p.port:
                raise ValueError('Facebook redirected outside the supported public domain.')
            await _check_public_dns(current)
            async with client.stream('GET',current) as response:
                if response.status_code in {301,302,303,307,308}:
                    current=urljoin(current,response.headers.get('location',''));continue
                if response.status_code!=200:raise ValueError('Facebook page could not be read publicly.')
                body=bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>2_000_000:break
                soup=BeautifulSoup(bytes(body),'html.parser')
                tag=soup.select_one('link[rel="canonical"]') or soup.select_one('meta[property="og:url"]')
                candidate=(tag.get('href') or tag.get('content')) if tag else current
                identity=identity_or_none(candidate)
                title=soup.title.get_text(' ',strip=True) if soup.title else ''
                if not identity or not title or title.strip().casefold() in {'facebook','log in to facebook','facebook - log in or sign up'}:
                    raise ValueError('A public Facebook identity could not be verified. Enter the direct URL.')
                return {'url':identity,'name':title.split(' | ')[0][:140]+' - Facebook'}
    raise ValueError('Facebook share link exceeded the redirect limit.')
