import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from app.intelligence.content_quality import canonical_article_url,website_content_reason,excluded_item_query
from app.intelligence.generic_website_adapter import Page,parse_html_article,discover_links,GenericWebsiteAdapter
from app.intelligence.models import NewsItem

@pytest.mark.parametrize('url,title',[
 ('https://example.org/en/privacy-policy','Privacy Policy'),
 ('https://example.org/terms-service','Terms of Use'),
 ('https://example.org/rrethnesh','Rreth nesh'),
 ('https://example.org/hello-world/','Hello world!'),
 ('https://example.org/home','Folder: Meny'),
])
def test_static_pages_are_not_announcements(url,title):
    assert website_content_reason('https://example.org',url,title)

def test_only_tracking_parameters_are_removed():
    assert canonical_article_url('https://example.org/article/?id=7&utm_source=rss&ref=home&lang=sq')=='https://example.org/article?id=7&lang=sq'

def test_discovery_skips_navigation_but_keeps_article():
    links,_=discover_links('<nav><a href="/news">News and announcements</a></nav><main><article><a href="/new-call?utm_source=x">A new funding call</a></article><a href="/privacy-policy">Privacy policy</a></main>','https://example.org/')
    assert links==[('https://example.org/new-call','A new funding call')]

def test_undated_product_page_is_not_a_news_article():
    page=Page('https://example.org/feature',b'<main><h1>Power up your platform</h1><p>Product services and features for enterprise businesses with a great offering to learn more about.</p></main>','text/html')
    assert parse_html_article(page,'Platform feature') is None

def test_local_chapter_feed_excludes_unrelated_global_posts():
    assert website_content_reason('https://women-in-tech.org/ko/','https://women-in-tech.org/panama-news','Panama holds a wellness day','Local Panama activities')
    assert website_content_reason('https://women-in-tech.org/ko/','https://women-in-tech.org/kosovo-news','Women in Tech Kosovo event','AI workshop in Kosovo') is None
    assert website_content_reason('https://www.startupgrind.com/tirana/','https://www.startupgrind.com/blog/global','A global business story')

def test_website_outside_source_is_not_accepted():
    assert website_content_reason('https://example.org/news','https://other.org/news','A news article')

def test_exclusions_are_correlated_before_feed_limit():
    query=str(excluded_item_query(NewsItem.id).compile(dialect=postgresql.dialect()))
    assert 'news_app_metadata.key' in query and 'intelligence_items.id' in query

def test_static_posts_never_reach_analysis_or_database(monkeypatch):
    from app.intelligence import collection
    from app.intelligence.services import CollectedNewsItem
    analyze=AsyncMock();monkeypatch.setattr(collection,'analyze_news_item',analyze)
    db=SimpleNamespace(scalar=AsyncMock())
    post=CollectedNewsItem(None,'https://example.org/privacy-policy','Privacy Policy','Apply for grants in the related navigation.',None)
    result=asyncio.run(collection._store_post(db,SimpleNamespace(type='WEBSITE',url='https://example.org'),post))
    assert result is False
    analyze.assert_not_awaited();db.scalar.assert_not_awaited()

def test_global_advertised_feed_falls_back_to_local_article_links(monkeypatch):
    adapter=GenericWebsiteAdapter('https://women-in-tech.org/ko/')
    home=Page(adapter.source_url,b'<link rel="alternate" type="application/rss+xml" href="/feed/"><article><a href="/kosovo-news-workshop">Kosovo innovation workshop</a></article>','text/html')
    feed=Page('https://women-in-tech.org/feed/',b'<rss version="2.0"><channel><title>Global</title><item><title>Panama wellness day</title><link>https://women-in-tech.org/panama</link></item></channel></rss>','application/rss+xml')
    article=Page('https://women-in-tech.org/kosovo-news-workshop',b'<meta property="article:published_time" content="2026-10-23T10:00:00Z"><main><h1>Kosovo innovation workshop</h1><p>Women in Tech Kosovo organised a workshop on practical innovation and technology applications in Kosovo.</p></main>','text/html')
    async def get(client,url):return {p.url.rstrip('/'):p for p in (home,feed,article)}[url.rstrip('/')]
    monkeypatch.setattr(adapter,'_get',get)
    posts=asyncio.run(adapter.collect())
    assert len(posts)==1 and posts[0].title=='Kosovo innovation workshop'
