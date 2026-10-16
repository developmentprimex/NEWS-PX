"""One-source Bright Data experiment. Dry-run by default; never starts an app/scheduler.

Live mode is restricted to a private loopback clone, requires all collection gates
and explicit operator attestations, and permanently consumes one approval before
any provider traffic. Imported provider billing remains separate from estimates.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal, engine
from app.intelligence.credit_control import denial_reason, executions_for_budget, usage_totals
from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter, LinkedInCollectionError, parse_posts
from app.intelligence.linkedin_collection import collect_linkedin, finish, incremental_start
from app.intelligence.linkedin_schedule import is_due
from app.intelligence.models import BrightDataAccount, BrightDataCanaryRun, BrightDataExecution, NewsItem, NewsSource

POSTS_DATASET = "gd_lyy3tktm25m4avu764"
MAX_PROGRESS_READS = 6
MONITOR_SECONDS = 600


class CanaryError(ValueError):
    """Safe operator-facing refusal, without connection strings or credentials."""


def canonical_source_url(value):
    try:
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        valid = (url.scheme == "https" and url.hostname in {"www.linkedin.com", "linkedin.com"}
                 and not url.username and not url.password and not url.port and not url.query and not url.fragment
                 and len(parts) == 2 and parts[0] in {"in", "company"}
                 and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1]))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("Use one HTTPS LinkedIn company/profile URL without credentials or query parameters")
    return f"https://www.linkedin.com/{parts[0]}/{parts[1].casefold()}/"


class OneValue(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise argparse.ArgumentError(self, "Select exactly one value; repeated options are forbidden")
        setattr(namespace, self.dest, values)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    selector = result.add_mutually_exclusive_group(required=True)
    selector.add_argument("--source-id", type=UUID, action=OneValue)
    selector.add_argument("--source-url", type=canonical_source_url, action=OneValue)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    result.add_argument("--confirm-live-request", action="store_true")
    result.add_argument("--approval-ref", action=OneValue)
    result.add_argument("--account-ref", action=OneValue)
    result.add_argument("--exclusive-account-ref", action=OneValue)
    result.add_argument("--max-reserved-records", type=int, action=OneValue)
    result.add_argument("--json", action="store_true")
    return result


def private_target():
    """No production-target override. Also supports the existing private QA cluster."""
    try:
        target = make_url(settings.NEWS_DATABASE_URL)
        valid = (target.drivername == "postgresql+asyncpg" and target.host == "127.0.0.1"
                 and (target.port, target.username) in {(55441, "credit_canary"), (55439, "credit_test")}
                 and target.database == "postgres" and not target.query)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise CanaryError("Canary requires a dedicated private loopback database; production targets are forbidden")
    return target


async def verify_identity(db, target):
    identity = (await db.execute(text("SELECT current_database(), current_user, inet_server_port()"))).one()
    if tuple(identity) != ("postgres", target.username, target.port):
        raise CanaryError("Connected database identity does not match the approved private target")


async def selected_source(db, args):
    if args.source_id:
        source = await db.get(NewsSource, args.source_id)
        candidates = [source] if source else []
    else:
        candidates = []
        for source in await db.scalars(select(NewsSource).where(NewsSource.type == "LINKEDIN")):
            try:
                if canonical_source_url(source.url) == args.source_url:
                    candidates.append(source)
            except argparse.ArgumentTypeError:
                continue
    if len(candidates) != 1:
        raise CanaryError("Exactly one existing source must match; no source will be created")
    source = candidates[0]
    if source.type != "LINKEDIN":
        raise CanaryError("The selected source must be LinkedIn")
    try:
        canonical_source_url(source.url)
    except argparse.ArgumentTypeError as exc:
        raise CanaryError("Stored source URL is not a valid LinkedIn company/profile URL") from exc
    return source


def request_configuration(source, now):
    from app.intelligence.linkedin_adapter import discovery_request
    query, body = discovery_request(settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID, source,
        incremental_start(source.last_successful_fetch_at, now).isoformat(), now.date().isoformat(), 6)
    return {"method": "POST", "endpoint": "https://api.brightdata.com/datasets/v3/trigger",
        "query": query, "body": body,
        "timeoutSeconds": 25, "automaticRetries": 0, "pagination": False, "enrichment": False,
        "snapshotHandling": "Persist ID; at most six status reads and one download; never retrigger",
        "maximumProgressReads": MAX_PROGRESS_READS, "monitoringSeconds": MONITOR_SECONDS}


async def preview(db, source, args, now):
    account = await db.get(BrightDataAccount, 1)
    executions = await executions_for_budget(db)
    totals = usage_totals(account or BrightDataAccount(id=1), executions, now)
    blockers = []
    if not settings.NEWS_ENABLE_COLLECTION:
        blockers.append("The global collection-disable flag must not be bypassed")
    if not settings.BRIGHTDATA_API_TOKEN:
        blockers.append("Bright Data credentials are missing")
    if settings.NEWS_LINKEDIN_RECORD_LIMIT != 6:
        blockers.append("The canary requires exactly six configured records")
    if settings.NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA:
        blockers.append("Optional high-priority extras must be disabled for the canary")
    if settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID != POSTS_DATASET:
        blockers.append("Only the reviewed LinkedIn posts dataset is allowed")
    if source.status != "ACTIVE":
        blockers.append("Selected source is inactive")
    if source.pending_snapshot_id or source.pending_execution_id:
        blockers.append("A pending/legacy execution requires reconciliation first")
    if source.linkedin_paused_reason:
        blockers.append("Selected source is paused and requires review")
    from app.intelligence.linkedin_identity import canonical_linkedin_url, matching_sources
    from app.intelligence.models import LinkedInSourceIdentity
    identity = canonical_linkedin_url(source.url)
    owner = await db.get(LinkedInSourceIdentity, identity)
    candidates = await matching_sources(db, identity) if owner is None else []
    owner_id = owner.source_id if owner else candidates[0].id if candidates else None
    if owner_id != source.id:
        blockers.append("A different historical source owns this canonical LinkedIn URL")
    if not is_due(source, now):
        blockers.append("Selected source is not due; canary never forces the daily interval")
    if not account or not account.evidence:
        blockers.append("Confirmed provider billing evidence is missing")
    if account:
        denial = denial_reason(account, totals, now, 6)
        if denial:
            blockers.append(denial)
    if args.live:
        if not settings.NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED:
            blockers.append("Canary live mode is disabled by configuration")
        if not args.confirm_live_request or not args.approval_ref or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,100}", args.approval_ref):
            blockers.append("Explicit live authorization and a valid approval reference are required")
        if args.max_reserved_records != 6:
            blockers.append("An explicit approved budget of exactly six reserved records is required")
        if not args.account_ref or not args.account_ref.strip():
            blockers.append("A verified provider account reference is required")
        if not args.exclusive_account_ref or not args.exclusive_account_ref.strip():
            blockers.append("A current exclusive-account/legacy-worker verification reference is required")
    return {"mode": "live" if args.live else "dry-run", "source": {"id": str(source.id), "name": source.name, "url": source.url},
        "provider": "Bright Data", "dataset": settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID, "requestedRecordLimit": 6,
        "fetchDateRange": request_configuration(source, now)["body"][0], "lastSuccessfulFetch": source.last_successful_fetch_at,
        "currentMonthlyReservation": totals["estimatedRecords"], "providerActualMonthlyRecords": totals["providerRecords"],
        "effectiveMonthlyUsage": totals["effectiveRecords"], "operationalBudget": totals["operationalBudget"],
        "remainingInternalBudget": max(0, totals["operationalBudget"] - totals["effectiveRecords"]),
        "automaticScheduler": "Not started by this command", "liveApiCalls": 0,
        "liveModeEnabled": settings.NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED,
        "requestConfiguration": request_configuration(source, now), "blockers": blockers,
        "billingWarning": "Internal reservations are estimates, never a guaranteed maximum provider charge"}


class CanaryAdapter(BrightDataLinkedInAdapter):
    """Guards the actual outgoing HTTP boundary, including accidental repeated calls."""
    def __init__(self, token, specification, source):
        super().__init__(token, specification["query"]["dataset_id"])
        self.specification, self.source = specification, source
        self.requests, self.trigger_attempts, self.accepted_jobs = [], 0, 0
        self.snapshot_id, self.downloads = None, 0
        self.progress_reads = 0
        self.metrics = {"returnedRecords": None, "uniqueLinkedInPostIds": 0, "reposts": 0,
                        "invalidRecords": 0, "outOfWindowRecords": 0, "providerErrorRecords": 0,
                        "undatedRecords": 0, "unidentifiedPostRecords": 0}

    async def _guard(self, request):
        base = "https://api.brightdata.com/datasets/v3/"
        valid = False
        if request.method == "POST" and str(request.url).split("?")[0] == base + "trigger":
            query = {k: str(v) for k, v in self.specification["query"].items()}
            valid = (self.trigger_attempts == 0 and dict(request.url.params) == query
                     and json.loads(request.content) == self.specification["body"])
            if valid:
                self.trigger_attempts += 1
        elif request.method == "GET" and self.snapshot_id:
            if str(request.url) == base + "progress/" + self.snapshot_id and self.progress_reads < MAX_PROGRESS_READS:
                self.progress_reads += 1
                valid = True
            elif str(request.url) == base + "snapshot/" + self.snapshot_id + "?format=json" and self.downloads == 0:
                self.downloads += 1
                valid = True
        if not valid:
            raise LinkedInCollectionError("Canary HTTP scope/one-trigger/request limit violated")
        self.requests.append({"method": request.method, "endpoint": str(request.url)})

    def _client(self):
        client = super()._client()
        client.event_hooks["request"].append(self._guard)
        return client

    async def trigger(self, source, **kwargs):
        self.snapshot_id = await super().trigger(source, **kwargs)
        self.accepted_jobs = 1
        return self.snapshot_id

    async def download_records(self, snapshot_id):
        rows = await super().download_records(snapshot_id)
        self.metrics["returnedRecords"] = len(rows)
        ids, reposts = set(), set()
        start = self.specification["body"][0]["start_date"]
        end = self.specification["body"][0]["end_date"]
        for row in rows:
            if isinstance(row, dict) and (row.get("error") or row.get("error_code")):
                self.metrics["providerErrorRecords"] += 1
            try:
                posts = parse_posts([row], self.source)
            except (LinkedInCollectionError, TypeError, ValueError):
                posts = []
            if not posts:
                self.metrics["invalidRecords"] += 1
                continue
            post = posts[0]
            if not post.external_id or not re.fullmatch(r"[0-9]+", post.external_id):
                # The normal parser permits URL/string fallbacks. They are not
                # evidence of a verified canonical LinkedIn activity identifier.
                self.metrics["unidentifiedPostRecords"] += 1
                self.metrics["invalidRecords"] += 1
                continue
            ids.add(post.external_id)
            if (post.linkedin_data or {}).get("kind") == "REPOST":
                reposts.add(post.external_id)
            if not post.published_at:
                self.metrics["undatedRecords"] += 1
            elif not start <= post.published_at.astimezone(timezone.utc).date().isoformat() <= end:
                self.metrics["outOfWindowRecords"] += 1
        self.metrics["uniqueLinkedInPostIds"], self.metrics["reposts"] = len(ids), len(reposts)
        return rows

    def validate_records(self, records):
        if len(records) > 6:
            raise LinkedInCollectionError("Provider exceeded six requested records; account paused for billing review")
        if self.metrics["invalidRecords"] or self.metrics["providerErrorRecords"] or self.metrics["undatedRecords"]:
            raise LinkedInCollectionError("Invalid, failed or undated records require review; no canary ingestion or checkpoint advance")
        if self.metrics["outOfWindowRecords"]:
            raise LinkedInCollectionError("Out-of-window publication/repost dates require review; checkpoint retained")


async def store_canary_post(db, source, post):
    """Raw insertion into the private clone; no AI, enrichment or existing-row edits."""
    digest = hashlib.sha256(post.url.encode()).hexdigest()
    existing = await db.scalar(select(NewsItem.id).where(NewsItem.source_id == source.id,
        or_(NewsItem.external_id == post.external_id, NewsItem.url == post.url, NewsItem.content_hash == digest)).limit(1))
    if existing:
        return False
    db.add(NewsItem(source_id=source.id, external_id=post.external_id, url=post.url, title=post.title,
        original_text=post.original_text, published_at=post.published_at, image_url=post.image_url,
        content_hash=digest, linkedin_data=post.linkedin_data))
    await db.flush()
    return True


async def execute(db, source, args, report, now, wait):
    claim = BrightDataCanaryRun(approval_ref=args.approval_ref, source_id=source.id, started_at=now,
        credential_fingerprint=hashlib.sha256(settings.BRIGHTDATA_API_TOKEN.encode()).hexdigest(),
        report=json.loads(json.dumps({**report, "accountReference": args.account_ref,
            "exclusiveAccountEvidence": args.exclusive_account_ref, "state": "claimed",
            "liveApiCalls": None, "apiRequests": None, "triggerOutcome": "unconfirmed"}, default=str)))
    db.add(claim)
    try:
        await db.commit()  # A crash here consumes the approval, even before POST/reservation.
    except IntegrityError as exc:
        await db.rollback()
        raise CanaryError("This approval reference was already used; a restart cannot authorize another trigger") from exc
    adapter = CanaryAdapter(settings.BRIGHTDATA_API_TOKEN, report["requestConfiguration"], source)
    started = time.monotonic()
    reason = "canary:" + str(claim.id)
    result = await collect_linkedin(db, source, now, store_canary_post, reason=reason,
                                   adapter=adapter, validate_records=adapter.validate_records)
    execution = await db.scalar(select(BrightDataExecution).where(BrightDataExecution.trigger_reason == reason))
    while result in {"started", "pending"} and adapter.progress_reads < MAX_PROGRESS_READS:
        await wait(60)
        if time.monotonic() - started >= MONITOR_SECONDS:
            break
        result = await collect_linkedin(db, source, datetime.now(timezone.utc), store_canary_post,
                                       reason=reason, adapter=adapter, validate_records=adapter.validate_records)
    await db.refresh(source)
    if execution:
        await db.refresh(execution)
    await db.refresh(claim)
    stopped = result != "completed" or bool(source.linkedin_paused_reason) or (execution and execution.state != "completed")
    errors = [execution.api_errors] if execution and execution.api_errors else []
    if stopped:
        source.linkedin_paused_reason = source.linkedin_paused_reason or "Canary stopped; operator reconciliation is required before further requests"
        if execution and execution.state in {"reserved", "running", "retry"}:
            finish(execution, datetime.now(timezone.utc), "uncertain", execution.api_errors or source.linkedin_paused_reason)
        if not errors:
            errors.append(source.last_error or source.linkedin_paused_reason)
    report.update({**adapter.metrics, "state": "stopped" if stopped else "completed",
        "runId": str(claim.id), "approvalReference": args.approval_ref, "accountReference": args.account_ref,
        "exclusiveAccountEvidence": args.exclusive_account_ref, "apiRequests": len(adapter.requests),
        "liveApiCalls": len(adapter.requests), "scrapingTriggerAttempts": adapter.trigger_attempts,
        "scrapingJobsAccepted": adapter.accepted_jobs, "triggerOutcome": "accepted" if adapter.accepted_jobs else "unconfirmed",
        "requests": adapter.requests, "snapshotId": adapter.snapshot_id,
        "executionId": str(execution.id) if execution else None,
        "newPosts": execution.newly_saved_posts if execution else 0,
        "duplicatePosts": execution.duplicate_posts if execution else 0,
        "estimatedBillableUsage": execution.estimated_billable_usage if execution else 0,
        "providerBillableRecords": None, "providerBillingStatus": "UNRECONCILED",
        "durationMs": int((time.monotonic() - started) * 1000), "retryCount": 0, "errors": errors})
    report = json.loads(json.dumps(report, default=str).replace(settings.BRIGHTDATA_API_TOKEN, "[redacted]"))
    claim.completed_at, claim.report = datetime.now(timezone.utc), report
    await db.commit()
    return report


async def run(args, *, wait=asyncio.sleep):
    target = private_target()  # Reject production before making a database connection.
    async with SessionLocal() as db:
        if not args.live:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await verify_identity(db, target)
            source = await selected_source(db, args)
            return await preview(db, source, args, datetime.now(timezone.utc))
        await verify_identity(db, target)
        source = await selected_source(db, args)
        source_id = source.id
        selected_identity = (source_id, source.type, canonical_source_url(source.url))
        await db.rollback()
        # Same key as the normal collector, held on a dedicated connection across commits.
        from app.intelligence.linkedin_identity import lock_key, canonical_linkedin_url
        key = lock_key(canonical_linkedin_url(selected_identity[2]))
        # One account-wide canary at a time, even if two distinct sources were selected.
        account_key = int.from_bytes(hashlib.sha256(b"news-brightdata-canary").digest()[:8], "big", signed=True)
        async with engine.connect() as lock:
            if not await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": account_key}):
                raise CanaryError("Another canary owns the account lock")
            acquired = False
            try:
                acquired = bool(await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
                if not acquired:
                    raise CanaryError("Another collector owns the selected source lock")
                # Re-resolve the original selector after rollback/locking. Source
                # admin edits do not participate in collector advisory locks.
                source = await selected_source(db, args)
                if (source.id, source.type, canonical_source_url(source.url)) != selected_identity:
                    raise CanaryError("Selected source changed during authorization; run a new dry-run and review")
                if await db.scalar(select(BrightDataCanaryRun.id).where(BrightDataCanaryRun.approval_ref == args.approval_ref)):
                    raise CanaryError("This approval reference was already used")
                now = datetime.now(timezone.utc)
                report = await preview(db, source, args, now)
                if report["blockers"]:
                    raise CanaryError("; ".join(report["blockers"]))
                # Refuse credentials accidentally pasted into operator evidence fields.
                if any(settings.BRIGHTDATA_API_TOKEN in value for value in (args.approval_ref, args.account_ref, args.exclusive_account_ref)):
                    raise CanaryError("Credentials cannot be used as authorization references")
                return await execute(db, source, args, report, now, wait)
            finally:
                if acquired:
                    await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": account_key})


def render(report):
    values = [
        ("Selected LinkedIn source", f"{report['source']['name']} ({report['source']['id']})"),
        ("LinkedIn URL", report["source"]["url"]), ("Provider", report["provider"]), ("Dataset", report["dataset"]),
        ("Requested record limit", report["requestedRecordLimit"]), ("Fetch date range", f"{report['fetchDateRange']['start_date']} to {report['fetchDateRange']['end_date']}"),
        ("Last successful fetch", report["lastSuccessfulFetch"]), ("Current monthly reservation", report["currentMonthlyReservation"]),
        ("Remaining internal budget", report["remainingInternalBudget"]), ("Automatic scheduler", report["automaticScheduler"]),
        ("Live API calls", report["liveApiCalls"])]
    lines = ["| Parameter | Value |", "| --- | --- |"]
    lines += [f"| {key} | {str(value).replace('|', '/').replace(chr(10), ' ')} |" for key, value in values]
    lines += ["", "Exact request configuration (credentials omitted):", json.dumps(report["requestConfiguration"], default=str, indent=2),
              "", report["billingWarning"]]
    if report["blockers"]:
        lines += ["Preflight blockers:", *report["blockers"]]
    if report["mode"] == "live":
        lines += ["", json.dumps(report, default=str, indent=2)]
    return "\n".join(lines)


def main():
    args = parser().parse_args()
    try:
        report = asyncio.run(run(args))
        output = json.dumps(report, default=str, indent=2) if args.json else render(report)
        if settings.BRIGHTDATA_API_TOKEN:
            output = output.replace(settings.BRIGHTDATA_API_TOKEN, "[redacted]")
        print(output)
        return 0 if not report["blockers"] and report.get("state") != "stopped" else 2
    except CanaryError as exc:
        message = str(exc)
        if settings.BRIGHTDATA_API_TOKEN:
            message = message.replace(settings.BRIGHTDATA_API_TOKEN, "[redacted]")
        print(json.dumps({"error": message, "liveAuthorized": False}))
        return 2
    except Exception as exc:
        # No traceback/DSN/provider payload can disclose a secret in CLI output.
        print(json.dumps({"error": "Canary failed; retain approvals/reservations and review privately", "type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
