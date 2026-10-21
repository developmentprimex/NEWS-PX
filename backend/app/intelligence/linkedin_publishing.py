"""Official LinkedIn publishing. Never guess a share URN from an activity ID."""
import re
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app.config import settings

CALLBACK_PATH = "/api/intelligence/linkedin/callback"
RETURN_PATH = "/intelligence/primex-linkedin"
POST_URN = re.compile(r"urn:li:(?:share|ugcPost):[0-9]{1,30}")


def token_cipher() -> Fernet:
    try:
        return Fernet((settings.NEWS_LINKEDIN_TOKEN_KEY or "").encode())
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "Configure the LinkedIn token encryption key on the server.") from exc


def configured() -> bool:
    if not settings.NEWS_LINKEDIN_CLIENT_ID or not settings.NEWS_LINKEDIN_CLIENT_SECRET:
        return False
    try:
        token_cipher()
        return bool(re.fullmatch(r"20\d{4}", settings.NEWS_LINKEDIN_API_VERSION))
    except HTTPException:
        return False


def callback_url() -> str:
    return settings.NEWS_PUBLIC_ORIGIN.rstrip("/") + CALLBACK_PATH


def decrypt_token(value: str) -> str:
    try:
        return token_cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(409, "Reconnect this LinkedIn account; its saved credential is unavailable.") from exc


def api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "LinkedIn-Version": settings.NEWS_LINKEDIN_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0"}


def api_client() -> httpx.AsyncClient:
    # No automatic transport retries: a POST may have succeeded before a timeout.
    return httpx.AsyncClient(timeout=20, follow_redirects=False)


def native_post_urn(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = unquote(value.strip())
    if POST_URN.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname in {"linkedin.com", "www.linkedin.com"} and not parsed.username:
        match = re.fullmatch(r"/feed/update/(urn:li:(?:share|ugcPost):\d{1,30})/?", parsed.path)
        return match.group(1) if match else None
    return None


def stored_parent(item) -> str | None:
    # This targets the displayed post. Do not silently substitute its original
    # parent when the displayed post itself is a quote/repost.
    data = item.linkedin_data or {}
    return next((urn for value in (data.get("postUrn"), item.external_id, item.url)
                 if (urn := native_post_urn(value))), None)


def parent_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # Only main-document canonical identity, never recommended/embedded posts.
    values = [node.get("content") for node in soup.select('meta[property="og:url"]')]
    values += [node.get("href") for node in soup.select('link[rel="canonical"]')]
    urns = {urn for value in values if (urn := native_post_urn(value))}
    return next(iter(urns)) if len(urns) == 1 else None


def public_post_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (parsed.scheme == "https" and parsed.hostname in {"linkedin.com", "www.linkedin.com"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and (parsed.path.startswith("/posts/") or parsed.path.startswith("/feed/update/")))
    except ValueError:
        return False


async def resolve_parent(item) -> str | None:
    known = stored_parent(item)
    if known:
        return known
    if not public_post_url(item.url):
        return None
    try:
        async with api_client() as client:
            url = item.url
            for _ in range(3):
                # Redirects to login, another host or arbitrary URLs are refused.
                if not public_post_url(url):
                    return None
                async with client.stream("GET", url, headers={"Accept": "text/html"}) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get("location", "")))
                        continue
                    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                        return None
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1_000_000:
                            return None
                    return parent_from_html(content.decode("utf-8", errors="replace"))
    except httpx.HTTPError:
        pass
    return None


async def exchange_code(code: str, scopes: list[str], organization_id: str | None) -> dict:
    async with api_client() as client:
        response = await client.post("https://www.linkedin.com/oauth/v2/accessToken", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": callback_url(),
            "client_id": settings.NEWS_LINKEDIN_CLIENT_ID, "client_secret": settings.NEWS_LINKEDIN_CLIENT_SECRET,
        })
        response.raise_for_status()
        credentials = response.json()
        token = credentials["access_token"]
        if not isinstance(token, str) or not token or not 0 < int(credentials["expires_in"]) <= 366 * 86400:
            raise ValueError("Invalid token response")
        # LinkedIn may omit scope if it is identical to the requested scope.
        granted = credentials.get("scope")
        if granted is not None and not set(scopes).issubset(set(re.split(r"[ ,]+", granted))):
            raise ValueError("Missing granted scopes")
        profile_response = await client.get("https://api.linkedin.com/v2/userinfo", headers=api_headers(token))
        profile_response.raise_for_status()
        profile = profile_response.json()
        subject = profile["sub"]
        if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", subject):
            raise ValueError("Invalid subject")
        member = f"urn:li:person:{subject}"
        author, name = member, str(profile.get("name") or "LinkedIn member")[:200]
        if organization_id:
            author = f"urn:li:organization:{organization_id}"
            authorized = False
            for start in range(0, 1000, 100):
                acl = await client.get("https://api.linkedin.com/rest/organizationAcls", headers=api_headers(token),
                    params={"q": "roleAssignee", "state": "APPROVED", "count": 100, "start": start})
                acl.raise_for_status()
                payload = acl.json()
                if any(row.get("organization") == author and row.get("roleAssignee") == member
                       and row.get("state") == "APPROVED"
                       and row.get("role") in {"ADMINISTRATOR", "CONTENT_ADMINISTRATOR", "CONTENT_ADMIN"}
                       for row in payload.get("elements", [])):
                    authorized = True
                    break
                if not any(link.get("rel") == "next" for link in payload.get("paging", {}).get("links", [])):
                    break
            if not authorized:
                raise ValueError("Page publishing role not verified")
            organization = await client.get(f"https://api.linkedin.com/rest/organizations/{organization_id}",
                                            headers=api_headers(token))
            organization.raise_for_status()
            name = str(organization.json().get("localizedName") or f"Company {organization_id}")[:200]
        return {"author": author, "member": member, "name": name, "token": token,
                "expires_in": int(credentials["expires_in"])}


def repost_payload(author: str, parent: str, commentary: str) -> dict:
    # Commentary uses LinkedIn's little text format; escape user text so it is
    # displayed literally rather than interpreted as annotations/mentions.
    literal = re.sub(r"([\\|{}@\[\]()<>#*_~])", r"\\\1", commentary)
    return {"author": author, "commentary": literal, "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "reshareContext": {"parent": parent}}
