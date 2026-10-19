from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from app.intelligence.linkedin_identity import canonical_linkedin_url

SourceType = Literal["WEBSITE", "RSS", "LINKEDIN", "FACEBOOK", "API", "OTHER"]
SourceStatus = Literal["ACTIVE", "PAUSED"]
SourcePriority = Literal["HIGH", "NORMAL", "LOW"]
NewsCategory = Literal["NEWS", "GRANT", "TENDER", "EVENT", "BUSINESS", "TECHNOLOGY", "REGULATION", "PARTNERSHIP"]
SourceCategory = Literal["Grants", "Tenders", "Funding", "Business", "Events", "Technology", "Regulations", "Partnerships", "General News"]
RepostGroup = Literal["CEO", "COMPANY"]


class NewsSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2000)
    type: SourceType
    status: SourceStatus = "ACTIVE"
    priority: SourcePriority = "NORMAL"
    categories: list[SourceCategory] = Field(default_factory=list, max_length=9)
    ai_instructions: str | None = Field(default=None, max_length=4000)
    fetch_interval_minutes: int = Field(default=60, ge=5, le=10080)
    email_enabled: bool = False
    repost_groups: list[RepostGroup] = Field(default_factory=list, max_length=2)
    repost_guidance: dict[RepostGroup, str] = Field(default_factory=dict)

    @field_validator("name", "url")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be empty")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Enter a valid http or https URL without credentials")
        return value

    @model_validator(mode="after")
    def valid_linkedin_profile(self) -> "NewsSourceCreate":
        self.repost_groups = list(dict.fromkeys(self.repost_groups))
        if self.repost_groups and self.type != "LINKEDIN":
            raise ValueError("Repost watch groups require a LinkedIn source.")
        if self.type == "LINKEDIN":
            self.fetch_interval_minutes = max(1440, self.fetch_interval_minutes)
            self.url = canonical_linkedin_url(self.url)
        if self.type == 'FACEBOOK':
            from app.intelligence.facebook_identity import identity_or_none,HOSTS
            parsed=urlparse(self.url)
            if parsed.hostname not in HOSTS:raise ValueError('Use a Facebook source URL.')
            self.url=identity_or_none(self.url) or self.url
            self.fetch_interval_minutes=max(1440,self.fetch_interval_minutes)
        return self


class NewsSourceUpdate(NewsSourceCreate):
    pass


class NewsSourceEmailInput(BaseModel):
    enabled: bool


class NewsSourceOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    type: SourceType
    status: SourceStatus
    priority: SourcePriority
    categories: list[str]
    ai_instructions: str | None
    fetch_interval_minutes: int
    last_checked_at: datetime | None
    last_started_at: datetime | None
    pending_snapshot_id: str | None
    last_error: str | None
    collection_supported: bool = False
    collection_enabled: bool = False
    collection_note: str | None = None
    email_enabled: bool = False
    created_at: datetime
    updated_at: datetime
    repost_groups: list[RepostGroup] = Field(default_factory=list)
    repost_guidance: dict[RepostGroup, str] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class LinkedInMonitorOut(BaseModel):
    """Read-only feed metadata; no editor instructions, subscriptions or billing."""
    id: uuid.UUID
    name: str
    url: str
    type: Literal["LINKEDIN"]
    status: SourceStatus
    fetch_interval_minutes: int
    last_checked_at: datetime | None
    pending_snapshot_id: str | None
    last_error: str | None
    collection_enabled: bool


class StructuredNewsAnalysis(BaseModel):
    """Contract for future server-side AI analysis output."""

    summary: str
    category: NewsCategory
    importanceScore: int = Field(ge=0, le=100)
    relevanceScore: int = Field(ge=0, le=100)
    whyItMatters: str | None = None
    deadline: date | None = None
    fundingAmount: str | None = None
    eligibility: str | None = None
    opportunityType: str | None = None
    tags: list[str] = Field(default_factory=list)


class IntelligenceStatusOut(BaseModel):
    linkedinConfigured: bool


class FeedAnalysisOut(BaseModel):
    summary: str
    category: NewsCategory
    importanceScore: int
    relevanceScore: int
    whyItMatters: str | None
    deadline: str | None
    fundingAmount: str | None
    eligibility: str | None
    opportunityType: str | None
    tags: list[str]


class LinkedInCommentOut(BaseModel):
    author: str | None = None
    text: str
    url: str | None = None
    publishedAt: str | None = None


class LinkedInActivityOut(BaseModel):
    kind: Literal["POST", "REPOST", "ARTICLE"] = "POST"
    authorName: str | None = None
    authorUrl: str | None = None
    reactionCount: int | None = Field(default=None, ge=0)
    commentCount: int | None = Field(default=None, ge=0)
    repostCount: int | None = Field(default=None, ge=0)
    comments: list[LinkedInCommentOut] = Field(default_factory=list)
    originalPostText: str | None = None
    originalPostUrl: str | None = None
    originalAuthor: str | None = None
    checkedAt: str | None = None


class FeedItemOut(BaseModel):
    id: str
    sourceId: str
    sourceName: str
    sourceType: SourceType
    externalId: str | None
    url: str
    title: str
    originalText: str | None
    publishedAt: str
    imageUrl: str | None
    contentHash: str | None
    createdAt: str
    readAt: str | None
    emailedAt: str | None
    savedAt: str | None
    linkedin: LinkedInActivityOut | None = None
    repostGroups: list[RepostGroup] = Field(default_factory=list)
    repostGuidance: dict[RepostGroup, str] = Field(default_factory=dict)
    location: str | None
    priority: Literal["HIGH", "NORMAL"]
    focusScore: int
    analysis: FeedAnalysisOut


class NewsFeedOut(BaseModel):
    items: list[FeedItemOut]
    hasLiveSources: bool
    emailConfigured: bool
    emailRecipient: str
    aiConfigured: bool


class DeepInsightOut(BaseModel):
    itemId: str
    generatedAt: datetime
    keyPoints: list[str]
    businessImpact: str
    recommendedActions: list[str]
    openQuestions: list[str]
    confidence: Literal["high", "medium", "low"]
    evidenceLimitations: str | None


class BriefSignalOut(BaseModel):
    itemId: str
    title: str
    url: str
    insight: str
    nextStep: str


class StrategicBriefOut(BaseModel):
    generatedAt: datetime
    headline: str
    overview: str
    signals: list[BriefSignalOut]
    watchouts: list[str]


class EmailShareOut(BaseModel):
    recipient: str
    sentAt: datetime
    alreadySent: bool


class SourceCheckOut(BaseModel):
    state: Literal["started", "pending", "completed", "error", "unavailable"]


class DigestSettingsInput(BaseModel):
    times: list[str] = Field(min_length=1, max_length=8)

    @field_validator("times")
    @classmethod
    def valid_times(cls, values: list[str]) -> list[str]:
        import re
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in values):
            raise ValueError("Use HH:MM times in 24-hour format")
        if len(set(values)) != len(values):
            raise ValueError("Times must be unique")
        return sorted(values)
