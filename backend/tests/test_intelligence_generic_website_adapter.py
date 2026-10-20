import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.intelligence.generic_website_adapter import (
    GenericWebsiteAdapter, GenericWebsiteError, Page, discover_links,
    parse_html_article, sitemap_links,
)
from app.intelligence.collection import category_allowed


def test_discovery_keeps_only_same_site_articles_and_advertised_feed():
    html = '''<link rel="alternate" type="application/rss+xml" href="/feed.xml">
    <nav><a href="/about">About the organisation</a></nav>
    <main><article><a href="/calls/grant-for-startups">Grant for startups</a></article>
    <a href="https://other.example/grant">Grant from another site</a></main>'''
    links, feeds = discover_links(html, "https://example.org/news")
    assert links == [("https://example.org/calls/grant-for-startups", "Grant for startups")]
    assert feeds == ["https://example.org/feed.xml"]


def test_sitemap_rejects_external_and_entity_urls():
    xml = b'<urlset><url><loc>https://example.org/grants/new-call</loc></url><url><loc>http://127.0.0.1/grant</loc></url></urlset>'
    assert sitemap_links(xml, "https://example.org") == ["https://example.org/grants/new-call"]
    assert sitemap_links(b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><urlset/>', "https://example.org") == []


def test_article_extracts_content_and_publication_date():
    page = Page("https://example.org/grants/new-call", b'<meta property="article:published_time" content="2026-09-29T10:00:00Z"><main><h1>New grant for SMEs</h1><p>Applications are now open for technology companies in Kosovo. The deadline is 30 October.</p></main>', "text/html")
    post = parse_html_article(page, "New grant")
    assert post is not None
    assert post.title == "New grant for SMEs"
    assert post.published_at.isoformat() == "2026-09-29T10:00:00+00:00"


def test_redirect_to_another_host_is_not_fetched(monkeypatch):
    adapter = GenericWebsiteAdapter("https://example.org/news")
    monkeypatch.setattr("app.intelligence.generic_website_adapter._check_public_dns", AsyncMock())
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://other.example/private"})))
    try:
        try:
            asyncio.run(adapter._get(client, adapter.source_url))
        except GenericWebsiteError as exc:
            assert "outside" in str(exc)
        else:
            assert False, "Cross-site redirect was accepted"
    finally:
        asyncio.run(client.aclose())


def test_grant_only_source_rejects_other_news():
    assert category_allowed(["Grants"], "GRANT")
    assert not category_allowed(["Grants"], "TENDER")
    assert not category_allowed(["Grants"], "NEWS")
    assert category_allowed([], "NEWS")


def test_transient_server_error_is_retried_before_reading_page(monkeypatch):
    monkeypatch.setattr("app.intelligence.generic_website_adapter._check_public_dns", AsyncMock())
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200,
                              headers={"content-type": "text/html"}, content=b"<html>Recovered</html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            page = await GenericWebsiteAdapter("https://example.org/news")._get(client, "https://example.org/news")
            assert page.body == b"<html>Recovered</html>"

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize("status,attempts", [(403, 1), (500, 2), (526, 1)])
def test_persistent_http_errors_have_bounded_attempts(monkeypatch, status, attempts):
    monkeypatch.setattr("app.intelligence.generic_website_adapter._check_public_dns", AsyncMock())
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await GenericWebsiteAdapter("https://example.org/")._get(client, "https://example.org/")
            assert error.value.response.status_code == status

    asyncio.run(run())
    assert len(requests) == attempts


def test_generic_failure_is_saved_without_leaving_stale_source_status(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4
    from app.intelligence import collection

    source = SimpleNamespace(id=uuid4(), type="WEBSITE", status="ACTIVE", url="https://example.org/",
                             last_started_at=None, last_checked_at=None, last_error="Old failure", fetch_interval_minutes=60)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: source)),
                         rollback=AsyncMock(), get=AsyncMock(return_value=source), commit=AsyncMock())
    monkeypatch.setattr(GenericWebsiteAdapter, "collect", AsyncMock(side_effect=GenericWebsiteError("No public announcement links were found.")))

    assert asyncio.run(collection.check_source(db, source.id)) == "error"
    assert source.last_error == "No public announcement links were found."
    assert source.last_started_at is not None
    assert source.last_checked_at is None
