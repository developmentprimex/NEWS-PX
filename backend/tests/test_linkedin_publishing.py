"""Real transactions, mocked official LinkedIn HTTP; no live publishing."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from app.auth import User, require_admin
from app.config import settings
from app.db import get_db
from app.intelligence import linkedin_publishing as provider
from app.intelligence.linkedin_adapter import parse_posts
from app.intelligence.linkedin_publishing_models import LinkedInConnection, LinkedInOAuthState, LinkedInRepost
from app.intelligence.linkedin_publishing_router import router
from app.intelligence.models import NewsItem, NewsSource
from test_brightdata_postgres_integration import database, private_postgres  # noqa: F401

ORIGIN = "http://localhost:3101"
PARENT = "urn:li:ugcPost:123456789"
USER = User(uuid4(), "publisher@example.com")


@pytest.fixture
def publishing(database, monkeypatch):
    _, sessions = database
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "NEWS_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_ORGANIZATION_ENABLED", True)
    app = FastAPI()
    app.include_router(router, prefix="/api/intelligence")
    app.dependency_overrides[require_admin] = lambda: USER
    async def get_test_db():
        async with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = get_test_db
    return app, sessions


async def seed(sessions, *, parent=PARENT, user=USER, author="urn:li:person:member", source_type="LINKEDIN"):
    async with sessions() as db:
        source = NewsSource(name="Example", type=source_type, url="https://www.linkedin.com/company/example/")
        db.add(source)
        await db.flush()
        item = NewsItem(source_id=source.id, url="https://www.linkedin.com/posts/example_activity-987654321-abcd",
                        title="A useful post", linkedin_data={"postUrn": parent} if parent else {})
        connection = LinkedInConnection(user_id=user.id, author_urn=author, member_urn="urn:li:person:member",
            display_name="Example Publisher", encrypted_token=provider.token_cipher().encrypt(b"private-access-token").decode(),
            scopes=["w_member_social" if ":person:" in author else "w_organization_social"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=5))
        db.add_all([item, connection])
        await db.commit()
        return item.id, connection.id


def fake_provider(monkeypatch, responder):
    requests = []
    async def respond(request):
        requests.append(request)
        result = responder(request)
        return await result if hasattr(result, "__await__") else result
    monkeypatch.setattr(provider, "api_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return requests


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN})


def approval(connection_id, **changes):
    return {"connection_id": str(connection_id), "parent_urn": PARENT, "commentary": "Our thoughts", "approved": True, **changes}


def test_native_identity_never_converts_activity_numbers():
    assert provider.native_post_urn("urn:li:activity:123") is None
    assert provider.native_post_urn("123") is None
    assert provider.native_post_urn("https://www.linkedin.com/posts/test_activity-123-abc") is None
    assert provider.native_post_urn("https://evil.example/feed/update/urn:li:share:123/") is None
    assert provider.native_post_urn("https://www.linkedin.com/feed/update/urn%3Ali%3AugcPost%3A123/") == "urn:li:ugcPost:123"
    assert provider.native_post_urn(PARENT) == PARENT
    assert not provider.public_post_url("https://www.linkedin.com.evil.example/posts/a")
    assert not provider.public_post_url("https://user@www.linkedin.com/posts/a")
    assert not provider.public_post_url("https://www.linkedin.com:8000/posts/a")


def test_public_resolution_uses_only_the_main_post_canonical_identity():
    assert provider.parent_from_html('<a href="https://www.linkedin.com/feed/update/urn:li:share:111/">Suggested</a>') is None
    assert provider.parent_from_html('<meta property="og:url" content="https://www.linkedin.com/feed/update/urn:li:share:111/">') == "urn:li:share:111"
    assert provider.parent_from_html('<meta property="og:url" content="urn:li:share:111"><link rel="canonical" href="urn:li:share:222">') is None


def test_collector_preserves_native_urn_but_does_not_invent_one():
    source = SimpleNamespace(name="Example", url="https://www.linkedin.com/company/example/", type="LINKEDIN")
    record = {"url": "https://www.linkedin.com/feed/update/urn:li:ugcPost:123/", "post_text": "Post", "id": "urn:li:ugcPost:123"}
    assert parse_posts([record], source)[0].linkedin_data["postUrn"] == "urn:li:ugcPost:123"
    record.update(id="123", url="https://www.linkedin.com/posts/example_activity-123-abc")
    assert parse_posts([record], source)[0].linkedin_data["postUrn"] is None


def test_duplicate_approval_across_tabs_posts_once_and_survives_reconnect(publishing, monkeypatch):
    app, sessions = publishing
    async def respond(request):
        await asyncio.sleep(0.05)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:999"})
    requests = fake_provider(monkeypatch, respond)
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            responses = await asyncio.gather(*[web.post(f"/api/intelligence/items/{item}/repost", json=approval(account)) for _ in range(2)])
            assert all(response.status_code == 200 for response in responses)
            assert {r.json()["status"] for r in responses} <= {"publishing", "published"}
            result = (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))).json()
            assert result["status"] == "published" and result["url"].endswith("urn:li:share:999/")
            # The same original post collected under another source still deduplicates.
            async with sessions() as db:
                original = await db.get(NewsItem, item)
                duplicate = NewsItem(source_id=original.source_id, url=original.url + "?duplicate=1", title="Duplicate", linkedin_data={"postUrn": PARENT})
                db.add(duplicate)
                await db.commit()
                duplicate_id = duplicate.id
            duplicate_result = (await web.post(f"/api/intelligence/items/{duplicate_id}/repost", json=approval(account))).json()
            assert duplicate_result["id"] == result["id"]
            await web.delete(f"/api/intelligence/linkedin/connections/{account}")
            async with sessions() as db:
                row = await db.get(LinkedInConnection, account)
                assert row.encrypted_token is None
                row.encrypted_token = provider.token_cipher().encrypt(b"new-token").decode()
                await db.commit()
            assert (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))).json()["status"] == "published"
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["author"] == "urn:li:person:member"
        assert body["reshareContext"]["parent"] == PARENT
        assert body["visibility"] == "PUBLIC" and body["commentary"] == "Our thoughts"
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["timeout", "server", "missing-id", "conflict"])
def test_uncertain_outcome_never_resends(publishing, monkeypatch, outcome):
    app, sessions = publishing
    def respond(request):
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-token must not leak", request=request)
        return httpx.Response({"server": 503, "missing-id": 201, "conflict": 409}[outcome])
    requests = fake_provider(monkeypatch, respond)
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            for _ in range(2):
                response = await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))
                assert response.json()["status"] == "unknown"
                assert "private-token" not in response.text
            assert len(requests) == 1
    asyncio.run(run())


def test_definitive_rejection_requires_new_explicit_approval(publishing, monkeypatch):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, lambda request: httpx.Response(403, json={"message": "private provider debug"}))
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            response = await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))
            assert response.json()["status"] == "failed" and "private provider" not in response.text
            for _ in range(2):
                assert (await web.get(f"/api/intelligence/items/{item}/repost")).status_code == 200
            assert len(requests) == 1
            assert (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account, approved=False))).status_code == 400
            assert len(requests) == 1
            assert (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))).json()["status"] == "failed"
            assert len(requests) == 2
    asyncio.run(run())


def test_security_and_ownership_checks_never_publish(publishing, monkeypatch):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, lambda request: httpx.Response(201))
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            path = f"/api/intelligence/items/{item}/repost"
            assert (await web.post(path, json=approval(account), headers={"Origin": "https://evil.example"})).status_code == 403
            assert (await web.post(path, json=approval(account, parent_urn="urn:li:share:777"))).status_code == 409
            assert (await web.post(path, json=approval(account, commentary="x" * 3001))).status_code == 422
            assert (await web.post(path, json=approval(account, parent_urn="urn:li:activity:123"))).status_code == 422
            app.dependency_overrides[require_admin] = lambda: User(uuid4(), "other@example.com")
            assert (await web.get("/api/intelligence/linkedin/connections")).json()["connections"] == []
            assert (await web.delete(f"/api/intelligence/linkedin/connections/{account}")).status_code == 404
            assert (await web.post(path, json=approval(account))).status_code == 409
            del app.dependency_overrides[require_admin]
            assert (await web.post(path, json=approval(account))).status_code == 401
        assert not requests
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["expired", "disconnected", "wrong-scope", "disabled-company", "not-linkedin"])
def test_unusable_accounts_and_non_linkedin_items_cannot_publish(publishing, monkeypatch, kind):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, lambda request: httpx.Response(201))
    async def run():
        item, account = await seed(sessions, author="urn:li:organization:123" if kind == "disabled-company" else "urn:li:person:member", source_type="WEBSITE" if kind == "not-linkedin" else "LINKEDIN")
        async with sessions() as db:
            row = await db.get(LinkedInConnection, account)
            if kind == "expired": row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            if kind == "disconnected": row.encrypted_token = None
            if kind == "wrong-scope": row.scopes = []
            if kind == "disabled-company": monkeypatch.setattr(settings, "NEWS_LINKEDIN_ORGANIZATION_ENABLED", False)
            await db.commit()
        async with client(app) as web:
            response = await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))
            assert response.status_code == (404 if kind == "not-linkedin" else 409)
        assert not requests
    asyncio.run(run())


def test_prepare_follows_only_public_linkedin_posts_and_never_publishes(publishing, monkeypatch):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://evil.example/secret"}))
    async def run():
        item, _ = await seed(sessions, parent=None)
        async with client(app) as web:
            response = await web.post(f"/api/intelligence/items/{item}/repost/prepare")
            assert response.json()["parentUrn"] is None
            assert len(requests) == 1 and requests[0].method == "GET"
            requests.clear()
            fake_provider(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=f'<meta property="og:url" content="https://www.linkedin.com/feed/update/{PARENT}/">'))
            response = await web.post(f"/api/intelligence/items/{item}/repost/prepare")
            assert response.json()["parentUrn"] == PARENT
    asyncio.run(run())


def oauth_responder(request):
    if request.url.path.endswith("accessToken"):
        return httpx.Response(200, json={"access_token": "private-oauth-token", "expires_in": 3600})
    if request.url.path.endswith("userinfo"):
        return httpx.Response(200, json={"sub": "member", "name": "Our CEO"})
    if request.url.path.endswith("organizationAcls"):
        return httpx.Response(200, json={"elements": [{"role": "ADMINISTRATOR", "state": "APPROVED", "roleAssignee": "urn:li:person:member", "organization": "urn:li:organization:123"}]})
    return httpx.Response(200, json={"localizedName": "Our Company"})


@pytest.mark.parametrize("company", [False, True])
def test_oauth_is_browser_bound_single_use_and_tokens_are_encrypted(publishing, monkeypatch, company):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, oauth_responder)
    async def run():
        async with client(app) as web:
            response = await web.post("/api/intelligence/linkedin/connect", json={"organization_id": "123"} if company else {})
            assert response.status_code == 200
            params = parse_qs(urlparse(response.json()["authorizationUrl"]).query)
            assert params["redirect_uri"] == [ORIGIN + provider.CALLBACK_PATH]
            assert "private" not in response.text
            state = params["state"][0]
            async with client(app) as wrong_browser:
                assert (await wrong_browser.get(provider.CALLBACK_PATH, params={"state": state, "code": "code"})).status_code == 400
            assert not requests
            callback = await web.get(provider.CALLBACK_PATH, params={"state": state, "code": "private-code"})
            assert callback.status_code == 303 and "linkedin=connected" in callback.headers["location"]
            assert "private" not in callback.text + callback.headers["location"]
            assert (await web.get(provider.CALLBACK_PATH, params={"state": state, "code": "code"})).status_code == 400
            accounts = await web.get("/api/intelligence/linkedin/connections")
            account = accounts.json()["connections"][0]
            assert account["kind"] == ("COMPANY" if company else "PERSONAL")
            assert account["name"] == ("Our Company" if company else "Our CEO")
            assert "token" not in accounts.text and "member_urn" not in accounts.text
            async with sessions() as db:
                row = await db.scalar(select(LinkedInConnection))
                assert row.encrypted_token != "private-oauth-token"
                assert provider.decrypt_token(row.encrypted_token) == "private-oauth-token"
                assert await db.scalar(select(LinkedInOAuthState)) is None
    asyncio.run(run())


def test_company_connection_requires_verified_role(publishing, monkeypatch):
    app, _ = publishing
    def respond(request):
        if request.url.path.endswith("organizationAcls"):
            return httpx.Response(200, json={"elements": [{"role": "ANALYST", "state": "APPROVED", "roleAssignee": "urn:li:person:member", "organization": "urn:li:organization:123"}]})
        return oauth_responder(request)
    requests = fake_provider(monkeypatch, respond)
    async def run():
        async with client(app) as web:
            response = await web.post("/api/intelligence/linkedin/connect", json={"organization_id": "123"})
            state = parse_qs(urlparse(response.json()["authorizationUrl"]).query)["state"][0]
            result = await web.get(provider.CALLBACK_PATH, params={"state": state, "code": "code"})
            assert "linkedin=error" in result.headers["location"]
            assert (await web.get("/api/intelligence/linkedin/connections")).json()["connections"] == []
            assert all(request.url.path != "/rest/posts" for request in requests)
    asyncio.run(run())


def test_crash_after_durable_claim_cannot_publish_again(publishing, monkeypatch):
    app, sessions = publishing
    def crash(request):
        raise RuntimeError("Simulated process failure")
    requests = fake_provider(monkeypatch, crash)
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            with pytest.raises(RuntimeError, match="Simulated process failure"):
                await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))
            response = await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))
            assert response.json()["status"] == "publishing"
            assert len(requests) == 1
    asyncio.run(run())


def test_revoked_credential_requires_reconnect_before_retry(publishing, monkeypatch):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, lambda request: httpx.Response(401))
    async def run():
        item, account = await seed(sessions)
        async with client(app) as web:
            assert (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))).json()["status"] == "failed"
            assert (await web.get("/api/intelligence/linkedin/connections")).json()["connections"][0]["usable"] is False
            assert (await web.post(f"/api/intelligence/items/{item}/repost", json=approval(account))).status_code == 409
            assert len(requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["expired", "wrong-state", "cancelled"])
def test_oauth_rejections_do_not_exchange_or_save_a_token(publishing, monkeypatch, reason):
    app, sessions = publishing
    requests = fake_provider(monkeypatch, oauth_responder)
    async def run():
        async with client(app) as web:
            response = await web.post("/api/intelligence/linkedin/connect", json={})
            state = parse_qs(urlparse(response.json()["authorizationUrl"]).query)["state"][0]
            if reason == "expired":
                async with sessions() as db:
                    row = await db.scalar(select(LinkedInOAuthState))
                    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await db.commit()
            params = {"state": "wrong-state" if reason == "wrong-state" else state, "code": "code"}
            if reason == "cancelled": params["error"] = "user_cancelled_authorize"
            result = await web.get(provider.CALLBACK_PATH, params=params)
            assert result.status_code == (303 if reason == "cancelled" else 400)
            assert not requests
            assert (await web.get("/api/intelligence/linkedin/connections")).json()["connections"] == []
    asyncio.run(run())
