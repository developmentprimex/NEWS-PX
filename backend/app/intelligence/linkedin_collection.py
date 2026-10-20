"""Persistent snapshot lifecycle. No automatic retries of billable triggers."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from app.config import settings
from app.intelligence.credit_control import locked_account, reserve_execution, record_alerts, usage_totals, executions_for_budget
from app.intelligence.linkedin_adapter import BrightDataLinkedInAdapter, parse_posts
from app.intelligence.linkedin_schedule import is_due
from app.intelligence.models import BrightDataExecution

DAILY_REASON = "scheduled-day"


def daily_window(covered_through, now, *, first_attempt=None):
    """One closed UTC day; only a completed daily execution advances coverage.

    A first-time rollout starts with yesterday, without inferring coverage from
    legacy wall-clock watermarks. Once daily attempts exist, retain their anchor.
    """
    yesterday = now.astimezone(timezone.utc).date() - timedelta(days=1)
    day = covered_through + timedelta(days=1) if covered_through else first_attempt or yesterday
    return (day, day) if day <= yesterday else None


def incremental_start(last_success, now):
    # Retained for the explicitly controlled legacy canary; normal scheduled
    # discovery uses closed UTC days and the durable execution ledger instead.
    start = (last_success.date() - timedelta(days=1)) if last_success else now.date() - timedelta(days=1)
    return max(start, now.date() - timedelta(days=7))


def finish(execution, now, state, error=None):
    execution.state = state
    execution.completed_at = now
    execution.duration_ms = max(0, int((now - execution.started_at).total_seconds() * 1000))
    execution.api_errors = error


async def collect_linkedin(db, source, now, store_post, *, reason="scheduled", backfill=None,
                           adapter=None, validate_records=None, cycle_source_id=None, clock=None):
    now = clock() if clock else now
    facebook = source.type == 'FACEBOOK'
    record_limit = settings.NEWS_FACEBOOK_RECORD_LIMIT if facebook else settings.NEWS_LINKEDIN_RECORD_LIMIT
    parser = parse_posts
    if facebook:
        from app.intelligence.facebook_adapter import BrightDataFacebookAdapter, dataset_for, parse_posts as facebook_posts
        from app.intelligence.facebook_identity import source_approved
        from app.intelligence.collection_cycles import confirmed_activity_time
        if not settings.NEWS_FACEBOOK_FETCH_ENABLED or not source_approved(source):
            return 'paused'
        parser = facebook_posts
    source_id = source.id
    execution_id = None
    if reason == 'trial-poll' and not source.pending_execution_id:
        return 'pending'
    cycle_row = None
    if cycle_source_id:
        from app.intelligence.collection_cycles import CollectionCycleSource, CollectionCycle, TemporalWindow, provider_dates, confirmed_activity_time
        from app.intelligence.cycle_service import cycle_authorized, TERMINAL
        from app.intelligence.cycle_service import source_identity
        cycle_row = await db.get(CollectionCycleSource, cycle_source_id)
        cycle = await db.get(CollectionCycle, cycle_row.cycle_id) if cycle_row else None
        if (not cycle or cycle_row.source_id != source.id or cycle_row.type != source.type
                or source_identity(source) != cycle_row.canonical_url):
            return 'paused'
        if cycle_row.state in TERMINAL:
            return 'completed'
        if cycle_row.execution_id and source.pending_execution_id != cycle_row.execution_id:
            # A completed/uncertain execution is never replaced by a second POST.
            previous = await db.get(BrightDataExecution, cycle_row.execution_id)
            cycle_row.state = previous.state if previous and previous.state in TERMINAL else 'uncertain'
            if cycle_row.state == 'completed' and not cycle_row.coverage_confirmed:
                cycle_row.state = 'partial'
                cycle_row.coverage_warning = 'Completion has no proven cycle coverage; review the original execution.'
            await db.commit()
            return 'completed'
        if not cycle_row.execution_id and not await cycle_authorized(db, cycle, now):
            return 'paused'
        if source.pending_execution_id and not cycle_row.execution_id:
            return 'pending'
        prefix = 'scheduled-cycle' if cycle.phase.startswith('scheduled-') else 'trial'
        reason = f'{prefix}:{cycle.phase}:{cycle.id}'
    if (settings.NEWS_SOCIAL_SCHEDULE_ENABLED and not cycle_row and not backfill
            and not source.pending_execution_id and not reason.startswith("canary:")):
        return "paused"
    if not settings.BRIGHTDATA_API_TOKEN:
        return "unavailable"
    if not settings.NEWS_ENABLE_COLLECTION or not settings.NEWS_LINKEDIN_FETCH_ENABLED:
        return "paused"
    # Canary-only mode never grants permission to scheduled/legacy-style callers.
    if settings.NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED != reason.startswith("canary:"):
        return "paused"
    if source.linkedin_paused_reason:
        return "paused"
    # Recover a known snapshot even when the monthly budget blocks new triggers.
    # The account latch blocks all automatic provider traffic after credit errors.
    account = await locked_account(db)
    if account.paused_reason or not account.provider_active or account.provider_balance is None or account.provider_balance < 0:
        source.last_error = account.paused_reason or "Provider billing has not been verified or balance is insufficient."
        await db.commit()
        return "paused"
    await db.commit()
    adapter = adapter or (BrightDataFacebookAdapter(settings.BRIGHTDATA_API_TOKEN, dataset_for(source)) if facebook
                          else BrightDataLinkedInAdapter(settings.BRIGHTDATA_API_TOKEN, settings.BRIGHTDATA_LINKEDIN_POSTS_DATASET_ID))
    execution = await db.get(BrightDataExecution, source.pending_execution_id) if source.pending_execution_id else None
    execution_id = execution.id if execution else None
    if source.pending_snapshot_id and not execution:
        # Never download an unreserved legacy snapshot automatically.
        source.linkedin_paused_reason = "Legacy snapshot requires explicit billing reconciliation."
        await db.commit()
        return "paused"
    if execution and not source.pending_snapshot_id:
        source.linkedin_paused_reason = "Trigger outcome is uncertain. Reconcile the existing execution before resuming."
        finish(execution, now, "uncertain", source.linkedin_paused_reason)
        await db.commit()
        return "paused"
    try:
        if source.pending_snapshot_id:
            if now - execution.started_at > timedelta(days=1):
                source.linkedin_paused_reason = "Snapshot timed out. Reconcile before starting another fetch."
                finish(execution, now, "uncertain", source.linkedin_paused_reason)
                await db.commit()
                return "paused"
            if execution.state == "retry":
                execution.retry_count += 1
                execution.state = "running"
                await db.commit()
            progress = await adapter.progress(source.pending_snapshot_id)
            if progress in {"starting", "running"}:
                return "pending"
            if progress == "failed":
                finish(execution, now, "failed", "Provider snapshot failed; reservation retained.")
                source.pending_snapshot_id = source.pending_execution_id = None
                if facebook:
                    source.linkedin_paused_reason = 'Facebook snapshot failed. Reconcile the existing reservation before resuming.'
                source.last_error = execution.api_errors
                await db.commit()
                return "error"
            records = await adapter.download_records(source.pending_snapshot_id)
            execution.returned_records = len(records)
            execution.estimated_billable_usage = max(execution.record_limit, len(records))
            errors = [str(record.get("error") or record.get("error_code")) for record in records if isinstance(record, dict) and (record.get("error") or record.get("error_code"))]
            safe_errors = "; ".join(errors).replace(settings.BRIGHTDATA_API_TOKEN, "[redacted]")[:2000] or None
            execution.api_errors = safe_errors
            account = await locked_account(db)
            if len(records) > execution.record_limit:
                account.paused_reason = "Provider exceeded the reserved record limit. Verify billing before resuming."
            if safe_errors and any(hint in safe_errors.casefold() for hint in ("not active", "inactive", "balance", "credit", "payment")):
                account.provider_active = False
                account.paused_reason = safe_errors[:500]
            record_alerts(account, usage_totals(account, await executions_for_budget(db), now))
            # Raw usage persists even if parsing, analysis or storage subsequently fails.
            await db.commit()
            if validate_records:
                validate_records(records)
            # Error-only and partial snapshots must not advance the watermark.
            from app.intelligence.linkedin_adapter import LinkedInCollectionError
            try:
                posts = parser(records, source)
            except LinkedInCollectionError:
                posts = []
            invalid = len(records) - len(posts)
            uncertain_dates = any(not post.published_at or not execution.start_date <= post.published_at.date() <= execution.end_date for post in posts)
            if facebook:
                uncertain_dates = any(confirmed_activity_time(post) is None for post in posts)
                def in_window(post):
                    stamp=confirmed_activity_time(post)
                    if stamp is None:return True
                    if cycle_row:return cycle_row.window_start < stamp <= cycle_row.window_end
                    return execution.start_date <= stamp.date() <= execution.end_date
                posts_in_window = [post for post in posts if in_window(post)]
                execution.ignored_records += len(posts) - len(posts_in_window)
                posts = posts_in_window
            if invalid or uncertain_dates:
                execution.coverage_warning = "Snapshot contains invalid, unattributed or out-of-window activity; review coverage before resuming."
                source.linkedin_paused_reason = execution.coverage_warning
            for post in posts:
                # Storage deduplicates identities while refreshing observations.
                # A repeated row may contain activity missing from the first row.
                saved = await store_post(db, source, post)
                if saved:
                    execution.newly_saved_posts += 1
                else:
                    # Category exclusions are tracked separately from duplicates.
                    from app.intelligence.models import NewsItem
                    from sqlalchemy import or_
                    exists = await db.scalar(select(NewsItem.id).where(NewsItem.source_id == source.id,
                        or_(NewsItem.external_id == post.external_id, NewsItem.url == post.url)).limit(1))
                    if exists:
                        execution.duplicate_posts += 1
                    else:
                        execution.ignored_records += 1
            execution.ignored_records += invalid
            saturated = len(records) >= execution.record_limit
            if saturated:
                source.linkedin_paused_reason = "Record limit reached; completeness is unknown. Authorize a separate backfill or reconcile the window."
            if len(records) > execution.record_limit:
                account = await locked_account(db)
                account.paused_reason = "Provider exceeded the reserved record limit. Verify billing before resuming."
            if not errors and not saturated and not invalid and not uncertain_dates and not execution.trigger_reason.startswith("backfill:"):
                source.last_successful_fetch_at = execution.started_at
                source.last_checked_at = datetime.now(timezone.utc)
                source.last_new_posts = execution.newly_saved_posts
                source.empty_fetch_streak = 0 if execution.newly_saved_posts else source.empty_fetch_streak + 1
            source.last_error = source.linkedin_paused_reason or safe_errors or execution.coverage_warning
            source.pending_snapshot_id = source.pending_execution_id = None
            finish(execution, datetime.now(timezone.utc), "partial" if errors or saturated or invalid or uncertain_dates else "completed", safe_errors)
            if cycle_row:
                known_times = all(confirmed_activity_time(post) is not None for post in posts)
                cycle_row.coverage_confirmed = execution.state == 'completed' and known_times
                cycle_row.state = 'completed' if cycle_row.coverage_confirmed else 'partial'
                cycle_row.coverage_warning = source.last_error or (None if known_times else 'Activity time is unverified; interval coverage is incomplete.')
                cycle_row.completed_at = execution.completed_at
            account = await locked_account(db)
            record_alerts(account, usage_totals(account, await executions_for_budget(db), now))
            await db.commit()
            return "error" if errors else "completed"
        if not backfill and not cycle_row and not is_due(source, now):
            return "pending"
        execution_reason = reason
        if cycle_row:
            start, end = provider_dates(TemporalWindow(cycle_row.window_start, cycle_row.window_end, cycle_row.verified_start))
            limit = record_limit
        elif backfill:
            start, end, limit = backfill
        elif reason.startswith("scheduled"):
            covered_through, first_attempt = (await db.execute(select(
                func.max(case((BrightDataExecution.state == "completed", BrightDataExecution.end_date))),
                func.min(BrightDataExecution.start_date),
            ).where(BrightDataExecution.source_id == source.id,
                    BrightDataExecution.trigger_reason == DAILY_REASON))).one()
            window = daily_window(covered_through, now, first_attempt=first_attempt)
            if window is None:
                return "pending"
            start, end = window
            limit = record_limit
            execution_reason = DAILY_REASON
        else:
            start, end, limit = incremental_start(source.last_successful_fetch_at, now), now.date(), record_limit
        now = clock() if clock else now
        if cycle_row and not await cycle_authorized(db, cycle, now):
            return 'paused'
        execution = await reserve_execution(db, source, now, start, end,
            reason=execution_reason, limit=limit, cycle_source_id=cycle_source_id,
            **({'dataset_id': dataset_for(source)} if facebook else {}))
        if not execution:
            return "paused"
        execution_id = execution.id
        if cycle_row and not await cycle_authorized(db, cycle, clock() if clock else now):
            finish(execution, clock() if clock else now, 'cancelled', 'Trial expired before POST; conservative reservation retained.')
            cycle_row.state, cycle_row.coverage_warning = 'blocked', execution.api_errors
            source.pending_execution_id = None
            await db.commit()
            return 'paused'
        snapshot = await adapter.trigger(source, start_date=start.isoformat(), end_date=end.isoformat(), limit=limit)
        source.pending_snapshot_id = execution.snapshot_id = snapshot
        source.last_error = None
        execution.state = "running"
        if cycle_row:
            cycle_row.state = 'running'
        await db.commit()
        return "started"
    except Exception as exc:
        # Persist reservations and errors. A POST timeout is ambiguous, never retried.
        error = str(exc).replace(settings.BRIGHTDATA_API_TOKEN, "[redacted]")[:2000]
        await db.rollback()
        from app.intelligence.models import NewsSource
        source = await db.get(NewsSource, source_id)
        execution_id = execution_id or source.pending_execution_id
        execution = await db.get(BrightDataExecution, execution_id) if execution_id else None
        source.last_error = error[:500]
        if execution:
            if execution.api_errors:
                error = f"{execution.api_errors}\n{error}"[-8000:]
            if not execution.snapshot_id:
                source.linkedin_paused_reason = "Trigger failed or outcome uncertain. Reconcile before retrying."
                finish(execution, datetime.now(timezone.utc), "uncertain", error)
            else:
                execution.api_errors = error
                execution.state = "retry"
                if execution.retry_count >= 3:
                    source.linkedin_paused_reason = "Snapshot retrieval failed three times; reconcile before retrying."
                    finish(execution, datetime.now(timezone.utc), "uncertain", error)
        if getattr(exc, "pause_account", False):
            account = await locked_account(db)
            account.provider_active = False
            account.paused_reason = error[:500]
        await db.commit()
        return "error"
