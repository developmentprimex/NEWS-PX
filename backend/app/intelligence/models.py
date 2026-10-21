from __future__ import annotations

import uuid
from datetime import datetime, date

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NewsSource(Base):
    __tablename__ = "intelligence_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="ACTIVE")
    priority: Mapped[str] = mapped_column(String(12), nullable=False, server_default="NORMAL")
    categories: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    repost_groups: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    repost_guidance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    ai_instructions: Mapped[str | None] = mapped_column(Text)
    fetch_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_snapshot_id: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_successful_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    empty_fetch_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_new_posts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    linkedin_paused_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LinkedInSourceIdentity(Base):
    __tablename__ = "intelligence_linkedin_source_identities"

    canonical_url: Mapped[str] = mapped_column(String(2000), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_sources.id", ondelete="CASCADE"), nullable=False, unique=True)


class NewsItem(Base):
    __tablename__ = "intelligence_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_intelligence_items_source_external"),
        UniqueConstraint("source_id", "content_hash", name="uq_intelligence_items_source_hash"),
        Index("ix_intelligence_items_published_at", "published_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_time_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text('false'))
    image_url: Mapped[str | None] = mapped_column(String(2000))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    linkedin_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BrightDataAccount(Base):
    """One shared gate; billing evidence is never inferred from returned posts."""
    __tablename__ = "intelligence_brightdata_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_month: Mapped[str | None] = mapped_column(String(7))
    provider_billable_records: Mapped[int | None] = mapped_column(Integer)
    provider_balance: Mapped[float | None] = mapped_column(Numeric(16, 4))
    provider_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_monthly_allowance: Mapped[int | None] = mapped_column(Integer)
    usage_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str | None] = mapped_column(Text)
    paused_reason: Mapped[str | None] = mapped_column(String(500))
    alert_month: Mapped[str | None] = mapped_column(String(7))
    alert_levels: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    billing_scope: Mapped[dict | None] = mapped_column(JSONB)


class BrightDataExecution(Base):
    __tablename__ = "intelligence_brightdata_executions"
    __table_args__ = (Index("ix_brightdata_execution_month", "budget_month"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_month: Mapped[str] = mapped_column(String(7), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(250), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(80), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    record_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    returned_records: Mapped[int | None] = mapped_column(Integer)
    newly_saved_posts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_posts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ignored_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_billable_usage: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    api_errors: Mapped[str | None] = mapped_column(Text)
    coverage_warning: Mapped[str | None] = mapped_column(String(500))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billing_account_ref: Mapped[str | None] = mapped_column(String(100))


class BrightDataCanaryRun(Base):
    """A consumed approval is permanent, including crashes before the trigger."""
    __tablename__ = "intelligence_brightdata_canary_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_ref: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class NewsAnalysis(Base):
    __tablename__ = "intelligence_analyses"

    news_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_items.id", ondelete="CASCADE"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    importance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date)
    funding_amount: Mapped[str | None] = mapped_column(String(160))
    eligibility: Mapped[str | None] = mapped_column(Text)
    opportunity_type: Mapped[str | None] = mapped_column(String(40))
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NewsUserState(Base):
    __tablename__ = "intelligence_user_states"
    __table_args__ = (Index("ix_intelligence_user_states_saved", "user_id", "saved_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    news_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_items.id", ondelete="CASCADE"), primary_key=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsDigestSettings(Base):
    __tablename__ = "intelligence_digest_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    times: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["12:30", "17:00", "21:00"])
    last_slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))


class NewsSourceEmailSubscription(Base):
    __tablename__ = "intelligence_source_email_subscriptions"

    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_sources.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NewsDigestDelivery(Base):
    __tablename__ = "intelligence_digest_deliveries"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
