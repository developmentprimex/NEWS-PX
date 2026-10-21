"""Operator-only billing import, reconciliation and explicitly approved backfills.

Billing imports make no network calls. Backfill is the ONLY command that can POST
to Bright Data; it requires an approval reference and the billable authorization flag.
"""
import argparse
import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.db import SessionLocal
from app.intelligence.collection import protected_linkedin_check
from app.intelligence.credit_control import locked_account, month_key, record_alerts, usage_totals, executions_for_budget
from app.intelligence.linkedin_adapter import SNAPSHOT_ID
from app.intelligence.models import BrightDataExecution, NewsSource


def parse_evidence(payload, now):
    as_of = datetime.fromisoformat(payload["usage_as_of"].replace("Z", "+00:00"))
    if as_of.tzinfo is None or as_of > now or payload["month"] != month_key(as_of):
        raise ValueError("Evidence needs a timezone-aware usage timestamp in its billing month")
    for field in ("billable_records", "verified_monthly_allowance"):
        if type(payload[field]) is not int or payload[field] < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
    if type(payload["active"]) is not bool or not str(payload["evidence"]).strip():
        raise ValueError("Active status and billing evidence are required")
    balance = Decimal(str(payload["balance"]))
    if not balance.is_finite():
        raise ValueError("Balance must be finite")
    return as_of, balance


async def run(args):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        if args.command == "billing-import":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            as_of, balance = parse_evidence(payload, now)
            account = await locked_account(db)
            if account.usage_as_of and as_of < account.usage_as_of:
                raise ValueError("Cannot replace billing evidence with an older snapshot")
            if (account.provider_month == payload["month"] and account.provider_billable_records is not None
                    and payload["billable_records"] < account.provider_billable_records):
                raise ValueError("Provider usage cannot decrease within a billing month")
            account.provider_month = payload["month"]
            account.provider_billable_records = payload["billable_records"]
            account.provider_balance = balance
            account.provider_active = payload["active"]
            account.verified_monthly_allowance = payload["verified_monthly_allowance"]
            account.usage_as_of, account.verified_at = as_of, now
            account.evidence = payload["evidence"]
            if payload.get('billing_scope') is not None:
                from app.intelligence.credit_control import verified_scope
                prior_scope = account.billing_scope or {}
                if prior_scope.get('account_ref') not in {None, payload.get('account_ref')}:
                    raise ValueError('Provider account changed; reconcile billing scope first.')
                account.billing_scope = payload['billing_scope']
                if account.billing_scope.get('complete') and not verified_scope(account, now):
                    raise ValueError('Billing dataset scope is inconsistent with account totals.')
            if not payload["active"] or balance < 0:
                account.paused_reason = "Provider billing evidence reports an inactive account or negative balance."
            if args.clear_pause:
                if not args.approval_ref or not payload["active"] or balance < 0:
                    raise ValueError("Clearing the account latch requires active billing evidence and --approval-ref")
                account.paused_reason = None
            record_alerts(account, usage_totals(account, await executions_for_budget(db), now))
            await db.commit()
            print("Billing evidence imported. Fetch configuration has not been changed.")
            return
        source = await db.scalar(select(NewsSource).where(NewsSource.id == args.source).with_for_update())
        if not source or source.type != "LINKEDIN":
            raise ValueError("LinkedIn source not found")
        if args.command == "reconcile":
            execution = await db.get(BrightDataExecution, source.pending_execution_id) if source.pending_execution_id else None
            if args.snapshot:
                if not execution or not SNAPSHOT_ID.fullmatch(args.snapshot):
                    raise ValueError("Attach requires an existing reserved execution and a valid snapshot ID")
                if execution.snapshot_id and execution.snapshot_id != args.snapshot:
                    raise ValueError("Cannot replace a known snapshot")
                source.pending_snapshot_id = execution.snapshot_id = args.snapshot
                execution.state = "running"
                execution.api_errors = f"Snapshot attached: {args.evidence}; approval: {args.approval_ref}"
            else:
                if execution:
                    execution.state = "reconciled"
                    execution.completed_at = now
                    execution.duration_ms = int((now - execution.started_at).total_seconds() * 1000)
                    execution.api_errors = f"Operator reconciliation: {args.evidence}; approval: {args.approval_ref}"
                source.pending_execution_id = source.pending_snapshot_id = None
            source.linkedin_paused_reason = source.last_error = None
            await db.commit()
            print("Source reconciled; prior usage reservation retained.")
            return
        if not args.authorize_billable or not args.approval_ref:
            raise ValueError("Backfill requires --authorize-billable and --approval-ref")
        if args.start > args.end or args.end > now.date() or not 1 <= args.max_records <= 130:
            raise ValueError("Invalid backfill dates or record limit")
        if source.pending_execution_id or source.pending_snapshot_id:
            raise ValueError("Reconcile the existing execution before backfilling")
        source.linkedin_paused_reason = None
        # All normal monthly, daily, account and lock safeguards also apply.
        result = await protected_linkedin_check(db, source, now,
            reason=f"backfill:{args.approval_ref}"[:200],
            backfill=(args.start, args.end, args.max_records))
        print(f"Backfill result: {result}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    billing = commands.add_parser("billing-import")
    billing.add_argument("file")
    billing.add_argument("--clear-pause", action="store_true")
    billing.add_argument("--approval-ref")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--source", type=UUID, required=True)
    reconcile.add_argument("--snapshot")
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--approval-ref", required=True)
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--source", type=UUID, required=True)
    backfill.add_argument("--start", type=date.fromisoformat, required=True)
    backfill.add_argument("--end", type=date.fromisoformat, required=True)
    backfill.add_argument("--max-records", type=int, default=6)
    backfill.add_argument("--authorize-billable", action="store_true")
    backfill.add_argument("--approval-ref", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
