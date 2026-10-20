"""One scheduled email containing new updates from subscribed sources."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.intelligence.email_service import _smtp_reachable
from app.intelligence.models import (
    NewsAnalysis, NewsDigestDelivery, NewsDigestSettings,
    NewsItem, NewsSource, NewsSourceEmailSubscription,
)
from app.mail import GmailService

logger = logging.getLogger(__name__)
DIGEST_ZONE = ZoneInfo("Europe/Budapest")
DEFAULT_TIMES = ["12:30", "17:00", "21:00"]


def latest_slot(now: datetime, times: list[str]) -> datetime:
    local_now = now.astimezone(DIGEST_ZONE)
    slots = []
    for offset in (0, -1):
        day = local_now.date() + timedelta(days=offset)
        for value in times:
            hour, minute = map(int, value.split(":"))
            slot = datetime.combine(day, time(hour, minute), DIGEST_ZONE).astimezone(timezone.utc)
            if slot <= now:
                slots.append(slot)
    return max(slots)


async def ensure_digest_settings(db: AsyncSession, user_id: uuid.UUID) -> NewsDigestSettings:
    digest = await db.get(NewsDigestSettings, user_id)
    if digest is None:
        digest = NewsDigestSettings(user_id=user_id, times=DEFAULT_TIMES.copy(), last_slot_at=datetime.now(timezone.utc))
        db.add(digest)
        await db.flush()
    return digest


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def activity_kind(item: NewsItem, source: NewsSource) -> str:
    if source.type == 'FACEBOOK':
        return 'POST'
    if source.type == "LINKEDIN":
        return str((item.linkedin_data or {}).get("kind") or "POST").upper()
    return "NEWS" if source.type in {"RSS", "WEBSITE"} else "ACTIVITY"


ACTIVITY_LABELS = {"NEWS": "Lajm", "POST": "Postim", "REPOST": "Repostim", "ARTICLE": "Artikull"}


def _safe_url(value: str | None) -> str | None:
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _stamp(value: datetime) -> str:
    return _utc(value).astimezone(DIGEST_ZONE).strftime("%d.%m.%Y %H:%M")


def activity_delivery_key(item: NewsItem) -> str:
    # Distinct sources/reposts can refer to the same original URL.
    return hashlib.sha256(f"activity:{item.id}".encode("utf-8")).hexdigest()


def digest_content(entries: list[dict], slot: datetime, source_names: list[str] | None = None,
                   *, interval_start: datetime | None = None, sources: list[NewsSource] | None = None) -> tuple[str, str, str]:
    start = interval_start or slot
    interval = f"{_stamp(start)} · {_stamp(slot)}"
    count = len(entries)
    title = "Raporti i aktivitetit"
    subject = f"[News Intelligence] {count} {'aktivitet i ri' if count == 1 else 'aktivitete të reja'} · {slot.astimezone(DIGEST_ZONE):%H:%M}"
    counts = {kind: sum(activity_kind(e["item"], e["source"]) == kind for e in entries) for kind in ACTIVITY_LABELS}
    other = count - sum(counts.values())
    summary = " · ".join(f"{counts[kind]} {label}" for kind, label in [("NEWS", "lajme"), ("POST", "postime"), ("REPOST", "repostime"), ("ARTICLE", "artikuj")] if counts[kind])
    if other:
        summary += f" · {other} aktivitete të tjera"
    text_lines = [title, f"Intervali: {interval} (Europe/Budapest)", summary, ""]
    sections: dict[str, list[dict]] = {}
    for entry in entries:
        sections.setdefault(str(entry["source"].id), []).append(entry)
    if not entries:
        message = "Nuk u gjet aktivitet i ri në të dhënat e mbledhura nga burimet e zgjedhura për këtë interval."
        text_lines.extend([message, ""])
    else:
        message = "Lajme, postime, repostime dhe artikuj nga burimet me email të aktivizuar."
    html_sections = []
    for group in sections.values():
        source_name = group[0]["source"].name
        text_lines.extend([f"{source_name} ({len(group)} aktivitete)", "-" * 36])
        cards = []
        for entry in group:
            item, analysis, source = entry["item"], entry["analysis"], entry["source"]
            kind = activity_kind(item, source)
            label = ACTIVITY_LABELS.get(kind, "Aktivitet")
            summary_text = getattr(analysis, "summary", None) or item.original_text or item.title
            summary_text = " ".join(summary_text.split())[:700]
            details = [label]
            if item.published_at:
                details.append(f"Publikuar: {_stamp(item.published_at)}")
                if _utc(item.published_at) <= _utc(start):
                    details.append("Marrë me vonesë nga burimi")
            else:
                details.append("Ora e publikimit nuk është dhënë nga burimi")
            details.append(f"Mbledhur: {_stamp(item.created_at)}")
            deadline = getattr(analysis, "deadline", None)
            if deadline:
                details.append(f"Afati: {deadline:%d.%m.%Y}")
            data = item.linkedin_data or {}
            for key, text in [("reactionCount", "reagime"), ("commentCount", "komente"), ("repostCount", "repostime")]:
                if isinstance(data.get(key), int) and not isinstance(data[key], bool):
                    details.append(f"{data[key]} {text}")
            if kind == "REPOST" and data.get("originalAuthor"):
                details.append("Autori origjinal: " + str(data["originalAuthor"]))
            url = _safe_url(item.url)
            heading = escape(item.title)
            if url:
                heading = f'<a href="{escape(url, quote=True)}" style="color:#245b38;text-decoration:none">{heading}</a>'
            link = f'<a href="{escape(url, quote=True)}" style="display:inline-block;color:#245b38;font-size:13px;font-weight:bold;text-decoration:underline">Hap aktivitetin ↗</a>' if url else ""
            original = _safe_url(data.get("originalPostUrl")) if kind == "REPOST" else None
            if original and original != url:
                link += f' &nbsp; <a href="{escape(original, quote=True)}" style="color:#687b6c;font-size:13px">Postimi origjinal ↗</a>'
            meta = " · ".join(details)
            text_lines.extend([f"[{label}] {item.title}", meta, summary_text, url or "", ""])
            if original and original != url:
                text_lines.extend(["Postimi origjinal: " + original, ""])
            cards.append('<tr><td style="padding:18px 20px;border-top:1px solid #e5ebe6">'
                         f'<p style="margin:0 0 7px;color:#63866a;font-size:11px;font-weight:bold;text-transform:uppercase">{escape(label)}</p>'
                         f'<h3 style="margin:0;font-size:17px;line-height:1.4">{heading}</h3>'
                         f'<p style="color:#687b6c;font-size:12px;line-height:1.6;margin:9px 0">{escape(meta)}</p>'
                         f'<p style="font-size:14px;line-height:1.6;margin:0 0 14px">{escape(summary_text)}</p>{link}</td></tr>')
        html_sections.append(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:22px;border:1px solid #dfe7df;border-radius:10px;background:#fff"><tr><td style="padding:15px 20px;background:#edf3ed;font-weight:bold;font-size:16px">{escape(source_name)} <span style="font-weight:normal;color:#687b6c">· {len(group)} aktivitete</span></td></tr>{"".join(cards)}</table>')
    html = ('<!doctype html><html lang="sq"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="margin:0;background:#f3f6f3;font-family:Arial,sans-serif;color:#24342a">'
            f'<div style="display:none;max-height:0;overflow:hidden">{escape(summary)} · Intervali: {escape(interval)}</div>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 12px">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:700px"><tr><td>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#254d34;color:#fff;border-radius:12px"><tr><td style="padding:24px">'
            '<p style="font-size:11px;letter-spacing:2px;margin:0 0 12px">NEWS INTELLIGENCE</p>'
            f'<h1 style="font-size:26px;margin:0 0 12px">{title}</h1>'
            f'<p style="font-size:13px;line-height:1.6;margin:0">Intervali: {escape(interval)}<br>Europe/Budapest</p></td></tr></table>'
            f'<p style="padding:15px;background:#fff;border:1px solid #dfe7df;border-radius:8px;font-size:14px;font-weight:bold;line-height:1.7">{escape(summary)}</p>'
            f'<p style="font-size:13px;line-height:1.6;color:#687b6c">{escape(message)}</p>' + "".join(html_sections) +
            '<p style="font-size:11px;line-height:1.6;color:#687b6c;margin-top:24px">Aktivitetet raportohen vetëm një herë. Aktivitetet e marra me vonesë përfshihen në raportin pasues dhe shënohen veçmas. Numrat e reagimeve tregojnë gjendjen në kontrollin e fundit.</p>'
            '</td></tr></table></td></tr></table></body></html>')
    return subject, "\n".join(text_lines), html


async def _send_digest(subject: str, body: str, html: str, *, credentials: tuple[str, str] | None = None) -> None:
    mailer = GmailService(credentials=credentials) if credentials is not None else GmailService()
    if mailer.port != 465 and not await _smtp_reachable(mailer.host, mailer.port):
        if not await _smtp_reachable(mailer.host, 465):
            raise ConnectionError("SMTP submission ports are unreachable")
        mailer.port = 465
    await mailer.send_verified(subject, [settings.NEWS_EMAIL_RECIPIENT], body, html=html)


async def send_digest_test(*, credentials: tuple[str, str] | None = None) -> None:
    subject = "[News Intelligence] Test i raportit me email"
    body = "Ky është një email test. Raportet e planifikuara do të përmbajnë lajmet, postimet, repostimet dhe artikujt e rinj nga burimet ku emaili është aktiv."
    html = ('<div style="max-width:600px;margin:auto;padding:28px;font-family:Arial,sans-serif;color:#24342a">'
            '<p style="color:#63866a;font-size:12px;font-weight:700;letter-spacing:2px">NEWS INTELLIGENCE</p>'
            '<h1 style="font-size:24px">Test i raportit me email</h1>'
            '<p>Konfigurimi i emailit po punon. Raportet e planifikuara do të përmbajnë lajmet, postimet, repostimet dhe artikujt e rinj nga burimet ku emaili është aktiv.</p></div>')
    await _send_digest(subject, body, html, credentials=credentials)


async def process_digest(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    digest = (await db.execute(select(NewsDigestSettings).where(NewsDigestSettings.user_id == user_id)
                               .with_for_update(skip_locked=True))).scalar_one_or_none()
    if digest is None:
        return 0
    slot = latest_slot(now, digest.times)
    if _utc(digest.last_slot_at) >= slot:
        return 0
    sources = (await db.scalars(
        select(NewsSource)
        .join(NewsSourceEmailSubscription, NewsSourceEmailSubscription.source_id == NewsSource.id)
        .where(NewsSourceEmailSubscription.user_id == user_id, NewsSource.status == "ACTIVE")
        .order_by(NewsSource.name)
    )).all()
    if not sources:
        digest.last_slot_at = slot
        await db.commit()
        return 0
    activity_time = func.coalesce(NewsItem.published_at, NewsItem.created_at)
    source_rows = (await db.execute(
        select(NewsItem, NewsAnalysis, NewsSource)
        .outerjoin(NewsAnalysis, NewsAnalysis.news_item_id == NewsItem.id)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
        .join(NewsSourceEmailSubscription, NewsSourceEmailSubscription.source_id == NewsSource.id)
        .where(NewsSourceEmailSubscription.user_id == user_id, NewsSource.status == "ACTIVE",
               NewsItem.created_at >= NewsSourceEmailSubscription.enabled_at,
               NewsItem.created_at <= slot, activity_time <= slot,
               or_(activity_time > digest.last_slot_at,
                   and_(NewsItem.created_at > digest.last_slot_at,
                        activity_time >= NewsSourceEmailSubscription.enabled_at)))
        .order_by(NewsSource.name, activity_time.desc(), NewsItem.id)
    )).all()
    candidates = {activity_delivery_key(item): {"item": item, "analysis": analysis, "source": source}
                  for item, analysis, source in source_rows}
    legacy = {key: hashlib.sha256(entry["item"].url.encode("utf-8")).hexdigest()
              for key, entry in candidates.items()}
    keys = set(candidates) | set(legacy.values())
    delivered = {delivery.url_hash: delivery.sent_at for delivery in (await db.scalars(
        select(NewsDigestDelivery).where(NewsDigestDelivery.user_id == user_id,
                                       NewsDigestDelivery.url_hash.in_(keys))
    )).all()} if keys else {}
    entries = []
    for key, entry in candidates.items():
        if key in delivered:
            continue
        previous = delivered.get(legacy[key])
        # Keep old URL-based receipts without hiding a new repost of that URL.
        if previous and activity_kind(entry["item"], entry["source"]) != "REPOST" and _utc(previous) >= _utc(entry["item"].created_at):
            continue
        entries.append(entry)
    if not entries:
        digest.last_slot_at = slot
        digest.last_error = None
        await db.commit()
        return 0
    subject, body, html = digest_content(entries, slot, interval_start=digest.last_slot_at, sources=sources)
    try:
        await _send_digest(subject, body, html)
    except Exception as exc:
        digest.last_error = str(exc)[:500]
        await db.commit()
        raise
    for entry in entries:
        db.add(NewsDigestDelivery(user_id=user_id, url_hash=activity_delivery_key(entry["item"]), sent_at=now))
    digest.last_sent_at = now
    digest.last_slot_at = slot
    digest.last_error = None
    await db.commit()
    return len(entries)


async def run_digest_cycle() -> None:
    if settings.NEWS_TRIAL_EMAIL_USER or settings.NEWS_TRIAL_EMAIL_PASSWORD:
        return
    if not settings.EMAIL_USER or not settings.EMAIL_PASSWORD:
        return
    async with SessionLocal() as db:
        user_ids = (await db.scalars(select(NewsDigestSettings.user_id))).all()
    for user_id in user_ids:
        try:
            async with SessionLocal() as db:
                await process_digest(db, user_id)
        except Exception:
            logger.exception("Could not send digest for %s", user_id)
