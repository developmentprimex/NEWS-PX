import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.intelligence import digest_service as service
from app.db import AppMetadata
from app.intelligence.content_quality import EXCLUSION_PREFIX


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    async def scalars(self, query):
        return self.session.scalars(query)

    def add(self, value):
        self.session.add(value)

    async def commit(self):
        self.session.commit()


def test_actual_query_interval_subscriptions_and_delivery_receipts(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for model in (AppMetadata,service.NewsSource, service.NewsItem, service.NewsAnalysis,
                  service.NewsDigestSettings, service.NewsSourceEmailSubscription, service.NewsDigestDelivery):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
                column.server_default = None
    metadata.create_all(engine)
    start = datetime(2026, 10, 6, 10, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    enabled = start - timedelta(days=1)
    user = uuid4()
    sent = []

    async def send(*args):
        sent.append(args)

    monkeypatch.setattr(service, "_send_digest", send)
    with Session(engine, expire_on_commit=False) as db:
        digest = service.NewsDigestSettings(user_id=user, times=["12:30", "17:00", "21:00"], last_slot_at=start)
        db.add(digest)
        selected = service.NewsSource(id=uuid4(), name="Selected", url="https://example.com", type="LINKEDIN", status="ACTIVE")
        unselected = service.NewsSource(id=uuid4(), name="Unselected", url="https://example.com", type="RSS", status="ACTIVE")
        paused = service.NewsSource(id=uuid4(), name="Paused", url="https://example.com", type="RSS", status="PAUSED")
        another_user = service.NewsSource(id=uuid4(), name="Other user", url="https://example.com", type="RSS", status="ACTIVE")
        db.add_all([selected, unselected, paused, another_user])
        db.flush()
        for source, subscriber in [(selected, user), (paused, user), (another_user, uuid4())]:
            db.add(service.NewsSourceEmailSubscription(source_id=source.id, user_id=subscriber, enabled_at=enabled))

        def item(title, published, created=end - timedelta(minutes=10), source=selected, kind="POST"):
            value = service.NewsItem(id=uuid4(), source_id=source.id, title=title, url="https://example.com/shared",
                                     published_at=published, created_at=created, original_text="Fallback content",
                                     linkedin_data={"kind": kind})
            db.add(value)
            return value

        item("Current post", start + timedelta(minutes=1))
        excluded=item('Quarantined policy page',end)
        db.add(AppMetadata(key=EXCLUSION_PREFIX+excluded.id.hex,value='Static page'))
        item("Current repost", end, kind="REPOST")
        item("Article without analysis", end, kind="ARTICLE")
        item("Unknown publication", None)
        item("Late arrival", start - timedelta(hours=1))
        item("Boundary already collected", start, created=start)
        item("Historical backfill", enabled - timedelta(minutes=1))
        item("Future publication", end + timedelta(seconds=1))
        item("Collected after slot", end, created=end + timedelta(seconds=1))
        item("Before subscription", start, created=enabled - timedelta(minutes=1))
        item("Unselected item", end, source=unselected)
        item("Paused item", end, source=paused)
        item("Other user item", end, source=another_user)
        old = item("Already emailed legacy post", end)
        item("New repost of legacy URL", end, kind="REPOST")
        # A legacy URL receipt suppresses old posts, but does not suppress a new repost.
        db.add(service.NewsDigestDelivery(user_id=user,
            url_hash=hashlib.sha256(old.url.encode()).hexdigest(), sent_at=start))
        # Give the legacy item an earlier collection time while retaining current publication.
        old.created_at = start
        db.commit()
        adapter = AsyncSessionAdapter(db)
        assert asyncio.run(service.process_digest(adapter, user, end + timedelta(minutes=1))) == 6
        body = sent[0][1]
        for title in ["Current post", "Current repost", "Article without analysis", "Unknown publication", "Late arrival", "New repost of legacy URL"]:
            assert title in body
        for title in ["Boundary already collected", "Historical backfill", "Future publication", "Collected after slot",
                      "Quarantined policy page",
                      "Before subscription", "Unselected item", "Paused item", "Other user item", "Already emailed legacy post"]:
            assert title not in body
        assert "Fallback content" in body
        assert "vones" in body
        # Only the item collected after the earlier slot is newly eligible next time.
        assert asyncio.run(service.process_digest(adapter, user, end + timedelta(hours=4, minutes=1))) == 2
        assert "Collected after slot" in sent[1][1]
        assert "Future publication" in sent[1][1]
        assert "Current post" not in sent[1][1]


def test_structured_email_escapes_content_and_rejects_unsafe_links():
    now = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
    source = SimpleNamespace(id=uuid4(), name="GA & team", type="LINKEDIN", last_checked_at=now,
                             last_error="Provider failed", pending_snapshot_id=None)
    item = SimpleNamespace(id=uuid4(), title="<script>unsafe</script>", original_text="<b>Original</b>",
                           url="javascript:alert(1)", published_at=now, created_at=now,
                           linkedin_data={"kind": "REPOST", "originalPostUrl": "https://example.com/original", "reactionCount": 2})
    subject, body, html = service.digest_content([{"item": item, "analysis": None, "source": source}], now,
                                              interval_start=now - timedelta(hours=4), sources=[source])
    assert "1 aktivitet i ri" in subject
    assert "Intervali:" in body and "REPOST" not in body and "Repostim" in body
    assert "Provider failed" not in html
    assert "Kontrolli" not in html
    assert "GA &amp; team" in html and "&lt;script&gt;" in html
    assert "javascript:" not in html and '<script>' not in html
    assert 'href="https://example.com/original"' in html
    assert "2 reagime" in body
