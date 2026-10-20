import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.intelligence import digest_service
from app.intelligence.schemas import DigestSettingsInput


def row(source_name, url, created_at):
    item = SimpleNamespace(id=uuid4(), url=url, title=f"Notice {url.rsplit('/', 1)[-1]}", created_at=created_at,
                           published_at=created_at, original_text="Original body", linkedin_data=None)
    analysis = SimpleNamespace(summary="A useful opportunity.", deadline=None)
    source = source_fixture(source_name)
    return item, analysis, source


def source_fixture(name):
    return SimpleNamespace(id=uuid4(), name=name, type="RSS", last_checked_at=None,
                           last_error=None, pending_snapshot_id=None)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def all(self):
        return self.value


class FakeSession:
    def __init__(self, digest, rows, sources=None):
        self.digest = digest
        self.rows = rows
        self.deliveries = []
        self.sources = sources if sources is not None else [row[2] for row in rows]

    async def execute(self, query):
        entity = query.column_descriptions[0]["entity"]
        if entity is digest_service.NewsDigestSettings:
            return FakeResult(self.digest)
        return FakeResult(self.rows)

    async def scalars(self, query):
        if query.column_descriptions[0]["entity"] is digest_service.NewsSource:
            return FakeResult(self.sources)
        return FakeResult(self.deliveries)

    def add(self, delivery):
        self.deliveries.append(delivery)

    async def commit(self):
        pass


def test_digest_groups_sources_and_sends_only_new_items(monkeypatch):
    sent = []

    async def fake_send(subject, body, html):
        sent.append((subject, body, html))

    monkeypatch.setattr(digest_service, "_send_digest", fake_send)
    digest = SimpleNamespace(times=["12:30", "17:00", "21:00"],
                             last_slot_at=datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc),
                             last_sent_at=None, last_error=None)
    first = row("Grants", "https://example.org/one", datetime(2026, 9, 30, 11, tzinfo=timezone.utc))
    duplicate = row("Business", "https://example.org/one", datetime(2026, 9, 30, 11, tzinfo=timezone.utc))
    second = row("Business", "https://example.org/two", datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
    db = FakeSession(digest, [first, duplicate, second])
    user_id = uuid4()

    async def run():
        count = await digest_service.process_digest(db, user_id, datetime(2026, 9, 30, 15, 1, tzinfo=timezone.utc))
        assert count == 3
        assert len(sent) == 1
        assert "Grants" in sent[0][1] and "Business" in sent[0][1]
        assert sent[0][1].count("https://example.org/one") == 2
        assert len(db.deliveries) == 3

        third = row("Grants", "https://example.org/three", datetime(2026, 9, 30, 17, tzinfo=timezone.utc))
        db.rows.extend([third])
        count = await digest_service.process_digest(db, user_id, datetime(2026, 9, 30, 19, 1, tzinfo=timezone.utc))
        assert count == 1
        assert len(sent) == 2
        assert "https://example.org/three" in sent[1][1]
        assert "https://example.org/one" not in sent[1][1]
        count = await digest_service.process_digest(db, user_id, datetime(2026, 10, 1, 10, 31, tzinfo=timezone.utc))
        assert count == 0
        assert len(sent) == 2
        assert len(db.deliveries) == 4

    asyncio.run(run())


def test_digest_times_are_unique_and_sorted():
    assert DigestSettingsInput(times=["21:00", "12:30", "17:00"]).times == ["12:30", "17:00", "21:00"]
    with pytest.raises(ValueError):
        DigestSettingsInput(times=["12:30", "12:30"])


def test_empty_report_advances_slot_without_sending(monkeypatch):
    sent = []

    async def fake_send(subject, body, html):
        sent.append((subject, body, html))

    monkeypatch.setattr(digest_service, "_send_digest", fake_send)
    digest = SimpleNamespace(times=["17:00", "21:00"], last_slot_at=datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc), last_sent_at=None, last_error=None)
    db = FakeSession(digest, [], sources=[source_fixture("KIESA & Lajme"), source_fixture("EU Digital")])
    user_id = uuid4()

    async def run():
        now = datetime(2026, 9, 30, 15, 1, tzinfo=timezone.utc)
        assert await digest_service.process_digest(db, user_id, now) == 0
        assert sent == []
        assert digest.last_slot_at == datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
        assert digest.last_sent_at is None
        assert not db.deliveries
        await digest_service.process_digest(db, user_id, now)
        await digest_service.process_digest(db, user_id, datetime(2026, 9, 30, 19, 1, tzinfo=timezone.utc))
        assert sent == []
        assert digest.last_slot_at == datetime(2026, 9, 30, 19, tzinfo=timezone.utc)

    asyncio.run(run())


def test_no_report_when_no_active_email_sources(monkeypatch):
    async def unexpected_send(*args):
        pytest.fail("No email should be sent without subscribed sources")

    monkeypatch.setattr(digest_service, "_send_digest", unexpected_send)
    digest = SimpleNamespace(times=["17:00"], last_slot_at=datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc), last_sent_at=None, last_error=None)
    db = FakeSession(digest, [])
    asyncio.run(digest_service.process_digest(db, uuid4(), datetime(2026, 9, 30, 15, 1, tzinfo=timezone.utc)))
    assert digest.last_sent_at is None


def test_failed_report_with_updates_retries_without_advancing_slot(monkeypatch):
    calls = []

    async def fake_send(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ConnectionError("SMTP unavailable")

    monkeypatch.setattr(digest_service, "_send_digest", fake_send)
    previous = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
    digest = SimpleNamespace(times=["17:00"], last_slot_at=previous, last_sent_at=None, last_error=None)
    db = FakeSession(digest, [row("KIESA", "https://example.org/new", datetime(2026, 9, 30, 12, tzinfo=timezone.utc))])
    user_id = uuid4()
    now = datetime(2026, 9, 30, 15, 1, tzinfo=timezone.utc)

    async def run():
        with pytest.raises(ConnectionError):
            await digest_service.process_digest(db, user_id, now)
        assert digest.last_slot_at == previous
        assert digest.last_sent_at is None
        assert digest.last_error == "SMTP unavailable"
        await digest_service.process_digest(db, user_id, now)
        assert digest.last_sent_at == now
        assert digest.last_error is None

    asyncio.run(run())


def test_report_omits_sources_without_new_updates_and_zero_categories():
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    item, analysis, active = row("Active source", "https://example.org/new", now)
    idle = source_fixture("Idle source")
    failed = source_fixture("Failed source")
    failed.last_error = "Provider failed"
    _, body, html = digest_service.digest_content(
        [{"item": item, "analysis": analysis, "source": active}], now,
        sources=[idle, failed, active], source_names=[idle.name, failed.name, active.name])
    for content in (body, html):
        assert active.name in content
        assert "1 lajme" in content
        assert idle.name not in content and failed.name not in content
        assert "0 postime" not in content
        assert "0 repostime" not in content
        assert "0 artikuj" not in content
        assert "Burimet e zgjedhura" not in content
        assert "Kontroll" not in content


@pytest.mark.parametrize("kind", ["NEWS", "POST", "REPOST", "ARTICLE"])
def test_report_summary_includes_only_nonempty_activity_types(kind):
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    item, analysis, source = row("Source", "https://example.org/new", now)
    if kind != "NEWS":
        source.type = "LINKEDIN"
        item.linkedin_data = {"kind": kind}
    _, body, html = digest_service.digest_content(
        [{"item": item, "analysis": analysis, "source": source}], now)
    for content in (body, html):
        for zero in ("0 lajme", "0 postime", "0 repostime", "0 artikuj"):
            assert zero not in content
