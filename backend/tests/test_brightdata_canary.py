"""Canary safety boundaries: real private PostgreSQL, provider HTTP mocked only."""
import asyncio
import importlib
import importlib.util
import json
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text

from app.config import settings
from app.intelligence import collection
from app.intelligence.models import BrightDataAccount, BrightDataExecution, NewsItem, NewsSource, NewsUserState
from test_brightdata_postgres_integration import database, private_postgres, records, source


def canary_module():
    assert importlib.util.find_spec("app.intelligence.brightdata_canary") is not None, "Single-source canary is not implemented"
    return importlib.import_module("app.intelligence.brightdata_canary")


def test_canary_command_exists():
    canary_module()


@pytest.fixture
def canary(database, monkeypatch):
    module = canary_module()
    engine, sessions = database
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "NEWS_ENABLE_COLLECTION", True)
    monkeypatch.setattr(settings, "NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED", True)
    async def setup():
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            account.evidence = "MOCK-ACCOUNT billing fixture, not a real entitlement"
            await db.commit()
    asyncio.run(setup())
    return module, sessions


def arguments(module, sid, *, live=False, ref="MOCK-APPROVAL-1"):
    argv = ["--source-id", str(sid)]
    if live:
        argv += ["--live", "--confirm-live-request", "--approval-ref", ref,
                 "--account-ref", "MOCK-ACCOUNT", "--exclusive-account-ref", "MOCK-WORKER-REVIEW",
                 "--max-reserved-records", "6"]
    return module.parser().parse_args(argv)


async def no_wait(_seconds):
    pass


class HTTP:
    """Only the remote transport is replaced; request guards/parsers remain real."""
    def __init__(self, monkeypatch, *, rows=None, failure=None, delay=0, progress="ready"):
        from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter
        self.requests = []
        async def respond(request):
            self.requests.append(request)
            if request.method == "POST":
                if delay:
                    await asyncio.sleep(delay)
                if failure == "trigger-timeout":
                    raise httpx.ReadTimeout("test-token must never appear in the report", request=request)
                return httpx.Response(200, json={"snapshot_id": "sd_canary123"})
            if "/progress/" in request.url.path:
                if failure == "progress-timeout":
                    raise httpx.ReadTimeout("test-token", request=request)
                if failure == "inactive":
                    return httpx.Response(402, json={"error": "Account inactive"})
                return httpx.Response(200, json={"status": progress})
            return httpx.Response(200, json=rows if rows is not None else records())
        monkeypatch.setattr(BrightDataLinkedInAdapter, "_client", lambda _self: httpx.AsyncClient(
            base_url="https://api.brightdata.com/datasets/v3/", timeout=25,
            transport=httpx.MockTransport(respond), follow_redirects=False))

    @property
    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


@pytest.mark.parametrize("argv", [[], ["--source-id", "bad"],
    ["--source-url", "https://evil.example/in/me"],
    ["--source-url", "https://user:secret@www.linkedin.com/in/me"],
    ["--source-url", "https://www.linkedin.com/posts/me_activity-42"],
    ["--source-id", str(uuid4()), "--source-id", str(uuid4())],
    ["--source-id", str(uuid4()), "--source-url", "https://www.linkedin.com/in/me"],
    ["--source-id", str(uuid4()), "--legacy-worker"],
    ["--source-id", str(uuid4()), "--start", "2026-01-01"]])
def test_rejects_missing_ambiguous_invalid_or_legacy_selection(argv):
    with pytest.raises(SystemExit):
        canary_module().parser().parse_args(argv)


def test_dry_run_selects_one_and_makes_no_provider_or_database_writes(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        first, second = await source(sessions), await source(sessions)
        preview = await module.run(arguments(module, first))
        assert preview["mode"] == "dry-run" and preview["source"]["id"] == str(first)
        assert preview["liveApiCalls"] == 0 and preview["requestedRecordLimit"] == 6
        assert preview["remainingInternalBudget"] == 4000 and preview["currentMonthlyReservation"] == 0
        assert preview["automaticScheduler"] == "Not started by this command"
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(BrightDataExecution)) == 0
            assert await db.scalar(select(func.count()).select_from(module.BrightDataCanaryRun)) == 0
            assert (await db.get(NewsSource, second)).last_started_at is None
        assert http.requests == []
    asyncio.run(run())


def test_selects_existing_url_without_creating_or_expanding_sources(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid = await source(sessions)
        args = module.parser().parse_args(["--source-url", "https://linkedin.com/company/example"])
        preview = await module.run(args)
        assert preview["source"]["id"] == str(sid)
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NewsSource)) == 1
        assert http.requests == []
    asyncio.run(run())


@pytest.mark.parametrize("case,fragment", [
    ("budget", "monthly"), ("daily", "Daily"), ("unverified", "billing"),
    ("stale", "billing"), ("allowance", "monthly"), ("inactive", "inactive"),
    ("token", "credentials"), ("global-off", "global"), ("fetch-off", "disabled"),
    ("live-off", "live mode"), ("limit", "six"), ("dataset", "dataset"),
    ("pending", "pending"), ("not-due", "due"), ("authorization", "authorization"),
    ("budget-approval", "six"), ("exclusive", "exclusive"), ("account-ref", "account"),
    ("extras", "extras"),
])
def test_live_preflight_blocks_unsafe_configuration(canary, monkeypatch, case, fragment):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid = await source(sessions)
        args = arguments(module, sid, live=True)
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            row = await db.get(NewsSource, sid)
            if case == "budget": account.provider_billable_records = 3995
            if case == "daily":
                now = datetime.now(timezone.utc)
                db.add(BrightDataExecution(source_id=sid, started_at=now, budget_month=now.strftime("%Y-%m"),
                    endpoint="mock", dataset_id="mock", start_date=now.date(), end_date=now.date(),
                    trigger_reason="fixture", record_limit=126, estimated_billable_usage=126, state="completed"))
            if case == "unverified": account.verified_at = None
            if case == "stale": account.usage_as_of -= timedelta(hours=25)
            if case == "allowance": account.verified_monthly_allowance = None
            if case == "inactive": account.provider_active = False
            if case == "pending": row.pending_snapshot_id = "sd_legacy123"
            if case == "not-due": row.last_started_at = datetime.now(timezone.utc)
            await db.commit()
        if case == "token": monkeypatch.setattr(settings, "BRIGHTDATA_API_TOKEN", None)
        if case == "global-off": monkeypatch.setattr(settings, "NEWS_ENABLE_COLLECTION", False)
        if case == "fetch-off": monkeypatch.setattr(settings, "NEWS_LINKEDIN_FETCH_ENABLED", False)
        if case == "live-off": monkeypatch.setattr(settings, "NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED", False)
        if case == "limit": monkeypatch.setattr(settings, "NEWS_LINKEDIN_RECORD_LIMIT", 5)
        if case == "dataset": monkeypatch.setattr(settings, "BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID", "other")
        if case == "authorization": args.confirm_live_request = False
        if case == "budget-approval": args.max_reserved_records = 7
        if case == "exclusive": args.exclusive_account_ref = None
        if case == "account-ref": args.account_ref = None
        if case == "extras": monkeypatch.setattr(settings, "NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA", True)
        with pytest.raises(module.CanaryError, match=fragment):
            await module.run(args, wait=no_wait)
        assert http.requests == []
    asyncio.run(run())


def test_live_is_one_job_and_approval_cannot_be_reused_after_restart(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid, other = await source(sessions), await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["apiRequests"] == 3 and report["scrapingTriggerAttempts"] == 1
        assert report["scrapingJobsAccepted"] == 1 and report["returnedRecords"] == 1
        assert report["newPosts"] == 1 and report["uniqueLinkedInPostIds"] == 1
        assert report["providerBillableRecords"] is None and report["estimatedBillableUsage"] == 6
        request = http.posts[0]
        assert request.url.params["limit_per_input"] == "6"
        assert request.url.params["discover_by"] == "company_url"
        body = json.loads(request.content)
        assert len(body) == 1 and body[0]["url"] == "https://www.linkedin.com/company/example/"
        assert body[0]["start_date"] == (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        assert "test-token" not in json.dumps(report)
        async with sessions() as db:
            assert (await db.get(NewsSource, other)).last_started_at is None
            row = await db.get(NewsSource, sid)
            row.last_started_at -= timedelta(days=2)
            await db.commit()
        with pytest.raises(module.CanaryError, match="approval.*used"):
            await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert len(http.posts) == 1
    asyncio.run(run())


def test_concurrent_commands_and_normal_collector_cannot_trigger_extra_jobs(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, delay=0.05)
    async def run():
        sid = await source(sessions)
        results = await asyncio.gather(
            module.run(arguments(module, sid, live=True), wait=no_wait),
            module.run(arguments(module, sid, live=True, ref="MOCK-APPROVAL-2"), wait=no_wait),
            return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, module.CanaryError) for r in results) == 1
        other = await source(sessions)
        async with sessions() as db:
            assert await collection.check_source(db, other) == "paused"
        assert len(http.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["trigger-timeout", "progress-timeout", "inactive"])
def test_errors_never_retry_or_advance_checkpoint_and_retain_reservation(canary, monkeypatch, failure):
    module, sessions = canary
    http = HTTP(monkeypatch, failure=failure)
    async def run():
        old = datetime.now(timezone.utc) - timedelta(days=3)
        sid = await source(sessions, last_successful_fetch_at=old)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["state"] == "stopped" and report["errors"]
        assert report["scrapingTriggerAttempts"] == 1
        assert report["apiRequests"] <= 2 and len(http.posts) == 1
        assert "test-token" not in json.dumps(report)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert row.last_successful_fetch_at == old and row.linkedin_paused_reason
            assert execution.estimated_billable_usage == 6 and execution.retry_count == 0
    asyncio.run(run())


@pytest.mark.parametrize("count", [6, 7])
def test_saturation_or_overdelivery_stops_and_preserves_checkpoint(canary, monkeypatch, count):
    module, sessions = canary
    http = HTTP(monkeypatch, rows=records(count))
    async def run():
        sid = await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["state"] == "stopped" and report["returnedRecords"] == count
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.last_successful_fetch_at is None and row.linkedin_paused_reason
            if count == 7:
                assert (await db.get(BrightDataAccount, 1)).paused_reason
                assert report["estimatedBillableUsage"] == 7
        with pytest.raises(module.CanaryError):
            await module.run(arguments(module, sid, live=True, ref="MOCK-APPROVAL-2"), wait=no_wait)
        assert len(http.posts) == 1
    asyncio.run(run())


def test_duplicates_and_reposts_preserve_existing_posts_and_read_state(canary, monkeypatch):
    module, sessions = canary
    original = records()[0]
    repost = {**records()[0], "id": "99", "url": "https://www.linkedin.com/posts/example_activity-99-abc",
              "repost": {"repost_url": "https://www.linkedin.com/posts/original_activity-12-abc",
                         "repost_text": "Original author's words"}, "post_type": "repost"}
    http = HTTP(monkeypatch, rows=[original, original, repost])
    async def run():
        sid = await source(sessions)
        item_id, user_id = uuid4(), uuid4()
        async with sessions() as db:
            db.add(NewsItem(id=item_id, source_id=sid, external_id="42", url=original["url"], title="Keep this",
                            original_text="Existing immutable canary baseline"))
            await db.flush()
            db.add(NewsUserState(user_id=user_id, news_item_id=item_id, saved_at=datetime.now(timezone.utc)))
            await db.commit()
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["returnedRecords"] == 3 and report["uniqueLinkedInPostIds"] == 2
        assert report["newPosts"] == 1 and report["duplicatePosts"] == 2
        assert report["reposts"] == 1 and report["invalidRecords"] == 0
        async with sessions() as db:
            assert (await db.get(NewsItem, item_id)).title == "Keep this"
            assert (await db.get(NewsUserState, (user_id, item_id))).saved_at is not None
            assert await db.scalar(select(func.count()).select_from(NewsItem)) == 2
            assert (await db.get(NewsSource, sid)).last_successful_fetch_at is not None
    asyncio.run(run())


def test_empty_results_are_not_assumed_free(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, rows=[])
    async def run():
        sid = await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["state"] == "completed" and report["newPosts"] == 0
        assert report["estimatedBillableUsage"] == 6 and report["providerBillableRecords"] is None
        assert report["returnedRecords"] == 0 and len(http.posts) == 1
    asyncio.run(run())


def test_pending_polling_has_finite_bound_and_no_retrigger(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, progress="running")
    async def run():
        sid = await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["state"] == "stopped" and report["apiRequests"] == 7
        assert len(http.posts) == 1
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.pending_snapshot_id == "sd_canary123" and row.linkedin_paused_reason
    asyncio.run(run())


def test_production_database_target_is_rejected_before_connection(monkeypatch):
    module = canary_module()
    monkeypatch.setattr(settings, "NEWS_DATABASE_URL", "postgresql+asyncpg://news_intel:never-print@127.0.0.1:5432/news_intel")
    with pytest.raises(module.CanaryError, match="private"):
        asyncio.run(module.run(arguments(module, uuid4())))


def test_incremental_overlap_and_repost_author_filtering_are_not_newness_guarantees(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, rows=[{"id": "101", "url": "https://www.linkedin.com/posts/original_activity-101-abc",
        "user_id": "original", "reposted_by": "https://www.linkedin.com/in/example/", "post_type": "repost",
        "post_text": "Shared", "date_posted": "2020-01-01T00:00:00Z"}])
    async def run():
        old = datetime.now(timezone.utc) - timedelta(days=30)
        sid = await source(sessions, last_successful_fetch_at=old)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            row.url = "https://www.linkedin.com/in/example/"
            await db.commit()
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        body = json.loads(http.posts[0].content)[0]
        assert body["start_date"] == (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        assert http.posts[0].url.params["discover_by"] == "profile_url"
        assert report["reposts"] == 1 and report["outOfWindowRecords"] == 1
        assert report["state"] == "stopped"
        async with sessions() as db:
            assert (await db.get(NewsSource, sid)).last_successful_fetch_at == old
    asyncio.run(run())


def test_no_new_posts_deduplicate_without_rewriting_stored_content(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, rows=[records()[0], records()[0]])
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            db.add(NewsItem(source_id=sid, external_id="42", url=records()[0]["url"], title="Preserved"))
            await db.commit()
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["newPosts"] == 0 and report["duplicatePosts"] == 2
        assert report["uniqueLinkedInPostIds"] == 1 and report["estimatedBillableUsage"] == 6
        async with sessions() as db:
            assert (await db.scalars(select(NewsItem))).one().title == "Preserved"
            assert (await db.get(NewsSource, sid)).last_successful_fetch_at is not None
    asyncio.run(run())


def test_normal_source_advisory_lock_is_honored(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid = await source(sessions)
        from app.intelligence.linkedin_identity import lock_key
        key = lock_key("https://www.linkedin.com/company/example/")
        async with module.engine.connect() as connection:
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                with pytest.raises(module.CanaryError, match="source lock"):
                    await module.run(arguments(module, sid, live=True), wait=no_wait)
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        assert http.requests == []
    asyncio.run(run())


def test_cancelled_process_keeps_consumed_approval_and_pending_reservation(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def cancelled(_):
        raise asyncio.CancelledError()
    async def run():
        sid = await source(sessions)
        with pytest.raises(asyncio.CancelledError):
            await module.run(arguments(module, sid, live=True), wait=cancelled)
        async with sessions() as db:
            claim = (await db.scalars(select(module.BrightDataCanaryRun))).one()
            execution = (await db.scalars(select(BrightDataExecution))).one()
            assert claim.completed_at is None and execution.estimated_billable_usage == 6
            assert claim.report["liveApiCalls"] is None  # A crash is never logged as zero paid exposure.
        with pytest.raises(module.CanaryError, match="approval.*used"):
            await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert len(http.posts) == 1
    asyncio.run(run())


def test_monthly_budget_reached_blocks_a_different_canary_source(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid, other = await source(sessions), await source(sessions)
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            account.provider_billable_records = 3994
            await db.commit()
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["estimatedBillableUsage"] == 6
        with pytest.raises(module.CanaryError, match="monthly"):
            await module.run(arguments(module, other, live=True, ref="MOCK-APPROVAL-2"), wait=no_wait)
        assert len(http.posts) == 1
    asyncio.run(run())


def test_request_guard_refuses_a_second_post_even_if_called_directly(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        sid = await source(sessions)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            spec = module.request_configuration(row, datetime.now(timezone.utc))
            adapter = module.CanaryAdapter("test-token", spec, row)
            dates = spec["body"][0]
            await adapter.trigger(row, start_date=dates["start_date"], end_date=dates["end_date"], limit=6)
            with pytest.raises(module.LinkedInCollectionError, match="one-trigger"):
                await adapter.trigger(row, start_date=dates["start_date"], end_date=dates["end_date"], limit=6)
        assert len(http.posts) == 1
    asyncio.run(run())


def test_cli_default_dry_run_renders_table_without_requests(canary, monkeypatch, capsys):
    import sys
    module, sessions = canary
    http = HTTP(monkeypatch)
    sid = asyncio.run(source(sessions))
    monkeypatch.setattr(sys, "argv", ["brightdata_canary", "--source-id", str(sid)])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "| Live API calls | 0 |" in output
    assert "| Requested record limit | 6 |" in output
    assert "test-token" not in output and "Bearer" not in output
    assert http.requests == []


def test_cli_redacts_stored_pause_errors(canary, monkeypatch, capsys):
    import sys
    module, sessions = canary
    async def setup():
        sid = await source(sessions)
        async with sessions() as db:
            account = await db.get(BrightDataAccount, 1)
            account.paused_reason = "Sensitive provider message test-token"
            await db.commit()
        return sid
    sid = asyncio.run(setup())
    args = arguments(module, sid, live=True)
    monkeypatch.setattr(module, "parser", lambda: type("Parser", (), {"parse_args": lambda _: args})())
    assert module.main() == 2
    output = capsys.readouterr().out
    assert "test-token" not in output


def test_unknown_and_ambiguous_sources_are_not_automatically_created(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch)
    async def run():
        with pytest.raises(module.CanaryError, match="Exactly one"):
            await module.run(arguments(module, uuid4()))
        await source(sessions)
        await source(sessions)
        args = module.parser().parse_args(["--source-url", "https://www.linkedin.com/company/example/"])
        with pytest.raises(module.CanaryError, match="Exactly one"):
            await module.run(args)
        assert http.requests == []
    asyncio.run(run())


@pytest.mark.parametrize("case", ["invalid-row", "error-code-only", "record-error", "missing-date", "invalid-date"])
def test_partial_or_undated_results_do_not_advance_success_or_store_unreviewed_rows(canary, monkeypatch, case):
    module, sessions = canary
    rows = records()
    if case == "invalid-row": rows.append({"unexpected": "schema"})
    if case == "error-code-only": rows.append({"error_code": "dead_page"})
    if case == "record-error": rows.append({"error": "Dead page", "error_code": "dead_page"})
    if case == "missing-date": rows[0].pop("date_posted")
    if case == "invalid-date": rows[0]["date_posted"] = "not-a-date"
    http = HTTP(monkeypatch, rows=rows)
    async def run():
        sid = await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["state"] == "stopped" and report["estimatedBillableUsage"] == 6
        assert report["newPosts"] == 0 and report["returnedRecords"] == len(rows)
        async with sessions() as db:
            row = await db.get(NewsSource, sid)
            assert row.last_successful_fetch_at is None and row.linkedin_paused_reason
            assert await db.scalar(select(func.count()).select_from(NewsItem)) == 0
        assert len(http.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("change", ["type", "url", "external-url"])
def test_source_edited_before_lock_cannot_change_the_authorized_request(canary, monkeypatch, change):
    module, sessions = canary
    http = HTTP(monkeypatch)
    original_select = module.selected_source
    edited = False
    async def select_then_edit(db, args):
        nonlocal edited
        row = await original_select(db, args)
        if not edited:
            edited = True
            async with sessions() as edit_db:
                updated = await edit_db.get(NewsSource, row.id)
                if change == "type": updated.type = "RSS"
                elif change == "url": updated.url = "https://www.linkedin.com/company/another/"
                else: updated.url = "https://other.example/feed"
                await edit_db.commit()
        return row
    monkeypatch.setattr(module, "selected_source", select_then_edit)
    async def run():
        sid = await source(sessions)
        with pytest.raises(module.CanaryError, match="changed|LinkedIn"):
            await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert http.requests == []
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(module.BrightDataCanaryRun)) == 0
    asyncio.run(run())


def test_unidentified_post_links_are_not_reported_as_verified_linkedin_ids(canary, monkeypatch):
    module, sessions = canary
    http = HTTP(monkeypatch, rows=[{"url": "https://www.linkedin.com/posts/example-no-activity-id",
        "post_text": "No canonical ID", "date_posted": datetime.now(timezone.utc).isoformat()}])
    async def run():
        sid = await source(sessions)
        report = await module.run(arguments(module, sid, live=True), wait=no_wait)
        assert report["uniqueLinkedInPostIds"] == 0
        assert report["unidentifiedPostRecords"] == 1 and report["state"] == "stopped"
        assert report["newPosts"] == 0 and len(http.posts) == 1
    asyncio.run(run())
