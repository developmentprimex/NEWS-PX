# One-day morning and evening collection trial

The trial is disabled until an operator explicitly activates it. Deployment alone does not trigger a trial, and tests use simulated provider/SMTP boundaries. Record the reviewed Git SHA, test output, fresh database dump hash and private release-evidence directory before changing production.

## Preserve the current installation

- Production root: `E:\websites\news-intelligence`; API loopback 8001; frontend 3101; public origin `https://news.primexeu.com`.
- Preserve API/Web boot and retry definitions, the hourly GET-only billing task and the disabled inert legacy sync task. Touch no unrelated service or application.
- Use the current ACL-restricted private launcher, its normalized backend manifest and the retained rollback configuration. The checked-in start script is not sufficient evidence of the actual launch context.
- Retain sources, posts, analyses, reading/saved state, subscriptions, old execution reservations, canary approvals and account/source pause latches. The four cycle/usage tables are additive. No production restore, ledger reset or inferred full-day coverage is part of this change.

## Verify and release the candidate

Run from `backend`: `python -m pytest -q tests`. PostgreSQL cases must run against the disposable test identity on loopback 55439, with no skipped integration coverage; external HTTP/SMTP is forbidden by the test fixture. Run from `frontend`: `node --test test/linkedin-monitoring.test.cjs test/collection-check.test.cjs`, `node node_modules/typescript/bin/tsc --noEmit`, `npm.cmd run build`. Also run existing deployment/rollback and Git guard checks required by the CI workflow.

Use an explicit feature-branch push with no tags/mirror options. Review the exact candidate and CI results before integrating into main. The release workflow records approval and runs checks; it does not itself deploy to the server. Apply the established exact-SHA release process and obtain only approvals that are still outstanding for this candidate.

Before installation, refresh News task/process/listener ownership and credential-consumer inventory. Hold News restart/legacy deployment paths as in the retained maintenance procedure. Take a fresh PG18 dump into the private release directory and restore it only into a new isolated target. Run candidate startup/schema creation twice there, comparing IDs, original-column hashes, counts, subscriptions, ledger/account state and orphan checks. Existing populated tables must be preserved; new trial tables start empty.

Install the tested immutable candidate paused and with trial SMTP credentials absent. Update the reviewed launcher manifest only after files match the tested version. Verify API health, public login/feed, all preserved data and single News runtime ownership; restore the reviewed boot/retry settings. Retain the independent paused rollback and both old private release directories used by the billing task/launcher.

## Private trial configuration

Keep the existing provider token and its account binding. Do not request another token, put it in commands, commit it or print dotenv values. Privately create a trial binding JSON alongside the retained binding with these reviewed fields:

- `accountId`, `credentialSha256`, `pricingPlanId`, `monthlyAllowance`: copied from the existing verified binding.
- `usageResource`: the exact dataset resource already verified by the installed billing observer, not a guessed dataset-to-zone conversion.
- `consumersIsolated`: true only if current inventory proves other consumers cannot share the measured account usage; otherwise false.

Set `NEWS_TRIAL_BILLING_BINDING_FILE` to that protected local file. Set `NEWS_TRIAL_EMAIL_USER`/`NEWS_TRIAL_EMAIL_PASSWORD` from the existing private SMTP configuration. Keep ordinary `EMAIL_USER`/`EMAIL_PASSWORD` and AI disabled in the application launcher. Trial credentials do not enable legacy 12:30/17:00 digests; the trial sender uses them explicitly. Do not send a separate test email or scrape as a configuration probe.

Verify the privately configured recipient and existing **Email on** subscriptions for the configured administrator. Activation refuses missing subscriptions or SMTP settings; it never silently subscribes additional sources. All active supported sources are collected for the feed, regardless of which accounts the current view filters.

## Activate and observe one day

Commands below run privately on the server from `backend`, with the guarded reviewed configuration loaded. They do not themselves POST a scraping request or send mail.

```powershell
..\.venv\Scripts\python.exe -m app.collection_trial_admin status
..\.venv\Scripts\python.exe -m app.collection_trial_admin activate --approval-ref owner-approved-one-day-trial
```

The activation command performs four billing GETs, verifies account/key/plan/meters, stores baseline evidence and checks full-inventory headroom. It selects today locally if before 21:00, otherwise tomorrow. Record the returned date; repeated activation for that date is idempotent. A different date requires review of the prior trial.

Limits remain 6 records/request, 130 reserved/UTC day, 4000 operational/month and 1000 reserve. Eight profiles and two phases can reserve 96 in addition to earlier use. Twenty require 240 and do not fit; do not increase limits to force activation. Each request rechecks the shared budget and existing latches under lock.

After activation, the user clicks **Check for updates** once. The cutoff is server acceptance time. Repeated clicks attach that phase. **Refresh feed** reads stored results and does not spend scraping credits. Capture the phase ID, cutoff, frozen source inventory, reservations and source states. The worker captures billing GETs before/after collection; failed billing reads prevent new spending or leave ending cost provisional.

At local 21:00 the worker starts the evening phase. Each source uses its successful morning checkpoint; without one it uses the last proven checkpoint. Missing historical proof is explicitly labelled and uses the bounded startup horizon. It never treats a click, a partial snapshot or a legacy migrated wall-clock watermark as successful coverage. An earlier pending snapshot is polled under its original execution, not replaced or retriggered.

The evening report waits until source work finishes, up to 21:30. It includes only confirmed activity time in each source's `(start, 21:00]` interval, including results stored after 21:00. Unknown repost times are excluded from the confirmed interval and counted as unknown in the feed. Failed/partial/pending sources are labelled. Provider date filtering may repeat morning rows and consume credits despite local deduplication; saturation at six retains existing safeguards and is not automatically backfilled.

The report is claimed durably before SMTP. A successful report is sent once for the date/recipient. A crash or uncertain SMTP outcome requires private verification; do not reset the claim or blindly resend. Missing configuration or changed recipient must be resolved without creating another paid phase.

## Cost evidence and expiry

```powershell
..\.venv\Scripts\python.exe -m app.collection_trial_admin status
```

Compare baseline, before/after manual, before/after evening and later settled observations. The existing hourly billing observer continues GET-only refreshes; the trial retains its newer bound evidence without starting another scrape.

Report confirmed snapshot acceptances (`postCount`) separately from ambiguous reservations (`uncertainPostCount`). Show reserved, returned, duplicate and newly stored records separately from the provider's account-wide usage delta and its month/cutoff. `provisional` remains true until the evening phase and provider evidence catch up. Other consumers, unresolved calls or a changed month/account make precise attribution uncertain/incomparable. No new feed cards does not imply zero credits.

At local midnight no new trial phase is allowed. Already-started snapshots and that day's single report may finish later. Normal closed-UTC-day collection resumes; it may revisit a date only partly touched by the trial, still within the budget and without a fabricated daily success marker. The two-phase schedule does not continue the next day automatically. Retain the evidence for the owner's continuation decision.

For an explicit early stop:

```powershell
..\.venv\Scripts\python.exe -m app.collection_trial_admin stop
```

Stopping retains reservations and known execution identities. Preserve account/source latches, investigate ambiguous outcomes, and use the independently reviewed paused rollback if the release itself must be reversed. Do not run a production database restore as rollback.
