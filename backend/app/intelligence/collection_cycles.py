"""Durable trial identities. Coverage and reporting cutoffs are separate facts."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TypedDict
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Date, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


@dataclass(frozen=True)
class TemporalWindow:
    start: datetime
    end: datetime
    verified_start: bool


class CycleStatus(TypedDict):
    state: str
    total: int
    processed: int
    failed: int
    started: int
    pending: int
    partial: int
    blocked: int
    cycle_id: str | None
    phase: str | None
    cutoff_at: str | None


class CollectionCycle(Base):
    __tablename__ = 'intelligence_collection_cycles'
    __table_args__ = (UniqueConstraint('trial_date', 'phase'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    trial_date: Mapped[date] = mapped_column(Date, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default='queued', nullable=False)
    inventory_frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionCycleSource(Base):
    __tablename__ = 'intelligence_collection_cycle_sources'
    __table_args__ = (UniqueConstraint('cycle_id', 'source_id'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_start: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    execution_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(24), default='queued', nullable=False)
    coverage_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    coverage_warning: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionCycleReport(Base):
    __tablename__ = 'intelligence_collection_cycle_reports'
    __table_args__ = (UniqueConstraint('trial_date', 'recipient_fingerprint'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    trial_date: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    recipient_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default='planned', nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))


class TrialUsageObservation(Base):
    __tablename__ = 'intelligence_trial_usage_observations'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    trial_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ScheduledDigestReport(Base):
    __tablename__ = 'intelligence_scheduled_digest_reports'
    __table_args__ = (UniqueConstraint('cycle_id', 'user_id', 'recipient_fingerprint'),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    recipient_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default='planned', nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))


def provider_dates(window: TemporalWindow) -> tuple[date, date]:
    return (window.start.astimezone(timezone.utc).date() - timedelta(days=1),
            window.end.astimezone(timezone.utc).date() + timedelta(days=1))


def confirmed_activity_time(item) -> datetime | None:
    data = item.linkedin_data or {}
    if data.get('kind') == 'REPOST':
        # Legacy date_posted describes the original post in some share records.
        if data.get('activityTimeEvidence') != 'verified-repost-time':
            return None
        try:
            value = datetime.fromisoformat(data['activityAt'].replace('Z', '+00:00'))
            return value.astimezone(timezone.utc) if value.tzinfo else None
        except (KeyError, ValueError, TypeError):
            return None
    if not getattr(item, 'publication_time_verified', False):
        return None
    value = item.published_at
    return value.astimezone(timezone.utc) if value and value.tzinfo else None


async def migrate_trial_schema(connection):
    from sqlalchemy import text
    await connection.execute(text('ALTER TABLE intelligence_items ADD COLUMN IF NOT EXISTS publication_time_verified BOOLEAN NOT NULL DEFAULT false'))
    await connection.execute(text('ALTER TABLE intelligence_brightdata_account ADD COLUMN IF NOT EXISTS billing_scope JSONB'))
    await connection.execute(text('ALTER TABLE intelligence_brightdata_executions ADD COLUMN IF NOT EXISTS billing_account_ref VARCHAR(100)'))
