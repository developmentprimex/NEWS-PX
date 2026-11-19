"""Send a selected update to the fixed internal Intelligence mailbox."""

import asyncio
import socket

from app.intelligence.models import NewsAnalysis, NewsItem, NewsSource
from app.intelligence.priority import analyzed_news_priority
from app.mail import GmailService
from app.config import settings

INTELLIGENCE_RECIPIENT = settings.NEWS_EMAIL_RECIPIENT


def _clean(value: str) -> str:
    return " ".join(value.split())


def news_email_content(item: NewsItem, source: NewsSource, analysis: NewsAnalysis) -> tuple[str, str]:
    title = _clean(item.title)
    subject = f"[News Intelligence] {title}"[:200]
    priority = analyzed_news_priority(item, source, analysis)
    lines = [title, "", f"{analysis.category.title()} · {priority.title()} priority", f"Source: {_clean(source.name)}"]
    if item.published_at:
        lines.append(f"Published: {item.published_at.date().isoformat()}")
    lines.extend(["", _clean(analysis.summary)])
    if analysis.why_it_matters:
        lines.extend(["", "Why this matters", _clean(analysis.why_it_matters)])
    for label, value in (
        ("Deadline", analysis.deadline.isoformat() if analysis.deadline else None),
        ("Funding", analysis.funding_amount),
        ("Eligibility", analysis.eligibility),
    ):
        if value:
            lines.append(f"{label}: {_clean(value)}")
    lines.extend(["", f"Read original: {item.url}"])
    return subject, "\n".join(lines)


async def _smtp_reachable(host: str, port: int) -> bool:
    def probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            return False

    return await asyncio.to_thread(probe)


async def send_news_email(item: NewsItem, source: NewsSource, analysis: NewsAnalysis, *, recipient: str | None = None) -> None:
    subject, body = news_email_content(item, source, analysis)
    gmail = GmailService()
    if gmail.port != 465 and not await _smtp_reachable(gmail.host, gmail.port):
        # Choose the available transport before sending so a network failure
        # after SMTP accepts the message cannot trigger a duplicate email.
        if not await _smtp_reachable(gmail.host, 465):
            raise ConnectionError("Gmail SMTP submission ports are unreachable")
        gmail.port = 465
    await gmail.send_verified(subject, [recipient or INTELLIGENCE_RECIPIENT], body)
