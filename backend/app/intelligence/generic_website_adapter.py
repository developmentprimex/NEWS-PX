"""Discover public announcements from ordinary websites without a custom connector."""

from __future__ import annotations

import asyncio
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

# Keep the Chromium binary alongside the virtual environment so staged Windows
# deployments can move both together without a machine-wide browser install.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
from playwright.async_api import async_playwright

from app.intelligence.rss_adapter import _check_public_dns, parse_feed, rss_url_is_supported
from app.intelligence.services import CollectedNewsItem

MAX_BODY = 4_000_000
MAX_LINKS = 35
REDIRECTS = {301, 302, 303, 307, 308}
SKIP_PATH = re.compile(r"/(?:login|signin|register|account|privacy|terms|contact|about|search|tag|author)(?:/|$)", re.I)
DOCUMENT_HINT = re.compile(r"grant|fund|call|tender|procure|opportun|apply|application|support|news|announcement|njoft|thirr|subvenc|konkurs|projekt|program|financ|mund[eë]si|afati", re.I)


class GenericWebsiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Page:
    url: str
    body: bytes
    content_type: str


def _same_site(url: str, source_url: str) -> bool:
    if not rss_url_is_supported(url):
        return False
    current, source = urlsplit(url), urlsplit(source_url)
    def bare(host: str | None) -> str:
        return (host or "").removeprefix("www.")
    return bare(current.hostname) == bare(source.hostname)


def _clean_url(url: str, base: str) -> str | None:
    resolved = urljoin(base, url.strip())
    if not _same_site(resolved, base):
        return None
    parts = urlsplit(resolved)
    if SKIP_PATH.search(parts.path):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _text(node: BeautifulSoup) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def discover_links(html: str, page_url: str) -> tuple[list[tuple[str, str]], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[str] = []
    for node in soup.select("link[rel][href]"):
        if "alternate" in node.get("rel", []) and any(value in node.get("type", "").lower() for value in ("rss", "atom")):
            link = _clean_url(node["href"], page_url)
            if link:
                feeds.append(link)
    entries: dict[str, tuple[int, str]] = {}
    for link in soup.select("main a[href], article a[href], a[href]"):
        url = _clean_url(link.get("href", ""), page_url)
        if not url or url == page_url or url in entries:
            continue
        title = _text(link)
        if not 8 <= len(title) <= 500 or title.lower() in {"read more", "learn more", "more", "lexo më shumë"}:
            continue
        path = urlsplit(url).path
        if path in {"", "/"} or path.rstrip("/") == urlsplit(page_url).path.rstrip("/"):
            continue
        context = link.parent
        context_text = _text(context)[:350] if context else title
        score = 10 if DOCUMENT_HINT.search(title + " " + path) else 0
        score += 3 if link.find_parent("article") else 0
        score += 2 if context and context.find_parent("main") else 0
        score += 2 if path.lower().endswith(".pdf") else 0
        entries[url] = (score, title if score else context_text[:500])
    ranked = sorted(entries.items(), key=lambda item: item[1][0], reverse=True)
    return [(url, title) for url, (_, title) in ranked[:MAX_LINKS]], feeds


def parse_publication_date(soup: BeautifulSoup) -> datetime | None:
    for selector, attr in (("meta[property='article:published_time']", "content"), ("meta[name='date']", "content"), ("time[datetime]", "datetime")):
        node = soup.select_one(selector)
        if not node or not node.get(attr):
            continue
        try:
            result = datetime.fromisoformat(node[attr].replace("Z", "+00:00"))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
        except ValueError:
            continue
    return None


def parse_html_article(page: Page, fallback_title: str) -> CollectedNewsItem | None:
    soup = BeautifulSoup(page.body, "html.parser")
    for node in soup.select("script, style, nav, footer, header, aside, form, noscript"):
        node.decompose()
    main = soup.select_one("article") or soup.select_one("main") or soup.select_one("body")
    if main is None:
        return None
    title_node = soup.select_one("h1")
    title = _text(title_node) if title_node else fallback_title
    body = _text(main)[:20_000]
    if len(title) < 8 or len(body) < 60:
        return None
    return CollectedNewsItem(page.url[:500], page.url, title[:500], body, parse_publication_date(soup))


def parse_pdf_article(page: Page, fallback_title: str) -> CollectedNewsItem | None:
    try:
        reader = PdfReader(io.BytesIO(page.body))
        if reader.is_encrypted:
            return None
        text = " ".join((part.extract_text() or "") for part in reader.pages[:12])
        text = " ".join(text.split())[:20_000]
    except Exception:
        return None
    if len(text) < 60:
        return None
    return CollectedNewsItem(page.url[:500], page.url, fallback_title[:500], text, None)


def sitemap_links(payload: bytes, source_url: str) -> list[str]:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I):
        return []
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return []
    urls: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
            continue
        url = _clean_url(node.text, source_url)
        if url and DOCUMENT_HINT.search(urlsplit(url).path):
            urls.append(url)
    return urls[:MAX_LINKS]


class GenericWebsiteAdapter:
    def __init__(self, source_url: str) -> None:
        if not rss_url_is_supported(source_url):
            raise GenericWebsiteError("Use a public HTTP or HTTPS website URL without credentials or a custom port.")
        self.source_url = source_url

    async def _get(self, client: httpx.AsyncClient, url: str) -> Page:
        try:
            return await self._get_once(client, url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {500, 502, 503, 504}:
                raise
        # Retry one transient read failure; access denials and certificate
        # problems need operator/site-owner action, not repeated requests.
        await asyncio.sleep(1)
        return await self._get_once(client, url)

    async def _get_once(self, client: httpx.AsyncClient, url: str) -> Page:
        for _ in range(4):
            if not _same_site(url, self.source_url):
                raise GenericWebsiteError("The page redirected outside the monitored website.")
            await _check_public_dns(url)
            async with client.stream("GET", url, headers={"User-Agent": "News-Intelligence/1.0"}) as response:
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise GenericWebsiteError("Website redirected without a destination.")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(kind in content_type for kind in ("html", "pdf", "xml", "rss", "atom", "octet-stream")):
                    raise GenericWebsiteError("Website returned an unsupported document format.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise GenericWebsiteError("Website document exceeds the 4 MB limit.")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not content_type or "octet-stream" in content_type:
                    if body.lstrip().startswith(b"%PDF"):
                        content_type = "application/pdf"
                    elif b"<html" in body[:1000].lower() or b"<!doctype html" in body[:1000].lower():
                        content_type = "text/html"
                    elif body.lstrip().startswith(b"<?xml"):
                        content_type = "application/xml"
                    else:
                        raise GenericWebsiteError("Website returned an unsupported document format.")
                return Page(url, body, content_type)
        raise GenericWebsiteError("Website redirected too many times.")

    async def _render_html(self, url: str) -> str:
        """Use a browser only when the server HTML has no discoverable links."""
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(service_workers="block")
                    async def safe_request(route):
                        request_url = route.request.url
                        if not rss_url_is_supported(request_url):
                            await route.abort()
                            return
                        try:
                            await _check_public_dns(request_url)
                        except Exception:
                            await route.abort()
                            return
                        if route.request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        await route.continue_()
                    await context.route("**/*", safe_request)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    await page.wait_for_timeout(1_500)
                    if not _same_site(page.url, self.source_url):
                        raise GenericWebsiteError("Browser navigation left the monitored website.")
                    html = await page.content()
                    return html[:MAX_BODY]
                finally:
                    await browser.close()
        except GenericWebsiteError:
            raise
        except Exception as exc:
            raise GenericWebsiteError(f"Browser could not read this website: {type(exc).__name__}.") from exc

    async def collect(self, max_items: int = MAX_LINKS) -> list[CollectedNewsItem]:
        max_items = max(1, min(max_items, MAX_LINKS))
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False, trust_env=False) as client:
            home = await self._get(client, self.source_url)
            if any(kind in home.content_type for kind in ("rss", "atom", "xml")):
                return parse_feed(home.body, home.url)[:max_items]
            if "pdf" in home.content_type:
                item = parse_pdf_article(home, urlsplit(home.url).path.rsplit("/", 1)[-1])
                return [item] if item else []
            links, feeds = discover_links(home.body.decode("utf-8", errors="replace"), home.url)
            for feed_url in feeds[:2]:
                try:
                    feed = await self._get(client, feed_url)
                    if "html" not in feed.content_type:
                        posts = parse_feed(feed.body, feed.url)
                        if posts:
                            return posts[:max_items]
                except (httpx.HTTPError, GenericWebsiteError, ValueError):
                    pass
            if not links or not any(DOCUMENT_HINT.search(url + " " + title) for url, title in links):
                try:
                    rendered = await self._render_html(home.url)
                    rendered_links, rendered_feeds = discover_links(rendered, home.url)
                    if rendered_links:
                        links = rendered_links
                    feeds.extend(rendered_feeds)
                except GenericWebsiteError:
                    pass
            if not links or not any(DOCUMENT_HINT.search(url + " " + title) for url, title in links):
                root = f"{urlsplit(home.url).scheme}://{urlsplit(home.url).netloc}/sitemap.xml"
                try:
                    sitemap = await self._get(client, root)
                    discovered = [(url, urlsplit(url).path.rsplit("/", 1)[-1].replace("-", " ")) for url in sitemap_links(sitemap.body, home.url)]
                    links = list(dict(links + discovered).items())[:MAX_LINKS]
                except (httpx.HTTPError, GenericWebsiteError):
                    pass
            if not links:
                raise GenericWebsiteError("No public announcement links were found. This site may require a browser or sign-in.")
            posts: list[CollectedNewsItem] = []
            for url, title in links[:max_items]:
                try:
                    page = await self._get(client, url)
                    item = parse_pdf_article(page, title) if "pdf" in page.content_type else parse_html_article(page, title) if "html" in page.content_type else None
                    if item:
                        posts.append(item)
                except (httpx.HTTPError, GenericWebsiteError):
                    continue
            if not posts:
                raise GenericWebsiteError("Announcement links were found, but their content could not be read.")
            return posts
