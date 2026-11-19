# Private user accounts verification

Implementation is local on `feat/private-user-sources`. Production code, database,
credentials, collection flags and email configuration have not been changed.

## Passing checks

- Full backend suite: **463 passed**. All ten new private-user cases pass, covering
  admin-only user management, member login and Unicode passwords, password-reset
  and disable session revocation, member-to-member and member-to-admin isolation,
  personal manual/test/scheduled recipients, foreign item mutations and deep
  analysis, independent LinkedIn/Facebook ownership, archive ownership and
  idempotent migration preserving read/saved state, subscriptions, delivery
  receipts and report checkpoints.
- Frontend: **22 passed**, including three new cases for user creation, empty
  member feeds and member access to personal Sources without billing access.
- Next.js production build: passed; includes `/intelligence/users`.
- TypeScript: passed after regenerating stale local Next.js route types.
- ESLint for every changed frontend source and the new test: passed.
- `git diff --check`: passed. Existing line-ending notices are informational.
- Independent code review: two findings fixed and regression-tested; follow-up
  review found no further substantive issues.

The full lint command also reports existing violations in unchanged CommonJS
test files and `intelligence-page.tsx`; these are outside this feature.

## Backend failures

The full backend suite has **10 failures**, all in existing social collection
lifecycle tests. No SMTP, AI or provider traffic was permitted during tests.

| File | Test |
| --- | --- |
| `test_collection_exception.py` | `test_every_approved_source_gets_one_larger_request_and_restart_never_repeats` |
| `test_collection_exception.py` | `test_reserved_snapshot_finishes_after_exception_expiry_without_a_second_post` |
| `test_collection_exception.py` | `test_larger_linkedin_result_saves_only_the_last_exact_interval` |
| `test_collection_repairs.py` | `test_one_daily_social_fetch_and_two_website_checks` |
| `test_collection_trial_end_to_end.py` | `test_morning_to_evening_fetch_storage_billing_and_single_report` |
| `test_social_schedule.py` | `test_two_social_fetches_per_day_repeat_daily_and_survive_restart` |
| `test_social_schedule.py` | `test_pending_snapshot_can_finish_after_slot_without_replacement_fetch` |
| `test_social_schedule.py` | `test_recurring_billing_refresh_does_not_require_a_trial_date[True]` |
| `test_social_schedule.py` | `test_concurrent_collection_workers_and_new_sources_wait_for_next_slot` |
| `test_social_schedule.py` | `test_facebook_also_runs_only_in_two_slots_and_websites_keep_periodic_checks` |

Baseline verification uses an export of the tracked backend at unchanged HEAD,
the same application configuration file, and a disposable PostgreSQL database.
All ten failures reproduced on that original code: five scheduler failures in
the first baseline run and the remaining five in the second. They are existing
failures, not regressions introduced by this feature.

No merge, push or production deployment has been performed. Deployment remains
subject to the repository's existing release controls.
