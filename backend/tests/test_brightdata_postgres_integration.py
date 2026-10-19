"""Real private PostgreSQL transactions + mocked Bright Data HTTP responses."""
import asyncio
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import Base
from app.intelligence import collection
from app.intelligence.credit_control import budget_status
from app.intelligence.credit_migration import migrate_credit_controls
from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsItem, NewsSource, NewsUserState


@pytest.fixture(scope="module")
def private_postgres():
    # CI owns a disposable postgres service on the same fixed loopback port.
    if os.environ.get("NEWS_TEST_USE_POSTGRES_SERVICE") == "1":
        yield
        return
    binary = Path(os.environ.get("NEWS_TEST_POSTGRES_BIN", "C:/Program Files/PostgreSQL/16/bin"))
    if not (binary / "initdb.exe").exists():
        pytest.skip("Set NEWS_TEST_POSTGRES_BIN to local PostgreSQL binaries")
    root = Path(__file__).resolve().parents[2] / "tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="credit-pg-", dir=root) as directory:
        data, log = Path(directory) / "data", Path(directory) / "postgres.log"
        def command(*args):
            # On Windows pg_ctl's server can inherit PIPE handles and prevent
            # communicate() from returning even after pg_ctl has exited.
            with tempfile.TemporaryFile(mode="w+") as output:
                result = subprocess.run(args, stdout=output, stderr=output, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=45)
                output.seek(0)
                assert result.returncode == 0, output.read()
        command(str(binary / "initdb.exe"), "-D", str(data), "-U", "credit_test", "-A", "trust", "--no-locale", "-E", "UTF8")
        started = False
        try:
            command(str(binary / "pg_ctl.exe"), "-D", str(data), "-l", str(log), "-o", "-h 127.0.0.1 -p 55439 -F", "-w", "start")
            started = True
            yield
        finally:
            if started:
                command(str(binary / "pg_ctl.exe"), "-D", str(data), "-m", "fast", "-w", "stop")


@pytest.fixture
def database(private_postgres, monkeypatch):
    assert settings.NEWS_DATABASE_URL == "postgresql+asyncpg://credit_test@127.0.0.1:55439/postgres"
    engine = create_async_engine(settings.NEWS_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            now = datetime.now(timezone.utc)
            db.add(BrightDataAccount(id=1, provider_active=True, provider_balance=0,
                verified_monthly_allowance=5000, provider_month=now.strftime("%Y-%m"),
                provider_billable_records=0, usage_as_of=now, verified_at=now, alert_levels=[]))
            await db.commit()
    asyncio.run(setup())
    monkeypatch.setattr(collection, "engine", engine)
    monkeypatch.setattr(collection, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_FETCH_ENABLED", True)
    monkeypatch.setattr(settings, "NEWS_ENABLE_COLLECTION", True)
    yield engine, sessions
    asyncio.run(engine.dispose())


class Provider:
    def __init__(self, monkeypatch, records=None, status=200, message=None, delay=0):
        self.records, self.status, self.message, self.delay = records or [], status, message, delay
        self.requests = []
        async def respond(request):
            self.requests.append(request)
            if request.url.path.endswith("/trigger"):
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.message == "timeout":
                    raise httpx.ReadTimeout("Trigger timed out", request=request)
                return httpx.Response(self.status, json={"snapshot_id": "sd_test123"} if self.status == 200 else {"error": self.message or "API error"})
            if "/progress/" in request.url.path:
                return httpx.Response(200, json={"status": "ready"})
            return httpx.Response(200, json=self.records)
        monkeypatch.setattr(BrightDataLinkedInAdapter, "_client", lambda _self: httpx.AsyncClient(
            base_url="https://api.brightdata.com/datasets/v3/", transport=httpx.MockTransport(respond)))

    @property
    def triggers(self):
        return [r for r in self.requests if r.method == "POST"]


async def source(sessions, *, url="https://www.linkedin.com/company/example/", **kwargs):
    async with sessions() as db:
        row = NewsSource(name="Example", url=url, type="LINKEDIN",
                         fetch_interval_minutes=1440, categories=[], **kwargs)
        db.add(row)
        await db.commit()
        return row.id


async def check(sessions, source_id, force=False):
    async with sessions() as db:
        return await collection.check_source(db, source_id, force=force)


def records(count=1):
    return [{"id": str(42 + n), "url": f"https://www.linkedin.com/posts/example_activity-{42+n}-abc",
             "post_text": "New update", "date_posted": datetime.now(timezone.utc).isoformat()} for n in range(count)]


def test_repeated_results_deduplicate_and_preserve_read_state(database, monkeypatch):
    _, sessions = database
    alternate = {**records()[0], "url": "https://www.linkedin.com/feed/update/urn:li:activity:42"}
    provider = Provider(monkeypatch, [*records(), alternate])
    async def run():
        sid = await source(sessions)
        assert await check(sessions, sid) == "started"
        assert await check(sessions, sid) == "completed"
        async with sessions() as db:
            item = (await db.scalars(select(NewsItem))).one()
            db.add(NewsUserState(user_id=uuid4(), news_item_id=item.id, saved_at=datetime.now(timezone.utc)))
            row = await db.get(NewsSource, sid)
            row.last_started_at -= timedelta(days=1)
            await db.commit()
        assert await check(sessions, sid) == "started"
        assert await check(sessions, sid) == "completed"
        async with sessions() as db:
            assert len((await db.scalars(select(NewsItem))).all()) == 1
            assert len((await db.scalars(select(NewsUserState))).all()) == 1
            runs = (await db.scalars(select(BrightDataExecution).order_by(BrightDataExecution.started_at))).all()
            assert [(r.newly_saved_posts, r.duplicate_posts, r.returned_records) for r in runs] == [(1, 1, 2), (0, 2, 2)]
            assert all(r.duration_ms is not None and r.estimated_billable_usage == 6 for r in runs)
        assert len(provider.triggers) == 2
    asyncio.run(run())


def test_duplicate_snapshot_rows_refresh_activity_without_another_item(database, monkeypatch):
    _, sessions = database
    first = records()[0]
    detailed = {**first, "url": "https://www.linkedin.com/feed/update/urn:li:activity:42",
                "num_likes": 9, "num_comments": 1, "comments": [{"text": "Useful"}]}
    provider = Provider(monkeypatch, [first, detailed, first])
    async def run():
        sid = await source(sessions)
        assert await check(sessions, sid) == "started"
        assert await check(sessions, sid) == "completed"
        async with sessions() as db:
            item = (await db.scalars(select(NewsItem))).one()
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert item.linkedin_data["reactionCount"] == 9
            assert item.linkedin_data["comments"][0]["text"] == "Useful"
            assert execution.newly_saved_posts == 1 and execution.duplicate_posts == 2
            assert execution.returned_records == 3 and execution.ignored_records == 0
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_zero_results_keep_usage_reservation(database, monkeypatch):
    _, sessions = database
    Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        assert await check(sessions, sid) == "completed"
        async with sessions() as db:
            run = (await db.scalars(select(BrightDataExecution))).one()
            row = await db.get(NewsSource, sid)
            assert run.returned_records == 0 and run.estimated_billable_usage == 6
            assert row.last_successful_fetch_at == run.started_at and row.empty_fetch_streak == 1
    asyncio.run(run())


def test_simultaneous_jobs_make_one_billable_post(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, delay=0.1)
    async def run():
        sid = await source(sessions)
        results = await asyncio.gather(*(check(sessions, sid) for _ in range(8)))
        assert results.count("started") == 1
        assert len(provider.triggers) == 1
        async with sessions() as db:
            assert len((await db.scalars(select(BrightDataExecution))).all()) == 1
    asyncio.run(run())


@pytest.mark.parametrize("status,message", [(500, "API error"), (400, "Customer is not active"), (402, "Insufficient balance"), (200, "timeout")])
def test_errors_retain_budget_and_never_repeat_trigger(database, monkeypatch, status, message):
    _, sessions = database
    provider = Provider(monkeypatch, status=status, message=message)
    async def run():
        sid = await source(sessions)
        assert await check(sessions, sid) == "error"
        assert await check(sessions, sid) == "paused"
        async with sessions() as db:
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.estimated_billable_usage == 6 and execution.api_errors and execution.state == "uncertain"
            if status in {400, 402}:
                another = await source(sessions)
                assert await check(sessions, another) == "paused"
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_month_budget_serializes_competing_sources(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, delay=0.1)
    async def run():
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            account.provider_billable_records = 3994
            await db.commit()
        ids = [await source(sessions, url=f"https://www.linkedin.com/company/budget-{n}/") for n in range(6)]
        states = await asyncio.gather(*(check(sessions, sid) for sid in ids))
        assert states.count("started") == 1
        assert len(provider.triggers) == 1
        async with sessions() as db:
            status = await budget_status(db)
            assert status["effectiveRecords"] == 4000
            assert status["alerts"] == [50, 75, 90]
    asyncio.run(run())


def test_restart_resumes_snapshot_without_new_post(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        assert await check(sessions, sid) == "started"
        # Entirely new DB session; only persistent source + execution state exist.
        assert await check(sessions, sid) == "completed"
        assert await check(sessions, sid) == "pending"
        assert len(provider.triggers) == 1
    asyncio.run(run())


@pytest.mark.parametrize("already_completed", [False, True])
def test_stale_session_reloads_job_under_source_lock(database, monkeypatch, already_completed):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as stale:
            row = await stale.get(NewsSource, sid)
            await stale.commit()
            assert await check(sessions, sid) == "started"
            if already_completed:
                assert await check(sessions, sid) == "completed"
            result = await collection.protected_linkedin_check(stale, row, datetime.now(timezone.utc))
            assert result == ("pending" if already_completed else "completed")
        assert len(provider.triggers) == 1
        async with sessions() as db:
            assert len((await db.scalars(select(BrightDataExecution))).all()) == 1
    asyncio.run(run())


def test_stale_session_observes_new_account_pause(database, monkeypatch):
    from app.intelligence.credit_control import locked_account
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as stale:
            row = await stale.get(NewsSource, sid)
            account = await locked_account(stale)
            assert not account.paused_reason
            await stale.commit()
            async with sessions() as db:
                current = await db.get(BrightDataAccount, 1)
                current.paused_reason = "Billing reconciliation required"
                await db.commit()
            assert await collection.protected_linkedin_check(stale, row, datetime.now(timezone.utc)) == "paused"
        assert provider.requests == []
    asyncio.run(run())


def test_stale_session_observes_source_deactivation(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as stale:
            row = await stale.get(NewsSource, sid)
            await stale.commit()
            async with sessions() as db:
                current = await db.get(NewsSource, sid)
                current.status = "PAUSED"
                await db.commit()
            assert await collection.protected_linkedin_check(stale, row, datetime.now(timezone.utc)) == "paused"
        assert provider.requests == []
    asyncio.run(run())


def test_malformed_url_keeps_valid_results_and_blocks_watermark(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, [*records(), {"url": "https://[invalid/posts/bad"}])
    async def run():
        sid = await source(sessions)
        assert await check(sessions, sid) == "started"
        assert await check(sessions, sid) == "completed"
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert row.last_successful_fetch_at is None and row.linkedin_paused_reason
            assert execution.state == "partial" and execution.ignored_records == 1
            assert len((await db.scalars(select(NewsItem))).all()) == 1
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_reservation_without_snapshot_after_crash_is_not_retried(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            execution = await db.get(BrightDataExecution, row.pending_execution_id)
            row.pending_snapshot_id = execution.snapshot_id = None
            await db.commit()
        assert await check(sessions, sid) == "paused"
        assert len(provider.triggers) == 1
    asyncio.run(run())


@pytest.mark.parametrize("count", [6, 7])
def test_record_limit_saturation_pauses_without_losing_posts(database, monkeypatch, count):
    _, sessions = database
    provider = Provider(monkeypatch, records(count))
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        assert await check(sessions, sid) == "completed"
        assert await check(sessions, sid) == "paused"
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.last_successful_fetch_at is None and row.linkedin_paused_reason
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.returned_records == count and execution.estimated_billable_usage == max(6, count)
            assert len((await db.scalars(select(NewsItem))).all()) == count
            if count > 6:
                assert (await db.get(BrightDataAccount, 1)).paused_reason
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_additive_migration_clamps_interval_and_retains_posts(database, monkeypatch):
    engine, sessions = database
    provider = Provider(monkeypatch, records())
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        await check(sessions, sid)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            row.fetch_interval_minutes = 5
            await db.commit()
        for _ in range(2):
            async with engine.begin() as connection:
                await migrate_credit_controls(connection)
        async with sessions() as db:
            assert (await db.get(NewsSource, sid)).fetch_interval_minutes == 1440
            assert len((await db.scalars(select(NewsItem))).all()) == 1
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_website_refreshes_work_when_fetching_paused(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, records())
    from app.auth import get_current_user, require_admin
    from app.db import get_db
    from app.main import app
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        await check(sessions, sid)
        monkeypatch.setattr(settings, "NEWS_LINKEDIN_FETCH_ENABLED", False)
        async def db_dependency():
            async with sessions() as db:
                yield db
        user = SimpleNamespace(id=uuid4(), role="ADMIN")
        app.dependency_overrides.update({get_db: db_dependency, get_current_user: lambda: user, require_admin: lambda: user})
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for _ in range(5):
                    response = await client.get("/api/intelligence/items")
                    assert response.status_code == 200 and len(response.json()["items"]) == 1
                    assert (await client.get("/api/intelligence/collection-budget")).status_code == 200
                assert (await client.post(f"/api/intelligence/sources/{sid}/check")).status_code == 409
                assert (await client.post("/api/intelligence/sources/check-all")).status_code == 409
                history = await client.get("/api/intelligence/collection-executions")
                assert history.status_code == 200 and history.json()[0]["returned_records"] == 1
                assert len(provider.triggers) == 1
        finally:
            app.dependency_overrides.clear()
    asyncio.run(run())


def test_scheduler_cycles_and_restart_do_not_repeat_triggers(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        await source(sessions)
        await asyncio.gather(collection.check_due_sources(), collection.check_due_sources())
        await collection.check_due_sources()
        await collection.check_due_sources()
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_daily_target_and_fair_rotation(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        ids = [await source(sessions, url=f"https://www.linkedin.com/company/daily-{n}/") for n in range(22)]
        states = [await check(sessions, sid) for sid in ids]
        assert states.count("started") == 21 and states[-1] == "paused"
        async with sessions() as db:
            status = await budget_status(db)
            assert status["dailyRecords"] == 126
            # The denied source retains its earlier start and sorts first next day.
            assert (await db.get(NewsSource, ids[-1])).last_started_at is None
        assert len(provider.triggers) == 21
    asyncio.run(run())


def test_inactive_evidence_prevents_any_provider_request(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            a = await db.get(BrightDataAccount, 1)
            a.provider_active, a.provider_balance = False, -1
            await db.commit()
        assert await check(sessions, sid) == "paused"
        assert provider.requests == []
    asyncio.run(run())


def test_provider_error_records_do_not_advance_watermark(database, monkeypatch):
    _, sessions = database
    Provider(monkeypatch, [{"error": "Customer is not active"}])
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        assert await check(sessions, sid) == "error"
        async with sessions() as db:
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.returned_records == 1 and execution.api_errors
            assert (await db.get(NewsSource, sid)).last_successful_fetch_at is None
            assert (await db.get(BrightDataAccount, 1)).paused_reason
    asyncio.run(run())


def test_storage_failure_keeps_raw_usage_and_bounds_retrieval_retries(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch, records())
    async def fail(*args):
        raise RuntimeError("Analysis unavailable")
    monkeypatch.setattr(collection, "analyze_news_item", fail)
    async def run():
        sid = await source(sessions)
        await check(sessions, sid)
        for _ in range(4):
            assert await check(sessions, sid) == "error"
        assert await check(sessions, sid) == "paused"
        async with sessions() as db:
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.returned_records == 1 and execution.retry_count == 3
            assert execution.estimated_billable_usage == 6 and execution.duration_ms is not None
            assert len((await db.scalars(select(NewsItem))).all()) == 0
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_timeout_and_legacy_snapshots_never_retrigger(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    async def run():
        legacy = await source(sessions, pending_snapshot_id="sd_legacy123")
        assert await check(sessions, legacy) == "paused"
        sid = await source(sessions, url="https://www.linkedin.com/company/nonlegacy/")
        await check(sessions, sid)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            execution = await db.get(BrightDataExecution, row.pending_execution_id)
            execution.started_at -= timedelta(days=2)
            await db.commit()
        assert await check(sessions, sid) == "paused"
        assert len(provider.triggers) == 1
    asyncio.run(run())


def test_explicit_backfill_requires_approval_and_keeps_budget_gate(database, monkeypatch):
    _, sessions = database
    provider = Provider(monkeypatch)
    from app import brightdata_admin
    monkeypatch.setattr(brightdata_admin, "SessionLocal", sessions)
    async def run():
        sid = await source(sessions)
        now = datetime.now(timezone.utc)
        args = SimpleNamespace(command="backfill", source=sid, start=(now - timedelta(days=60)).date(),
            end=now.date(), max_records=20, authorize_billable=False, approval_ref="DEV-MOCK-ONLY")
        with pytest.raises(ValueError, match="authorize-billable"):
            await brightdata_admin.run(args)
        assert provider.requests == []
        args.authorize_billable = True
        await brightdata_admin.run(args)
        assert len(provider.triggers) == 1
        assert provider.triggers[0].url.params["limit_per_input"] == "20"
        async with sessions() as db:
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert execution.trigger_reason == "backfill:DEV-MOCK-ONLY" and execution.estimated_billable_usage == 20
    asyncio.run(run())


def test_billing_import_tracks_actual_usage_and_never_reactivates_account(database, monkeypatch, tmp_path):
    import json
    from app import brightdata_admin
    _, sessions = database
    monkeypatch.setattr(brightdata_admin, "SessionLocal", sessions)
    provider = Provider(monkeypatch)
    async def run():
        now = datetime.now(timezone.utc)
        evidence = tmp_path / "billing.json"
        payload = dict(month=now.strftime("%Y-%m"), usage_as_of=now.isoformat(), balance=-1,
                       active=False, billable_records=7049, verified_monthly_allowance=5000, evidence="DEV-MOCK-ONLY")
        evidence.write_text(json.dumps(payload))
        args = SimpleNamespace(command="billing-import", file=str(evidence), clear_pause=False, approval_ref=None)
        await brightdata_admin.run(args)
        async with sessions() as db:
            a = await db.get(BrightDataAccount, 1)
            assert a.provider_billable_records == 7049 and a.paused_reason
            assert a.alert_levels == [50, 75, 90]
        assert provider.requests == []
        # Active evidence alone does not clear an existing inactive-account latch.
        payload.update(active=True, balance=1)
        evidence.write_text(json.dumps(payload))
        await brightdata_admin.run(args)
        assert await check(sessions, await source(sessions)) == "paused"
        args.clear_pause = True
        with pytest.raises(ValueError, match="approval-ref"):
            await brightdata_admin.run(args)
        # Even approval cannot bypass the monthly operational ceiling.
        args.approval_ref = "DEV-MOCK-ONLY"
        await brightdata_admin.run(args)
        assert await check(sessions, await source(sessions)) == "paused"
        assert provider.requests == []
    asyncio.run(run())


def test_additive_migration_on_legacy_source_schema(database):
    engine, sessions = database
    from app.intelligence.credit_migration import SOURCE_COLUMNS
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            row.fetch_interval_minutes = 5
            row.last_checked_at = datetime.now(timezone.utc) - timedelta(days=1)
            await db.commit()
        # Simulate a pre-fix database, without touching item or analysis tables.
        async with engine.begin() as connection:
            for column in SOURCE_COLUMNS:
                await connection.execute(text(f"ALTER TABLE intelligence_sources DROP COLUMN {column}"))
            await migrate_credit_controls(connection)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.fetch_interval_minutes == 1440
            assert row.last_successful_fetch_at == row.last_checked_at
            assert row.empty_fetch_streak == 0
    asyncio.run(run())


@pytest.mark.parametrize("provider_used,expected", [(0, "started"), (3990, "paused")])
def test_high_priority_extra_requires_monthly_headroom(database, monkeypatch, provider_used, expected):
    _, sessions = database
    monkeypatch.setattr(settings, "NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA", True)
    provider = Provider(monkeypatch)
    async def run():
        now = datetime.now(timezone.utc)
        sid = await source(sessions, priority="HIGH", last_started_at=now - timedelta(hours=13), last_new_posts=1)
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            account.provider_billable_records = provider_used
            await db.commit()
        assert await check(sessions, sid) == expected
        assert len(provider.triggers) == int(expected == "started")
        if expected == "started":
            async with sessions() as db:
                execution = (await db.scalars(select(BrightDataExecution))).one()
                assert execution.trigger_reason == "scheduled-extra"
    asyncio.run(run())
