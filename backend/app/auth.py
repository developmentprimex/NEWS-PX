import hmac
import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from starlette.requests import Request
from sqlalchemy import Boolean, DateTime, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base, get_db

router = APIRouter()
COOKIE_NAME = "news_session"
ADMIN_ID = uuid.uuid5(uuid.NAMESPACE_DNS, f"news-admin:{settings.NEWS_ADMIN_EMAIL.casefold()}")


class MemberAccount(Base):
    __tablename__ = 'news_member_accounts'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def migrate_users(connection) -> None:
    # Add ownership in place: existing IDs, subscriptions, receipts and settings survive.
    await connection.execute(text(f"ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS owner_id UUID NOT NULL DEFAULT '{ADMIN_ID}'"))
    await connection.execute(text('CREATE INDEX IF NOT EXISTS ix_intelligence_sources_owner_id ON intelligence_sources (owner_id)'))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'scrypt${salt.hex()}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, salt, expected = encoded.split('$')
        if kind != 'scrypt':
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def user_recipient(user: 'User') -> str:
    return settings.NEWS_EMAIL_RECIPIENT if user.id == ADMIN_ID else user.email


async def account_recipient(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    if user_id == ADMIN_ID:
        return settings.NEWS_EMAIL_RECIPIENT
    account = await db.get(MemberAccount, user_id, populate_existing=True)
    return account.email if account and account.active else None


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    email: str
    role: str = "ADMIN"


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(max_length=1024)


class CreateMemberInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)


class UpdateMemberInput(BaseModel):
    active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=1024)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue")
    try:
        claims = jwt.decode(token, settings.NEWS_SESSION_SECRET, algorithms=["HS256"])
        user_id = uuid.UUID(claims['sub'])
        if user_id == ADMIN_ID:
            return User(id=ADMIN_ID, email=settings.NEWS_ADMIN_EMAIL)
        account = await db.get(MemberAccount, user_id)
        if not account or not account.active or claims.get('sv') != account.session_version:
            raise JWTError('Unknown user')
        return User(id=account.id, email=account.email, role='MEMBER')
    except (JWTError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired") from exc


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != 'ADMIN':
        raise HTTPException(status_code=403, detail='Administrator access required')
    return user


@router.post("/login")
async def login(payload: LoginInput, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    email_ok = hmac.compare_digest(payload.email.casefold().encode(), settings.NEWS_ADMIN_EMAIL.casefold().encode())
    password_ok = hmac.compare_digest(payload.password.encode(), settings.NEWS_ADMIN_PASSWORD.encode())
    version = None
    if email_ok and password_ok:
        user = User(id=ADMIN_ID, email=settings.NEWS_ADMIN_EMAIL)
    else:
        account = await db.scalar(select(MemberAccount).where(MemberAccount.email == payload.email.casefold()))
        if not account or not account.active or not await asyncio.to_thread(verify_password, payload.password, account.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect email or password")
        user = User(id=account.id, email=account.email, role='MEMBER')
        version = account.session_version
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    token = jwt.encode({"sub": str(user.id), "exp": expires, 'sv': version}, settings.NEWS_SESSION_SECRET, algorithm="HS256")
    response.set_cookie(
        COOKIE_NAME, token, max_age=7 * 24 * 60 * 60, path="/api", httponly=True,
        secure=settings.NEWS_COOKIE_SECURE, samesite="lax",
    )
    return {"id": str(user.id), "email": user.email, "role": user.role}


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/api")


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict[str, str]:
    return {"id": str(user.id), "email": user.email, "role": user.role}


def _member_out(account: MemberAccount) -> dict:
    return {'id': str(account.id), 'email': account.email, 'role': 'MEMBER',
            'active': account.active, 'createdAt': account.created_at}


@router.get('/users')
async def list_users(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    members = await db.scalars(select(MemberAccount).order_by(MemberAccount.created_at, MemberAccount.id))
    return [{'id': str(ADMIN_ID), 'email': settings.NEWS_ADMIN_EMAIL, 'role': 'ADMIN', 'active': True, 'createdAt': None},
            *[_member_out(member) for member in members]]


@router.post('/users', status_code=201)
async def create_user(payload: CreateMemberInput, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    email = payload.email.casefold()
    if email == settings.NEWS_ADMIN_EMAIL.casefold():
        raise HTTPException(status_code=409, detail='An account with this email already exists.')
    account = MemberAccount(email=email, password_hash=await asyncio.to_thread(hash_password, payload.password))
    db.add(account)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail='An account with this email already exists.') from exc
    await db.refresh(account)
    return _member_out(account)


@router.put('/users/{user_id}')
async def update_user(user_id: uuid.UUID, payload: UpdateMemberInput, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    account = await db.scalar(select(MemberAccount).where(MemberAccount.id == user_id).with_for_update())
    if not account:
        raise HTTPException(status_code=404, detail='Member account not found')
    if payload.active is not None and payload.active != account.active:
        account.active = payload.active
        account.session_version += 1
    if payload.password is not None:
        account.password_hash = await asyncio.to_thread(hash_password, payload.password)
        account.session_version += 1
    await db.commit()
    return _member_out(account)
