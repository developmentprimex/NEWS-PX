# News Intelligence

This folder is a standalone application. It has its own Next.js frontend, FastAPI backend, PostgreSQL database, login, source polling, and optional OpenAI, Bright Data, and SMTP integrations. It does not call PrimeFlow at runtime.

## Email reports

Each news source has an Email on/off choice. Enabled sources are combined into one structured email at each configured report time (12:30, 17:00, and 21:00 Europe/Budapest by default). Each report includes only updates discovered after email was enabled and not already delivered; the same article appears once even if several sources contain it. Reports show only sources with new updates and omit zero-count categories and collection diagnostics. No email is sent when there are no new updates or no active email subscriptions. Report times are editable on the Email settings page. Delivery requires `EMAIL_USER` and `EMAIL_PASSWORD`, and the recipient is `NEWS_EMAIL_RECIPIENT`. Source checks run automatically when `NEWS_ENABLE_COLLECTION=true`; the report scheduler runs every minute when the API is running. A manual **Check now** action is also available. Report settings and delivery history are stored in PostgreSQL; fresh installations create their tables at startup.

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

`NEWS_ENABLE_COLLECTION=true` starts the scheduler's five-minute due-source scan. LinkedIn sources start with one automatic attempt per 24 hours, with longer intervals for inactive sources. `NEWS_LINKEDIN_FETCH_ENABLED=false` is the safe default: LinkedIn collection additionally requires verified provider billing evidence, available allowance and an active account. Website/RSS intervals are unchanged. Set `NEWS_ENABLE_COLLECTION=false` to disable all automatic collection while reviewing stored content. See [release readiness and approval gates](docs/release-readiness.md).

Website collection keeps dedicated KIESA and EU Digital extractors and now also attempts public websites without RSS through a generic collector. The generic collector discovers same-site article links, advertised RSS/Atom feeds, and sitemap entries; it reads HTML and text PDFs. If the initial page has no useful links, Chromium renders JavaScript as a fallback. Transient HTTP 500/502/503/504 reads receive one retry after a short delay. Access denials and certificate failures remain visible and require an accessible official feed or action by the website owner; checks do not bypass those restrictions. Generic collection failures update the stored source error and attempt time. The Sources and LinkedIn activity pages distinguish daily/monthly allowance deferrals from failed checks; existing allowance and billing controls remain in effect. Select source categories such as Grants to keep unrelated new items out of the feed. Facebook pages and arbitrary APIs still need a connector. LinkedIn needs the Bright Data token and public post availability depends on the provider.

## Moving to another repository or server

Before copying to another computer, run `./prepare-copy.ps1` in PowerShell from this folder. It saves the full current database to `data/01-current.sql` and stops the app. Copy the entire `news/` folder, including `.env` and `data/01-current.sql`, then run `docker compose up -d --build` at the destination. The database restore includes collected articles, sources, read state, and saved items. Restart the original copy with `docker compose up -d` if needed. No PrimeFlow deployment or database connection is needed. The local PrimeFlow checkout has had its News routes, navigation item, worker jobs, and module code removed. Historical PrimeFlow migrations and the original live tables remain for data safety until a separate verified cleanup is authorized and deployed.

## LinkedIn activity

LinkedIn cards show available likes/reactions, comment and repost/share counts, a preview of public comments, and shared-post content and attribution when Bright Data returns them. Missing counts remain unknown. Comment previews are partial and do not include comments the source account makes across other people's posts. Plain repost discovery depends on provider coverage; the app cannot promise a complete activity feed.

LinkedIn updates use the last successful fetch timestamp, a date-boundary overlap, and a six-record limit. First checks cover yesterday and today; normal checks cover at most the last seven days. Web requests read stored posts and cannot trigger or poll LinkedIn scrapes. Historical backfills require a separate operator command and an approval reference. Stored posts, analyses and read/saved state remain available when fetching is paused. Saturated result windows pause for reconciliation instead of silently skipping unseen posts. Existing installations add the collection ledger and source checkpoints at API startup.

## Repost watch

The dedicated LinkedIn activity page discovers every eligible personal profile and company source. Active sources become eligible on the next enabled scheduler scan, subject to daily, account, ownership and budget checks. Paused sources retain stored history. Existing account bookmarks, group filters and read/saved preferences remain available. Posts and supported attributed reposts can be viewed for every source; provider coverage is not guaranteed. The page refreshes stored data every 30 seconds and never starts scraping.

The Repost watch page separates CEO (Sheet 1, 16 accounts) and Company (Sheet 2, 15 accounts) from the supplied repost workbook. The 14 shared accounts are collected once and appear in both groups. All tracked posts appear by publication date without a relevance threshold. Available interaction guidance from each sheet is shown on the post. The page and its unread sidebar badge refresh every 30 seconds; source collection follows each source's configured interval. Only in-app notifications are enabled for these imported accounts.

The curated, canonicalized source manifest is in `data/repost-watch-sources.json`, including worksheet/row provenance. Import it once, or rerun it safely, with `docker compose exec -T api python -m app.import_repost_watch /srv/news/data/repost-watch-sources.json`. Existing source records and email settings are preserved. The remaining workbook sheets are excluded from this import. Reposting is performed manually on LinkedIn.

Canonical LinkedIn URL validation rejects duplicate creation and edits, including concurrent submissions. Historical duplicate rows remain intact and share one canonical collection owner. Identity changes and deletion are blocked after collection history exists; pause the source to retain its posts, analyses, saved state and daily limit.

## Release safety

Development CI performs verification only. The production sync mechanism must remain held until its separately approved transition is complete. Production deployment and the first live provider request are separate approvals. See [the consolidated checklist](docs/release-readiness.md). Never commit private audit exports, temporary investigations, environment files or account evidence. Repository-local guards can be enabled with `git config --local core.hooksPath .githooks`.
