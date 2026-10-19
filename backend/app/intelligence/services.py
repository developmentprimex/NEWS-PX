"""Contracts shared by isolated Intelligence source adapters and analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import re
from typing import Protocol

from app.intelligence.models import NewsSource
from app.intelligence.schemas import StructuredNewsAnalysis


@dataclass(frozen=True)
class CollectedNewsItem:
    external_id: str | None
    url: str
    title: str
    original_text: str | None
    published_at: datetime | None
    image_url: str | None = None
    linkedin_data: dict | None = None
    publication_time_verified: bool = False


def publication_has_precise_time(value: object) -> bool:
    """Require an actual clock time and explicit timezone in the original input."""
    if not isinstance(value, str) or not re.search(r'\d{1,2}:\d{2}', value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


class SourceAdapter(Protocol):
    async def collect(self, source: NewsSource) -> list[CollectedNewsItem]: ...


class NewsAIService(Protocol):
    async def analyze_news_item(self, item: CollectedNewsItem, source: NewsSource) -> StructuredNewsAnalysis: ...
    async def generate_daily_brief(self, analyses: list[StructuredNewsAnalysis]) -> str: ...
