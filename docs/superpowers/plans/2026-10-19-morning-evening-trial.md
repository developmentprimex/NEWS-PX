# Morning and Evening Collection Trial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run one local day of manual morning collection and a 21:00 collection followed by an interval-specific email, with attributable credit measurements.

**Architecture:** Persist collection phases, source windows, report claims and billing observations in PostgreSQL. Route manual and scheduled work through the existing identity locks, reservation ledger and snapshot lifecycle; independently persist reporting cutoffs and proven coverage. Keep the legacy daily collector available outside the trial and the legacy email credentials disabled throughout it.

**Tech Stack:** Existing FastAPI, async SQLAlchemy/PostgreSQL, APScheduler, Next.js/React/TypeScript, pytest and Node test runner. No new runtime dependencies.

**Spec:** [Approved design](../specs/2026-10-19-morning-evening-trial-design.md).

## Global Constraints

- One local trial date; timezone `Europe/Budapest`; evening cutoff 21:00; report wait deadline 21:30.
- Morning cutoff is server acceptance time, not download completion. Email interval is `(successful_source_cutoff, evening_cutoff]`; without morning success use the last proven source checkpoint.
- Fetch all active supported sources; email only existing **Email on** subscriptions to the existing privately configured recipient.
- Preserve limits: 6 records/request, 130 reserved records/UTC day, 4000 operational/month, 1000 reserve, 5000 ceiling. Retain all prior reservations and account/source latches.
- Failed, saturated or uncertain coverage never advances a checkpoint. No automatic retry of an ambiguous billable POST or SMTP send. Recovery redesign remains out of scope.
- Provider date filtering can repeat morning rows; local deduplication does not refund them. Repost time must be independently supported, never substituted with original-post time.
- Trial expires after its local date. Already-started snapshots and its single pending report may finish later; no next-day trial phase. Normal closed-day collection then resumes without inferring full-day coverage from trial phases.
- Existing production data and private credentials must remain intact. No provider POST or SMTP during development tests. Do not modify unrelated staged files in the original checkout.

## Review Focus

1. Source is paused, deleted or changes identity after a phase is accepted: do not fetch a different target or report the old target as checked (Task 3).
2. Morning snapshot is still running at 21:00: retain ownership, never replace its execution, then use only the checkpoint actually proven before starting evening work (Task 3).
3. Subscription is enabled or removed during collection: respect the original subscription boundary and recheck current authorization before sending (Task 4).
4. Process crashes after SMTP acceptance but before receipt commit: retain a durable `sending` claim as uncertain; do not resend automatically (Task 4).
5. Billing month rolls over or another account consumer runs: do not subtract incomparable totals or present account-wide usage as exact trial cost (Task 5).

## Files and Interfaces

Create focused backend modules under `backend/app/intelligence/`:

- `collection_cycles.py`: ORM models and immutable window/status types.
- `cycle_service.py`: durable acceptance, checkpoints, source dispatch and expiry.
- `cycle_digest.py`: strict activity selection, report content and durable delivery claim.
- `trial_usage.py`: append-only billing observations and provisional/final cost summaries.
- `collection_trial_admin.py`: operator-only activation, status, usage import and stop commands; no web activation endpoint.

Integrate with `config.py`, `main.py`, `intelligence/router.py`, `collection.py`, `linkedin_collection.py`, `credit_control.py`, `linkedin_adapter.py`, `digest_service.py`, and `mail.py`. UI changes stay in the current intelligence feature, with a small `collection-check.ts` request helper. Add focused tests in `backend/tests/test_collection_cycles.py`, `test_collection_trial_workflow.py`, `test_collection_trial_digest.py`, `test_collection_trial_usage.py` and `frontend/test/collection-check.test.cjs`. Operational instructions live in `docs/morning-evening-trial-runbook.md`.

### Task 1: Persist phase identity, windows and expiry

**Files:** Create `backend/app/intelligence/collection_cycles.py`, `cycle_service.py`, `backend/tests/test_collection_cycles.py`; modify `backend/app/main.py` and `backend/tests/conftest.py`.

**Interfaces:** Produce `TemporalWindow(start: datetime, end: datetime, verified_start: bool)` and `CycleStatus` (TypedDict: existing state/total/processed/failed/started/pending fields plus cycle_id, phase, cutoff_at, partial, blocked). ORM models: `CollectionCycle`, `CollectionCycleSource`, `CollectionCycleReport`, `TrialUsageObservation`. Produce `trial_date_for_activation(now: datetime) -> date`, `trial_active(config: dict, now: datetime) -> bool`, `latest_verified_checkpoint(db, source_id: UUID) -> datetime | None`, `ensure_cycle(db, *, phase: str, now: datetime) -> CollectionCycle`, `cycle_status(db, cycle_id: UUID | None = None) -> CycleStatus`.

- [ ] **Step 1: Write failing PostgreSQL tests** using the existing disposable `database`/`private_postgres` fixtures, importing all new models before metadata creation. Assertions: concurrent `ensure_cycle` calls give one ID per `(trial_date, phase)`; restarting sessions retains cutoff; local activation at 20:59 uses today and at 21:00 uses tomorrow; both DST transitions resolve 21:00 correctly. A completed, verified source cycle yields its cutoff; partial/failed/uncertain cycles do not. A legacy completed `scheduled-day` yields midnight UTC after its end date; `last_started_at` and migrated legacy watermarks alone yield no proof.

  ```python
  def test_activation_date_uses_local_21_cutoff():
      assert trial_date_for_activation(datetime.fromisoformat("2026-10-19T20:59:00+02:00")) == date(2026, 10, 19)
      assert trial_date_for_activation(datetime.fromisoformat("2026-10-19T21:00:00+02:00")) == date(2026, 10, 20)
  ```
- [ ] **Step 2: Run** `python -m pytest -q tests/test_collection_cycles.py` from `backend`; expect failures for missing interfaces.
- [ ] **Step 3: Implement** the interfaces and additive tables. Cycle fields include UUID, local trial date, phase, cutoff, deadline, state and timestamps, with unique `(trial_date, phase)`. Manual cutoff is acceptance time; evening cutoff is exactly that date's local 21:00 even if a restarted worker accepts it at 21:05. Source rows have UUID, cycle/source identity, frozen name/type/canonical URL, start/end, verified-start flag, execution UUID, state, coverage warning and completion time, unique `(cycle_id, source_id)`. Report rows have unique `(trial_date, recipient_fingerprint)`, cycle/user identity, state, immutable selected item IDs/content and send timestamps; no recipient secret in status responses. Observations use Task 5's evidence fields. Retain history if a source is deleted; do not cascade-delete execution/report evidence. Use PostgreSQL insert-on-conflict for acceptance, not process-local locks.
- [ ] **Step 4: Implement** a versioned activation record in existing `AppMetadata` under `news_morning_evening_trial_v1`: selected local date, enabled flag, authorized user UUID, recipient fingerprint and approval reference; no credentials. Bootstrap without proven checkpoint uses the existing bounded first-collection horizon (yesterday UTC midnight), labels it unverified historical coverage, and never invents a previous success. Register models at startup; do not rewrite existing rows or subscription settings. Test defaults leave the trial disabled.
- [ ] **Step 5: Rerun** Task 1 tests; require no skips for PostgreSQL cases and no schema/data loss when startup runs twice against a legacy-shaped fixture. Commit only Task 1 files: `feat: persist one-day collection cycle windows`.

### Task 2: Connect explicit time windows to the protected LinkedIn lifecycle

**Files:** Modify `backend/app/intelligence/collection.py`, `linkedin_collection.py`, `credit_control.py`, `linkedin_adapter.py`; test in `backend/tests/test_collection_trial_workflow.py` and existing adapter tests.

**Interfaces:** Extend `protected_linkedin_check` and `collect_linkedin` with `cycle_source_id: UUID | None = None`; extend `reserve_execution` with the same optional argument. Existing callers remain unchanged. Produce `provider_dates(window: TemporalWindow) -> tuple[date, date]` and `confirmed_activity_time(item: NewsItem) -> datetime | None` in `collection_cycles.py`. Add `activityAt` and `activityTimeEvidence` metadata without changing the existing feed's `published_at` contract.

- [ ] **Step 1: Write failing tests** through mocked provider HTTP and real transactions: morning and evening windows can each reserve once despite the 1440-minute normal schedule; repeats reuse one execution; crash immediately after reservation commit recovers its linked execution without POST. Assert unknown/legacy repost time returns `None`, original author time never qualifies as repost time, valid post time is retained. Saturated six-row snapshots, failures and ambiguous triggers keep existing reservations/latches and do not advance cycle coverage.

  ```python
  def test_provider_dates_enclose_precise_utc_interval():
      window = TemporalWindow(datetime.fromisoformat("2026-10-19T08:00:00+00:00"),
                              datetime.fromisoformat("2026-10-19T21:00:00+00:00"), True)
      assert provider_dates(window) == (date(2026, 10, 18), date(2026, 10, 20))
  ```
- [ ] **Step 2: Run** `python -m pytest -q tests/test_collection_trial_workflow.py tests/test_intelligence_linkedin_adapter.py`; require the new cases to fail before implementation.
- [ ] **Step 3: Implement** phase validation under the existing source identity lock. Load the immutable source window from its row, reject mismatched/inactive/expired new work, use reasons `trial:manual:<cycle_uuid>` and `trial:evening:<cycle_uuid>`, and bypass normal due-time selection only for that validated phase. Atomically bind the reservation UUID to the source-cycle row in the same pre-POST transaction. Preserve all token/collection/canary/account/pending-job gates. Never use the general backfill argument to evade these checks.
- [ ] **Step 4: Implement** date-only request bounds containing the precise UTC interval: `window.start` converted to UTC date minus one day, `window.end` converted to UTC date plus one day. This conservative padding accommodates documented exclusive date boundaries; assert a window entirely on 2026-10-19 requests 2026-10-18 through 2026-10-20. Retain actual requested dates in the ledger and filter precise activity time locally. Extra returned rows can consume the cap; do not present the padded request as hourly provider filtering. Store duplicate observations and out-of-precise-window feed items normally, account for all returned rows, and leave cap-six saturation protection intact. Metadata for unsupported repost time is explicitly unknown; add only provider fields backed by schema/actual evidence, not guessed field names. Cycle coverage additionally requires confirmed activity times; it does not borrow a legacy execution's optimistic timestamp.
- [ ] **Step 5: Run** Task 2 tests plus `tests/test_brightdata_postgres_integration.py tests/test_linkedin_source_identity.py tests/test_linkedin_daily_window.py`; require all to pass without new external network access. Commit: `feat: collect protected LinkedIn trial windows`.

### Task 3: Coordinate manual acceptance, evening work and scheduler ownership

**Files:** Modify `backend/app/intelligence/cycle_service.py`, `router.py`, `collection.py`, `backend/app/main.py`; extend `backend/tests/test_collection_trial_workflow.py`.

**Interfaces:** Produce `accept_manual_cycle(db, *, user_id: UUID, now: datetime) -> CycleStatus`, `run_collection_trial_cycle(now: datetime | None = None) -> None`, `trial_owns_source(db, source_id: UUID, now: datetime) -> bool`, `dispatch_cycle_source(db, row_id: UUID, now: datetime) -> str`. The existing POST and status GET return `CycleStatus` during the trial and preserve legacy behavior outside it.

- [ ] **Step 1: Write failing route/worker tests**: admin POST includes active supported RSS, websites and LinkedIn; non-admin cannot spend; two API sessions and two workers still yield one POST/source/phase. At 21:00 without manual work, use each source's last proven checkpoint. Restart resumes snapshots. Test the Review Focus 1/2 races: source identity edit/deletion/pause blocks that frozen target; unfinished morning work is polled without substitution, then evening uses actual success or the prior checkpoint. Expiry forbids new trial phases but permits an existing report/snapshot to finish.

  ```python
  # test_two_manual_requests_and_worker_restart_reuse_execution:
  assert first_status["cycle_id"] == repeated_status["cycle_id"]
  assert len(provider.triggers) == len(linkedin_sources)
  assert len(executions_after_restart) == len(linkedin_sources)
  ```
- [ ] **Step 2: Run** `python -m pytest -q tests/test_collection_trial_workflow.py`; confirm new route/scheduler cases fail.
- [ ] **Step 3: Implement** acceptance that freezes the active supported inventory at phase creation. The main button uses the complete inventory; existing optional source-filter parameters cannot create separate paid phases. A repeated click attaches the existing manual phase even after completion; after 21:00 it returns the evening phase instead of starting a late manual one. Preserve old filtered non-LinkedIn behavior outside the trial. Each row freezes identity and explicit interval; RSS/website collection reuses `check_source(force=True)` and advances its cycle checkpoint only on actual completion, not `pending`.
- [ ] **Step 4: Implement** a one-minute scheduler entry and a dedicated PostgreSQL session advisory lock held across worker commits. Worker starts the evening phase at local 21:00, resumes durable rows, links known earlier snapshots without relabelling them, and leaves blocked/error rows visible. Existing scheduled collection skips sources owned by an active trial or its unresolved snapshot; it cannot race in a `scheduled-day` POST. At expiry release ownership only when pending snapshots are safely accounted for. Do not mark the partially processed trial date complete in the old daily ledger.
- [ ] **Step 5: Run** Task 3 and existing source workflow/refresh tests; assert status `finished` means terminal source rows, not merely successful trigger submission, and partial/blocked counts are distinct from zero updates. Commit: `feat: coordinate morning and evening source checks`.

### Task 4: Send the strict evening report once after collection

**Files:** Create `backend/app/intelligence/cycle_digest.py`; modify `cycle_service.py`, `digest_service.py`, `backend/app/config.py`, `backend/app/mail.py`; create `backend/tests/test_collection_trial_digest.py`.

**Interfaces:** Produce `trial_digest_entries(db, cycle_id: UUID, user_id: UUID) -> list[dict]`, `trial_digest_content(entries: list[dict], source_windows: list[dict], cutoff: datetime) -> tuple[str, str, str]`, `process_trial_digest(db, cycle_id: UUID, now: datetime) -> int`. Extend `GmailService.__init__(*, credentials: tuple[str, str] | None = None)`; existing calls retain existing credentials. New settings `NEWS_TRIAL_EMAIL_USER` and `NEWS_TRIAL_EMAIL_PASSWORD` default empty, privately supplied only to the trial sender. Legacy `EMAIL_USER`/`EMAIL_PASSWORD` remain blank in the production launcher.

- [ ] **Step 1: Write failing selection tests** with exact boundaries: activity at 08:00 excluded, at 08:00:01 and 21:00 included, at 21:00:01 excluded; activity published by 21:00 and persisted at 21:02 included; old newly downloaded articles and unknown-time reposts excluded. Include current subscriptions, removed subscriptions, another user's items, subscription-enabled boundaries and legacy delivery receipts. Assert per-source fallback intervals and unknown/incomplete source labels in text and escaped HTML.

  ```python
  # test_evening_selection_uses_activity_time_not_insert_time:
  assert {entry["item"].title for entry in entries} == {"Afternoon post", "At 21:00, stored 21:02"}
  assert "Morning post" not in body
  assert "Unknown repost time" not in {entry["item"].title for entry in entries}
  ```
- [ ] **Step 2: Write failing delivery tests**: before collection completion no SMTP; completion at 21:02 sends once; deadline at 21:30 sends available results with pending labels; two workers send once. Crash after accepted SMTP before DB acknowledgement leaves `sending`/uncertain and never retries automatically. Restart after midnight may finish the existing report but cannot create another day's report. Legacy 12:30/17:00 and expired trial sends stay disabled when only trial credentials are present.
- [ ] **Step 3: Run** `python -m pytest -q tests/test_collection_trial_digest.py`; confirm failures for missing interfaces.
- [ ] **Step 4: Implement** strict `(row.window_start, row.window_end]` activity predicates with no `created_at <= 21:00` exclusion and no unknown-date fallback. For newly enabled subscriptions require activity no earlier than their enabled time; recheck subscription and recipient fingerprint immediately before claiming a send. Reuse activity delivery keys/legacy receipt compatibility from the existing service. Freeze selected item IDs and rendered report, commit `sending` before SMTP, then commit receipts and `sent` after acknowledged success. Treat crash/exception after claim conservatively as uncertain with a redacted status; no blind retry. No sender runs if private trial credentials or approved recipient are missing.
- [ ] **Step 5: Implement** the dedicated trial report renderer using existing escaping/URL helpers, but remove legacy copy promising later inclusion of old activity. Show per-source interval, missing timestamp counts and collection status; successful send never advances failed coverage. Sender checks the one report identity and due/completion deadline. Run Task 4 plus `tests/test_intelligence_digest.py tests/test_intelligence_digest_interval.py`; preserve legacy tests. Commit: `feat: send one interval-specific evening trial digest`.

### Task 5: Measure credits and expose operator-only trial control

**Files:** Create `backend/app/intelligence/trial_usage.py`, `backend/app/collection_trial_admin.py`, `backend/tests/test_collection_trial_usage.py`; modify `cycle_service.py` and `backend/app/config.py`.

**Interfaces:** Produce `record_usage_observation(db, *, label: str, evidence: dict, now: datetime) -> UUID`, `capture_trial_usage(db, *, label: str, now: datetime) -> UUID`, `trial_usage_summary(db, trial_date: date) -> dict` in `trial_usage.py`. CLI: `python -m app.collection_trial_admin activate --approval-ref <ref>`, `status`, `usage-import --label <label> --file <private-path>`, `stop`. Status/import/activation do not trigger provider scraping or send mail; activation performs GET-only billing verification.

Summary keys are `postCount`, `reservedRecords`, `returnedRecords`, `duplicateRecords`, `newItems`, `providerDeltaRecords` (nullable), `provisional`, `attribution` (`isolated`/`uncertain`/`incomparable`), `providerMonth` and `usageAsOf`; no token or recipient details.

- [ ] **Step 1: Write failing tests**: eight sources/two phases reserve 96, twenty require 240 and cannot fit 130; pre-existing usage reduces headroom. Stale/missing billing evidence blocks activation. Mocked billing HTTP sees only GET `/status`, `/customer/balance`, `/pricing_plans`, `/customer/bw`; account/key/plan/meter mismatch fails closed. Same-month provider delta is separate from reservations/returned/new/duplicate counts; stale rollup remains provisional; month changes return incomparable instead of a negative delta; unisolated account consumers mark attribution uncertain. Secrets in evidence are rejected/redacted, never echoed. Activation and stop are idempotent; stop does not erase outstanding reservations or in-flight ownership.

  ```python
  # test_delayed_provider_rollup_is_not_zero_cost:
  assert summary["postCount"] == 16
  assert summary["reservedRecords"] == 96
  assert summary["provisional"] is True
  assert all(request.method == "GET" for request in billing_requests)
  ```
- [ ] **Step 2: Run** `python -m pytest -q tests/test_collection_trial_usage.py`; verify failures before implementation.
- [ ] **Step 3: Implement** append-only observations with trial date, label, observation UTC time, provider month/records/usage cutoff/verification time, balance, bound account ID and credential fingerprint, isolation status and execution-counter snapshot. Labels: `baseline`, `before_manual`, `after_manual`, `before_evening`, `after_evening`, `settled`. Extract the reviewed GET-only parsing and request logic from the original workspace's ignored `tmp/linkedin_account_billing.py` into `trial_usage.py`, retaining tests for daily/current-month totals and unsupported/shared meters. Use the existing server token and a read-only private binding file selected by new optional setting `NEWS_TRIAL_BILLING_BINDING_FILE`; never commit either value. Reuse `brightdata_admin.parse_evidence` validation and import monotonically under the account lock without clearing latches. Worker calls `capture_trial_usage` before the first phase trigger and after terminal phase completion/deadline; failed reads prevent new spending or leave the ending measurement explicitly provisional. Keep the hourly observer unchanged for delayed settlement. Finality requires a provider cutoff later than all trial execution completions and accounted-for other consumers; otherwise report provisional/incomplete, not zero.
- [ ] **Step 4: Implement** activation under a durable lock: derive local trial date, verify current inventory/budget/credential binding, SMTP recipient and existing enabled subscriptions, store baseline and versioned activation. Report full-inventory headroom honestly; individual reservations still recheck budget under account lock. Refuse activation if no required email configuration/subscriptions rather than silently changing them. No default account IDs, keys, approvals or live activation in startup/migrations.
- [ ] **Step 5: Run** usage tests plus `tests/test_brightdata_credit_control.py` and existing CLI tests; require unchanged limits and permanent old ledger entries. Commit: `feat: record trial credit evidence and operator controls`.

### Task 6: Wire the live button and verify the complete offline flow

**Files:** Create `frontend/src/features/intelligence/collection-check.ts`, `frontend/test/collection-check.test.cjs`; modify `frontend/src/features/intelligence/components/intelligence-page.tsx`, related status copy/types, and `.github/workflows/deploy.yml` only to include the new Node test. Extend `backend/tests/test_collection_trial_workflow.py`.

**Interfaces:** Export `ApiFetch = (path: string, init?: RequestInit) => Promise<Response>` and `startCollectionCheck(apiFetch: ApiFetch): Promise<SourceCheckStatus>` in the new helper, with `SourceCheckStatus` matching Task 1; `refresh feed` remains GET-only. New status fields are nullable/optional for legacy responses; retain `idle`/`running`/`finished` UI state values.

- [ ] **Step 1: Write failing behavioral tests**: helper POSTs `/intelligence/sources/check-all`, accepts existing phase/status, and propagates 403/409 errors; refresh invokes no collection POST. Add an end-to-end offline backend scenario: manual at 08:00, morning completion, one new afternoon post/repost/RSS entry, evening at 21:00, delayed download at 21:02, one mocked SMTP report containing only eligible afternoon activity, repeat/restart causes no new POST or email. Also exercise no morning, budget denial, saturation and failed source labels.

  ```javascript
  // Check for updates starts collection and returns the accepted phase:
  assert.deepEqual(requests, [{path: '/intelligence/sources/check-all', method: 'POST'}]);
  assert.equal(result.cycle_id, acceptedCycleId);
  ```
- [ ] **Step 2: Run** `node --test test/collection-check.test.cjs` from `frontend` and `python -m pytest -q tests/test_collection_trial_workflow.py` from `backend`; require new cases to fail before wiring.
- [ ] **Step 3: Implement** the helper and remove the `activityView` read-only shortcut from `checkForUpdates`. Reuse progress polling with durable cycle IDs, load status on page mount so a restart/navigation does not hide ongoing work, and render partial/blocked completion accurately. Keep viewing/filtering existing posts and the separate Refresh feed action unchanged. No product copy about internal guards, reservation implementation or credentials; messages explain check progress and incomplete results.
- [ ] **Step 4: Run** the complete backend suite (`python -m pytest -q tests`), frontend Node tests (`node --test test/linkedin-monitoring.test.cjs test/collection-check.test.cjs`), TypeScript (`node node_modules/typescript/bin/tsc --noEmit`), build (`npm.cmd run build`) and `git diff --check`. Require all pass, no skipped PostgreSQL coverage, provider/SMTP network forbidden. Smoke-check both buttons against the local mocked/offline environment; do not trigger live collection for UI verification.
- [ ] **Step 5: Commit** `feat: connect update button to durable trial progress`; record exact test counts and commands. Request one whole-branch review before release using the execution method selected by the user, resolve material findings, and rerun only affected checks plus release-required checks.

### Task 7: Prepare, activate and observe the one-day production trial

**Files:** Create `docs/morning-evening-trial-runbook.md`; operational helpers and private evidence remain in ignored `tmp/` and ACL-restricted server directories. Modify neither unrelated checkout changes nor general deployment safeguards to bypass their gates.

- [ ] **Step 1: Write the runbook** with exact tested SHA, commands from Task 5, source/subscription inventory, local trial date/21:00 slot, GET-only billing captures, report deadline, rollback and expiry verification. Preserve `NewsIntelligenceSync` disabled, the hourly billing observer, unrelated apps and private launcher manifests. Review any automation guard changes against the actual current server; never assume the checked-in `start-api.cmd` is the live guarded launcher.
- [ ] **Step 2: Complete existing release checks** and immutable CI approval requirements. Prepare a fresh private PostgreSQL dump and restore rehearsal; capture sources/items/analyses/user states/subscriptions/ledger/account state. Install the tested candidate paused, verify additive schema twice and data preservation, update only the reviewed backend manifest, and confirm public health/login/feed before activation. Obtain only any release approval still explicitly required at that point, with a concrete candidate and evidence ready.
- [ ] **Step 3: Stage scoped private email configuration** from the already configured private sender into the new trial-only fields, keep legacy email fields and AI blank, and verify configured recipient/subscriptions without printing secrets. If actual SMTP credentials or billing access are missing, report that concrete blocker; do not run a separate unsolicited email or provider test. Record baseline GET-only billing evidence and remaining budget, then activate once for the selected local date.
- [ ] **Step 4: Observe** the user's manual check without clicking a second paid phase. Record fresh GET-only usage before/after each phase through the existing bound observer and import it under Task 5 labels. At 21:00 verify one evening phase, all execution IDs, report readiness/deadline, one delivery state and no legacy digest. A deferred failure/cap-six case remains visible under existing safeguards; do not authorize recovery automatically.
- [ ] **Step 5: Verify expiry and cost**: no next-day trial phase/send, existing snapshot polling/report completion allowed, normal daily policy resumes without false full-day checkpoint. Reconcile provider evidence after its cutoff catches up. Present POST count, reserved records, returned/duplicate/new counts and provider-reported delta, distinguishing provisional/unattributable values. Do not claim a complete day's credit result before the trial finishes; retain observations and give the user the evidence needed to decide whether to continue.

## Self-review and Execution Handoff

Coverage: Tasks 1–3 cover accepted cutoffs, fallback, restarts, ownership and existing collection guards; Task 4 covers strict subscribed-source email after collection; Task 5 covers bounded activation and credit evidence; Task 6 covers the user's button and regression checks; Task 7 covers deployment, one-day observation and expiry. The five Review Focus cases are pinned to the owning test steps. No product code or live trial action is authorized by this document alone.

Recommended execution method: **Native**, with the same agent implementing tasks in this session and one fresh whole-branch reviewer after tests. The tasks share phase/window/ledger interfaces and are mostly sequential; per-task implementer/reviewer agents would add context cost without enabling much parallel progress. User review of this plan and execution-method selection precede implementation.
