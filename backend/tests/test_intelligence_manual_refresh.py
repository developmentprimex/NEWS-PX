import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.intelligence import router as news_router


def test_manual_refresh_checks_active_sources_and_reports_progress(monkeypatch):
    sources = [
        SimpleNamespace(id=uuid4(), type="RSS", status="ACTIVE", url="https://example.org/feed"),
        SimpleNamespace(id=uuid4(), type="WEBSITE", status="ACTIVE", url="https://example.org/news"),
        SimpleNamespace(id=uuid4(), type="LINKEDIN", status="ACTIVE", url="https://www.linkedin.com/company/example"),
        SimpleNamespace(id=uuid4(), type="RSS", status="PAUSED", url="https://example.org/paused"),
    ]

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return sources[:3]

    class FakeSession:
        async def execute(self, _statement):
            return FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    checked = []

    async def fake_check_source(_session, source_id, *, force):
        assert force
        checked.append(source_id)
        return {sources[0].id: "completed", sources[1].id: "pending", sources[2].id: "started"}[source_id]

    monkeypatch.setattr(news_router, "SessionLocal", FakeSession)
    monkeypatch.setattr(news_router, "check_source", fake_check_source)
    monkeypatch.setattr(news_router, "linkedin_configured", lambda: True)
    monkeypatch.setattr(news_router, "_check_all_task", None)

    async def run():
        status = await news_router.check_all_sources_now(db=FakeSession(), _=None)
        assert status == {"state": "running", "total": 2, "processed": 0, "failed": 0, "started": 0, "pending": 0}
        await news_router._check_all_task
        assert checked == [source.id for source in sources[:2]]
        assert (await news_router.check_all_sources_status(_=None)) == {
            "state": "finished", "total": 2, "processed": 2, "failed": 0, "started": 0, "pending": 1,
        }

    asyncio.run(run())
