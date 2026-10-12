# Windows Server deployment

The deployment on `192.168.10.8` lives at `E:\websites\news-intelligence`.
It uses a dedicated PostgreSQL 18 database and role named `news_intel`, a
Python virtual environment in `.venv`, and a Next.js production build in
`frontend\.next`. Private settings are in the server's `.env` file.

Two scheduled tasks start the application at boot:

- `NewsIntelligenceAPI` runs `start-api.cmd` on `127.0.0.1:8001`.
- `NewsIntelligenceWeb` runs `start-web.cmd` on `127.0.0.1:3101`.

Both tasks run as SYSTEM with no execution time limit (`PT0S`). On failure,
Task Scheduler retries every minute, up to 10 times. This replaces the default
72-hour limit that could stop the long-running API and web processes.

Cloudflare Tunnel publishes `news.primexeu.com` to `http://127.0.0.1:3101`.
The IIS site `News Intelligence` uses the `NewsIntelligencePool` application
pool and the same host binding on port 80 as an origin fallback. Its root is
`iis-proxy`, which proxies requests to the Next.js service. The other IIS
sites and PostgreSQL databases are separate.

Pushing to `main` runs `.github/workflows/deploy.yml` on GitHub Actions to
verify the frontend build and backend syntax. The server task
`NewsIntelligenceSync` checks the latest workflow result every five minutes.
When the successful run matches the current `main` commit, `sync.ps1` fetches
that commit and runs `deploy/deploy.ps1`. A failed workflow never deploys.

`deploy/deploy.ps1` builds the checkout in `_deploy\stage`, replaces only this
application's `backend` and `frontend` directories (and `.venv` only when
Python requirements or its runtime change), then checks the
API and web endpoints. It restores the previous directories if the health
check fails. The two latest backups stay in `_deploy\backups`. The production
`.env`, PostgreSQL database, `data` directory, IIS configuration, and
Cloudflare route are outside the replacement set.

Cloudflare Tunnel manages the public DNS record and HTTPS endpoint.

Deployments reuse immutable frontend dependencies in `_deploy\node-cache`,
keyed by `package.json`, `package-lock.json`, project `.npmrc`, Node/npm
versions and architecture. Each release links to its cache entry; it never
runs `npm ci` against the live frontend. The first optimized deployment imports
the existing dependencies with a parallel copy. Changed dependencies create
a new cache entry. Cache entries are retained for the releases that use them.

Unchanged Python requirements/runtime reuse the working `.venv` and Chromium;
`pip check` verifies the environment. A changed environment is built separately
and participates in rollback. Health checks complete before the deployed SHA
is written. Partial directory switches restore only directories actually backed up.

`cleanup.ps1` drains `_deploy\cleanup-queue` in a hidden background process,
independent of deployment success. It keeps the two latest backups and removes
only explicitly queued release directories. Junctions are unlinked without
following them into shared dependency caches. A file lock prevents overlapping
cleanup workers; the sync task retries deferred requests on later checks.
Failures are recorded in `_deploy\cleanup.log` and do not roll back a healthy site.

Run `powershell -NoProfile -ExecutionPolicy Bypass -File deploy/test-deploy.ps1`
to check dependency invalidation, partial/full rollback, cleanup path boundaries,
junction preservation and the cleanup worker lock.
