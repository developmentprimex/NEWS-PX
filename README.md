# News Intelligence

This folder is a standalone application. It has its own Next.js frontend, FastAPI backend, PostgreSQL database, login, source polling, and optional OpenAI, Bright Data, and SMTP integrations. It does not call PrimeFlow at runtime.

## Email reports

For once-daily social collection, adaptive source limits and verified reservation
settlement, see [daily collection](docs/daily-collection.md). That mode collects a
closed UTC day once at 00:05 UTC while keeping the 07:00/21:00 email reports.

Each source has an Email on/off choice. With `NEWS_SOCIAL_SCHEDULE_ENABLED=true` (the default), LinkedIn and approved Facebook sources are checked in two daily cycles at **07:00 and 21:00 Europe/Budapest**, including daylight-saving changes. Website/RSS collection continues at each source's interval. Manual checks and ordinary scans cannot start extra social fetches. A delayed scheduler may start a slot within 30 minutes; missed slots are not replayed. Pending snapshots can still be retrieved after that window without starting replacement jobs.

After collection finishes, a combined report includes previously undelivered updates from subscribed active sources, including periodic News/website/RSS updates. Reports wait up to 30 minutes for pending social results. No email is sent without new updates; empty categories, idle sources and collection diagnostics are omitted. Late results can enter the next report. Delivery claims and item receipts survive restarts; uncertain SMTP outcomes block automatic resending and subsequent reports for that recipient/user until verified. Collection continues under the existing account, daily/monthly, Facebook pilot and source-cap protections; these may defer sources and never guarantee complete coverage. Source inventory is frozen separately for each slot, so newly added sources enter the next slot.

Recurring email uses `EMAIL_USER` / `EMAIL_PASSWORD` and `NEWS_EMAIL_RECIPIENT`; one-day trial credentials do not grant recurring email access. Email settings display the fixed report times. Setting `NEWS_SOCIAL_SCHEDULE_ENABLED=false` restores the legacy daily/trial schedule and editable email times. Application startup creates the additive schedule-report table without changing historical delivery or billing records.

An owner-authorized `NEWS_COLLECTION_EXCEPTION_DATE=YYYY-MM-DD` enables a single evening experiment on that local date. Only its 21:00 cycle can exceed the ordinary daily and Facebook sublimits, with up to 80 returned records reserved per social source. The shared monthly ceiling, 1,000-record reserve, billing/account checks, identity checks and source pauses remain enforced; only previous record-cap saturation may retry for this specific cycle, retaining its pause history. The experiment requests the last 07:00–21:00 interval and also checks active website/RSS sources. New-request permission expires at 21:30; tomorrow's ordinary limits resume without a restart. Conservative reservations remain in the ledger. Provider usage observations before and after the cycle are stored separately in application metadata, refreshed through noon tomorrow, and labelled provisional while the provider rollup has not covered completed executions. Unsupported sources, unverifiable repost times and saturated results still prevent a claim of complete coverage.

The experiment's email uses the exact `(07:00, 21:00]` activity window for every source type, even when the previous report checkpoint is older or results are inserted late. Undated or unverified activity remains excluded from this report. Ordinary scheduled reports retain their existing late-result handling.

When the existing private `NEWS_TRIAL_BILLING_BINDING_FILE` is configured, each social cycle refreshes verified shared-account billing with read-only requests before starting new jobs. A failed refresh blocks new requests while permitting pending-result retrieval. Without that binding, fresh operator-imported billing evidence is still required and expires after 24 hours. Facebook retains its explicit dataset and approved-source binding requirements.

## What was moved

- News feed, Overview, Opportunities, Saved, and admin Sources pages
- KIESA and EU Digital website collectors, generic public RSS/Atom collector, and Bright Data LinkedIn post collector
- Server-side AI analysis, source intervals, priority scoring, read and saved state, and optional email sharing
- A snapshot of **77 collected articles and analyses** from PrimeFlow on 25 September 2026, plus **6 source records** in `data/primeflow-export.json`

The five official source URLs and guidance were reconstructed from PrimeFlow's seed migrations. The LinkedIn profile URL came from the source previously provided by the user. Source settings that are not exposed by the feed API were reconstructed with normal priority and a 60 minute interval. The old app stored bookmarks in each browser's local storage; those browser-specific bookmarks and other users' read/email state are not in the export. Sources with no collected articles may also be absent. The new app stores new bookmarks and reading state in its own database.

## Run locally or after copying the folder

Copy the whole `news/` folder, including its hidden `.env` file. From inside it, run `docker compose up -d --build`, then open <http://localhost:3101>. Sign in with `NEWS_ADMIN_EMAIL` and `NEWS_ADMIN_PASSWORD` from `news/.env`. The current local copy already has generated credentials; a new copy made from Git needs `.env.example` copied to `.env` with private values filled in once.

Docker starts the database, API, and web app. On a fresh database, PostgreSQL restores `data/01-current.sql` if present. Otherwise, the API imports the 77-article snapshot from `data/primeflow-export.json` once. Subsequent restarts preserve the database volume. The app does not need PrimeFlow, Python, or Node installed on the destination; Docker is enough.

## Production configuration

Run one backend worker because the five-minute scheduler lives in the API process. Use a dedicated PostgreSQL database, set `NEWS_COOKIE_SECURE=true` behind HTTPS, and set `NEWS_PUBLIC_ORIGIN` to the public frontend origin. The Docker build points the frontend's `/api` proxy at the bundled API. Keep `.env` out of source control; it is the single private file copied with the folder.

`OPENAI_API_KEY` enables structured article analysis. `BRIGHTDATA_API_TOKEN` enables LinkedIn post checks. Website and RSS collection work without those credentials, with a basic fallback analysis when OpenAI is absent. `EMAIL_USER` and `EMAIL_PASSWORD` enable email delivery; set `NEWS_EMAIL_RECIPIENT` to the intended destination. The service checks Gmail SMTP port 587 and uses TLS port 465 if 587 cannot be reached. Email delivery still depends on outbound SMTP access from the new host.

With OpenAI configured, the Overview page can generate a Strategic Brief from the latest saved analyses. Each live article also has a Deep analysis action with key points, business impact, suggested next steps, and questions to verify. Both actions run only when clicked and cache their results in the database; the brief updates when the set of recent articles changes or on a new UTC day. Existing imported articles can use these actions without being recollected. The AI output is based on saved source material, so verify consequential details at the original link.

The company focus is a Kosovo business offering AI solutions. New AI analyses prioritize actionable grants and tenders, especially Kosovo calls, and explicitly flag unknown eligibility. Overview shows all updates ordered by publication date, newest first. The For You page keeps the personalized selection and promotes relevant current grants and tenders ahead of broad policy news. High priority filters updates by their assigned priority. Expired calls and beneficiary/result lists are not promoted as application opportunities. The Strategic Brief follows the same focus and regenerates when its prompt version changes.

`NEWS_ENABLE_COLLECTION=true` enables collection. A five-minute scan checks due website/RSS sources; a separate one-minute coordinator finds the 07:00/21:00 social slots and retrieves pending snapshots. `NEWS_LINKEDIN_FETCH_ENABLED=false` and `NEWS_FACEBOOK_FETCH_ENABLED=false` remain safe defaults: social collection additionally requires verified provider billing evidence, available allowance and an active account. Website/RSS intervals are unchanged. Set `NEWS_ENABLE_COLLECTION=false` to disable collection while reviewing stored content. See [release readiness and approval gates](docs/release-readiness.md).

Website collection keeps dedicated KIESA and EU Digital extractors and now also attempts public websites without RSS through a generic collector. The generic collector discovers same-site article links, advertised RSS/Atom feeds, and sitemap entries; it reads HTML and text PDFs. If the initial page has no useful links, Chromium renders JavaScript as a fallback. Transient HTTP 500/502/503/504 reads receive one retry after a short delay. Access denials and certificate failures remain visible and require an accessible official feed or action by the website owner; checks do not bypass those restrictions. Generic collection failures update the stored source error and attempt time. The Sources and LinkedIn activity pages distinguish daily/monthly allowance deferrals from failed checks; existing allowance and billing controls remain in effect. Select source categories such as Grants to keep unrelated new items out of the feed. Facebook pages and arbitrary APIs still need a connector. LinkedIn needs the Bright Data token and public post availability depends on the provider.

## Moving to another repository or server

Before copying to another computer, run `./prepare-copy.ps1` in PowerShell from this folder. It saves the full current database to `data/01-current.sql` and stops the app. Copy the entire `news/` folder, including `.env` and `data/01-current.sql`, then run `docker compose up -d --build` at the destination. The database restore includes collected articles, sources, read state, and saved items. Restart the original copy with `docker compose up -d` if needed. No PrimeFlow deployment or database connection is needed. The local PrimeFlow checkout has had its News routes, navigation item, worker jobs, and module code removed. Historical PrimeFlow migrations and the original live tables remain for data safety until a separate verified cleanup is authorized and deployed.

## LinkedIn activity

LinkedIn cards show available likes/reactions, comment and repost/share counts, a preview of public comments, and shared-post content and attribution when Bright Data returns them. Missing counts remain unknown. Comment previews are partial and do not include comments the source account makes across other people's posts. Plain repost discovery depends on provider coverage; the app cannot promise a complete activity feed.

Scheduled LinkedIn updates use the last confirmed cycle checkpoint and a six-record limit. A source's first cycle starts at the previous 07:00/21:00 slot. Provider date filters enclose the precise cycle interval, so returned duplicates may consume allowance. Social email entries require a confirmed activity timestamp. Web requests read stored posts and cannot trigger or poll scrapes. Historical backfills require a separate operator command and an approval reference. Stored posts, analyses and read/saved state remain available when fetching is paused. Saturated result windows pause for reconciliation instead of silently skipping unseen posts. Existing installations add the collection ledger and source checkpoints at API startup.

## Repost watch

The dedicated LinkedIn activity page discovers every eligible personal profile and company source. Active sources enter the next 07:00/21:00 cycle, subject to account, ownership and budget checks. Paused sources retain stored history. Existing account bookmarks, group filters and read/saved preferences remain available. Posts and supported attributed reposts can be viewed for every source; provider coverage is not guaranteed. The page refreshes stored data every 30 seconds and never starts scraping.

The Repost watch page separates CEO (Sheet 1, 16 accounts) and Company (Sheet 2, 15 accounts) from the supplied repost workbook. The 14 shared accounts are collected once and appear in both groups. All tracked posts appear by publication date without a relevance threshold. Available interaction guidance from each sheet is shown on the post. The page and its unread sidebar badge refresh every 30 seconds; source collection follows the shared social schedule. Email subscriptions remain individually controlled for these imported accounts.

The curated, canonicalized source manifest is in `data/repost-watch-sources.json`, including worksheet/row provenance. Import it once, or rerun it safely, with `docker compose exec -T api python -m app.import_repost_watch /srv/news/data/repost-watch-sources.json`. Existing source records and email settings are preserved. The remaining workbook sheets are excluded from this import. Reposting is performed manually on LinkedIn.

Canonical LinkedIn URL validation rejects duplicate creation and edits, including concurrent submissions. Historical duplicate rows remain intact and share one canonical collection owner. Identity changes and deletion are blocked after collection history exists; pause the source to retain its posts, analyses, saved state and daily limit.

## Release safety

The local push guard blocks production refs by default. For an explicitly owner-authorized `main` update, set `NEWS_APPROVED_MAIN_SHA` to the reviewed commit and `NEWS_APPROVED_MAIN_BASE` to the freshly fetched remote `main` commit for that push only. Both must match the actual update, which must be a fast-forward. Tags, deletions and other production refs stay blocked. Push approval does not dispatch a production deployment or create a server release permit.

Development CI performs verification only. The production sync mechanism must remain held until its separately approved transition is complete. Production deployment and the first live provider request are separate approvals. See [the consolidated checklist](docs/release-readiness.md). Never commit private audit exports, temporary investigations, environment files or account evidence. Repository-local guards can be enabled with `git config --local core.hooksPath .githooks`.
