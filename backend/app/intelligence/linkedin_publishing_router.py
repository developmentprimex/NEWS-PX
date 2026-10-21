import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import User, require_admin
from app.config import settings
from app.db import get_db
from app.intelligence import linkedin_publishing as provider
from app.intelligence.linkedin_publishing_models import LinkedInConnection, LinkedInOAuthState, LinkedInRepost
from app.intelligence.models import NewsItem, NewsSource

router = APIRouter()
STATE_COOKIE = "news_linkedin_oauth"


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def same_origin(request: Request) -> None:
    if request.headers.get("origin") != settings.NEWS_PUBLIC_ORIGIN.rstrip("/"):
        raise HTTPException(403, "Open this action from the configured News website.")


def require_configuration() -> None:
    if not provider.configured():
        raise HTTPException(503, "LinkedIn publishing needs server configuration. See the connection panel.")


def connection_result(row: LinkedInConnection) -> dict:
    return {"id": str(row.id), "authorUrn": row.author_urn, "name": row.display_name,
            "kind": "COMPANY" if row.author_urn.startswith("urn:li:organization:") else "PERSONAL",
            "expiresAt": row.expires_at.isoformat(), "connected": bool(row.encrypted_token),
            "usable": bool(row.encrypted_token) and row.expires_at > now()}


def receipt_result(row: LinkedInRepost) -> dict:
    return {"id": str(row.id), "authorUrn": row.author_urn, "parentUrn": row.parent_urn,
            "status": row.status, "message": row.message, "commentary": row.commentary,
            "approvedAt": row.approved_at.isoformat(),
            "publishedAt": row.published_at.isoformat() if row.published_at else None,
            "url": f"https://www.linkedin.com/feed/update/{row.post_urn}/" if row.post_urn else None}


class ConnectInput(BaseModel):
    organization_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")


class RepostInput(BaseModel):
    connection_id: uuid.UUID
    parent_urn: str = Field(pattern=r"^urn:li:(share|ugcPost):[0-9]{1,30}$")
    commentary: str = Field(default="", max_length=3000)
    approved: bool


@router.get("/linkedin/connections")
async def connections(response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    rows = await db.scalars(select(LinkedInConnection).where(LinkedInConnection.user_id == user.id)
                            .order_by(LinkedInConnection.connected_at))
    return {"configured": provider.configured(), "callbackUrl": provider.callback_url(),
            "companyEnabled": settings.NEWS_LINKEDIN_ORGANIZATION_ENABLED,
            "connections": [connection_result(row) for row in rows]}


@router.post("/linkedin/connect", dependencies=[Depends(same_origin)])
async def connect(payload: ConnectInput, response: Response, db: AsyncSession = Depends(get_db),
                  user: User = Depends(require_admin)) -> dict:
    require_configuration()
    if payload.organization_id and not settings.NEWS_LINKEDIN_ORGANIZATION_ENABLED:
        raise HTTPException(409, "Company publishing requires approved Community Management permissions on the server.")
    scopes = ["openid", "profile"] + (["w_organization_social", "r_organization_admin"]
              if payload.organization_id else ["w_member_social"])
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await db.execute(delete(LinkedInOAuthState).where(or_(LinkedInOAuthState.expires_at < now(),
                                                        LinkedInOAuthState.user_id == user.id)))
    db.add(LinkedInOAuthState(state_hash=digest(state), user_id=user.id, browser_hash=digest(browser),
        organization_id=payload.organization_id, scopes=scopes, expires_at=now() + timedelta(minutes=10)))
    await db.commit()
    response.set_cookie(STATE_COOKIE, browser, max_age=600, path=provider.CALLBACK_PATH,
                        httponly=True, secure=settings.NEWS_COOKIE_SECURE, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return {"authorizationUrl": "https://www.linkedin.com/oauth/v2/authorization?" + urlencode({
        "response_type": "code", "client_id": settings.NEWS_LINKEDIN_CLIENT_ID,
        "redirect_uri": provider.callback_url(), "state": state, "scope": " ".join(scopes)})}


@router.get("/linkedin/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = "",
                   db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> Response:
    require_configuration()
    saved = await db.scalar(select(LinkedInOAuthState).where(LinkedInOAuthState.state_hash == digest(state)).with_for_update())
    browser = request.cookies.get(STATE_COOKIE, "")
    if (not saved or saved.user_id != user.id or saved.expires_at <= now() or not browser
            or not hmac.compare_digest(saved.browser_hash, digest(browser))):
        raise HTTPException(400, "LinkedIn authorization expired or did not match this browser. Connect again.")
    scopes, organization_id = saved.scopes, saved.organization_id
    await db.delete(saved)
    await db.commit()  # Consume before the exchange, including cancellations/failures.
    result = "denied" if error else "error"
    if not error and code:
        try:
            identity = await provider.exchange_code(code, scopes, organization_id)
            values = dict(user_id=user.id, author_urn=identity["author"], member_urn=identity["member"],
                display_name=identity["name"], encrypted_token=provider.token_cipher().encrypt(identity["token"].encode()).decode(),
                scopes=scopes, expires_at=now() + timedelta(seconds=identity["expires_in"]), connected_at=now())
            statement = insert(LinkedInConnection).values(**values)
            await db.execute(statement.on_conflict_do_update(constraint="uq_linkedin_connection_author",
                set_={key: value for key, value in values.items() if key not in {"user_id", "author_urn"}}))
            await db.commit()
            result = "connected"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # Never put tokens, codes, raw provider errors or secrets in a URL.
            result = "error"
    response = RedirectResponse(settings.NEWS_PUBLIC_ORIGIN.rstrip("/") + provider.RETURN_PATH + "?linkedin=" + result, status_code=303)
    response.delete_cookie(STATE_COOKIE, path=provider.CALLBACK_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.delete("/linkedin/connections/{connection_id}", dependencies=[Depends(same_origin)])
async def disconnect(connection_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    row = await db.scalar(select(LinkedInConnection).where(LinkedInConnection.id == connection_id,
        LinkedInConnection.user_id == user.id).with_for_update())
    if not row:
        raise HTTPException(404, "LinkedIn connection not found.")
    # Keep the stable author and receipts so reconnecting cannot bypass deduplication.
    row.encrypted_token = None
    await db.commit()
    return {"disconnected": True}


async def linkedin_item(db: AsyncSession, item_id: uuid.UUID) -> NewsItem:
    row = await db.scalar(select(NewsItem).join(NewsSource, NewsSource.id == NewsItem.source_id)
                          .where(NewsItem.id == item_id, NewsSource.type == "LINKEDIN"))
    if not row:
        raise HTTPException(404, "LinkedIn post not found.")
    return row


async def item_result(db: AsyncSession, item: NewsItem, user: User) -> dict:
    parent = provider.stored_parent(item)
    rows = await db.scalars(select(LinkedInRepost).where(LinkedInRepost.user_id == user.id,
        or_(LinkedInRepost.item_id == item.id, LinkedInRepost.parent_urn == parent if parent else False))
        .order_by(LinkedInRepost.approved_at.desc()))
    return {"parentUrn": parent, "receipts": [receipt_result(row) for row in rows]}


@router.get("/items/{item_id}/repost")
async def repost_status(item_id: uuid.UUID, response: Response, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return await item_result(db, await linkedin_item(db, item_id), user)


@router.post("/items/{item_id}/repost/prepare", dependencies=[Depends(same_origin)])
async def prepare(item_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    item = await linkedin_item(db, item_id)
    parent = await provider.resolve_parent(item)
    if parent:
        # Lock and merge after the network read; do not overwrite a collector's
        # concurrent engagement/comment update.
        await db.refresh(item, with_for_update=True)
        item.linkedin_data = {**(item.linkedin_data or {}), "postUrn": parent}
        await db.commit()
    return await item_result(db, item, user)


@router.post("/items/{item_id}/repost", dependencies=[Depends(same_origin)])
async def approve_repost(item_id: uuid.UUID, payload: RepostInput, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)) -> dict:
    require_configuration()
    if payload.approved is not True:
        raise HTTPException(400, "Explicit repost approval is required.")
    item = await linkedin_item(db, item_id)
    parent = provider.stored_parent(item)
    if parent and payload.parent_urn != parent:
        raise HTTPException(409, "The post identifier changed. Review the post again.")
    parent = parent or payload.parent_urn  # An explicitly reviewed native link can identify an older post.
    connection = await db.scalar(select(LinkedInConnection).where(LinkedInConnection.id == payload.connection_id,
        LinkedInConnection.user_id == user.id).with_for_update())
    if not connection or not connection.encrypted_token or connection.expires_at <= now():
        raise HTTPException(409, "Connect or reconnect the LinkedIn account before approving.")
    scope = "w_organization_social" if connection.author_urn.startswith("urn:li:organization:") else "w_member_social"
    if scope not in connection.scopes:
        raise HTTPException(409, "Reconnect this account with permission to publish.")
    if scope == "w_organization_social" and not settings.NEWS_LINKEDIN_ORGANIZATION_ENABLED:
        raise HTTPException(409, "Company publishing is disabled on the server.")
    token = provider.decrypt_token(connection.encrypted_token)
    values = dict(user_id=user.id, item_id=item.id, author_urn=connection.author_urn, parent_urn=parent,
                  commentary=payload.commentary, status="publishing", approved_at=now())
    inserted = await db.scalar(insert(LinkedInRepost).values(**values).on_conflict_do_nothing(
        constraint="uq_linkedin_repost_author_parent").returning(LinkedInRepost.id))
    row = await db.scalar(select(LinkedInRepost).where(LinkedInRepost.user_id == user.id,
        LinkedInRepost.author_urn == connection.author_urn, LinkedInRepost.parent_urn == parent).with_for_update())
    if not inserted:
        if row.status != "failed":
            return receipt_result(row)
        for key, value in values.items():
            setattr(row, key, value)
        row.message = None
    # This durable claim survives a crash/restart. Publishing/unknown are never
    # automatically retried, even after reconnecting or clicking in another tab.
    await db.commit()
    try:
        async with provider.api_client() as client:
            response = await client.post("https://api.linkedin.com/rest/posts", headers=provider.api_headers(token),
                json=provider.repost_payload(connection.author_urn, parent, payload.commentary))
        post_urn = provider.native_post_urn(response.headers.get("x-restli-id"))
        if response.status_code == 201 and post_urn:
            row.status, row.post_urn, row.published_at = "published", post_urn, now()
            row.message = "Reposted on LinkedIn."
        elif 400 <= response.status_code < 500 and response.status_code not in {408, 409}:
            row.status = "failed"
            messages = {401: "LinkedIn authorization expired or was revoked. Reconnect this account.",
                403: "LinkedIn refused permission. Check app access, your Page role, and whether this post allows reposting.",
                404: "LinkedIn could not find an accessible post for this identifier.",
                422: "LinkedIn rejected this repost. Check the post identifier and whether reposting is allowed.",
                429: "LinkedIn's request limit was reached. Wait before approving another attempt."}
            row.message = messages.get(response.status_code, "LinkedIn rejected this repost. Review the post and app permissions before trying again.")
            if response.status_code == 401:
                await db.refresh(connection, with_for_update=True)
                # A simultaneous reconnect must not be invalidated by this old token.
                if connection.encrypted_token and provider.decrypt_token(connection.encrypted_token) == token:
                    connection.expires_at = now()
        else:
            row.status = "unknown"
            row.message = "LinkedIn did not confirm the outcome. Check your LinkedIn feed; another attempt is blocked to prevent a duplicate."
    except httpx.HTTPError:
        row.status = "unknown"
        row.message = "The connection ended without confirmation. Check your LinkedIn feed; another attempt is blocked to prevent a duplicate."
    await db.commit()
    return receipt_result(row)
