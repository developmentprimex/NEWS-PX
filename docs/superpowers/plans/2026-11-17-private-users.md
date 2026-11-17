# Private users implementation plan

**Goal:** Let the existing administrator create users with private sources and email reports.

**Architecture:** Keep the configured administrator ID, password and email recipient unchanged. Add database-backed member accounts and source ownership, backfill existing sources to the administrator, and enforce ownership at every API and report selection. Provider budget controls stay shared.

**Tech stack:** FastAPI, SQLAlchemy/PostgreSQL, Next.js/React; standard-library scrypt password hashing.

**Approved scope:** User requested implementation after confirming existing sources, history, saved state, email destination and schedules remain unchanged. Administrator creates member accounts; no public registration. Members manage their own sources. GA timetable attachments remain administrator-only.

## Tasks

- [x] Add failing integration tests for member login, admin-only account creation, source/item isolation, personal mail routing, and idempotent legacy migration.
- [x] Add account persistence, password hashing, role enforcement, recipient resolution, and additive ownership migration. Keep administrator cookies valid.
- [x] Scope every source/feed/item/brief/check endpoint to its owner. Scope social identities and retained source history per member without changing existing administrator identities or shared budget limits.
- [x] Route manual, test, legacy and scheduled email to the owning account; exclude disabled accounts and foreign subscriptions. Preserve existing recipient and timetable attachments for administrator.
- [x] Add administrator Users page, allow members to manage personal Sources, and show account identity in navigation.
- [x] Run PostgreSQL integration tests, complete backend/frontend test suites, TypeScript and lint checks; review diff for missed ownership paths and private-data leakage.

## Review focus

Foreign UUIDs return 404 without mutation; member cannot create users or inspect shared provider records; disabled accounts lose active sessions and email delivery; duplicate social URLs across users remain independently owned; existing delivery checkpoints and receipts remain unchanged on migration.

## Execution record

Implementation runs in the existing checkout on a feature branch, as authorized by `implemento`. No production deployment or provider calls are part of local verification.

The independent reviewer identified Unicode credential comparison and an archive lookup using administrator ownership. Both were corrected and covered by tests; a second review found no further substantive issues. Final backend verification: 463 passed, ten existing collector lifecycle failures; all ten reproduced on the unchanged HEAD export using the same configuration. Frontend: 22 tests pass, production build and changed-file lint pass. See [verification details](2026-11-17-private-users-verification.md). Work remains local and uncommitted; no production changes were made.
