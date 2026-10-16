# LinkedIn Intelligence release checklist

Local implementation is ready for verification and a development-branch commit. Production deployment and provider activation remain separate **NO-GO** decisions until their evidence and explicit approvals are accepted. Mocked tests do not establish real provider billing or LinkedIn coverage.

## Four external blockers

| Blocker / responsible owner | Minimum required action | Acceptance criteria | Blocks |
| --- | --- | --- | --- |
| Refreshed runtime inventory / server administrator | Run the existing single consolidated read-only diagnostic action in the privately retained administrator handoff. Privately review and return only the approved projection and hash. | One recent, simultaneous capture maps the News task/process/listener chain, Uvicorn parents, PM2/Celery worker and beat roles, unexplained listeners, duplicate instances, boot/recovery/deploy paths and all identified credential consumers. Unknown ownership is resolved or demonstrably isolated. No raw command lines, environment values or credentials are exported. | Deployment and live test |
| Fresh database backup and rehearsal / DBA | Create an authorized fresh production dump, verify its integrity, and separately authorize restoration to a new isolated PostgreSQL 18 target. Apply candidate migrations twice there. | Fresh timestamp and dump hash; readable and fully restorable backup; preserved source/post/analysis/read/saved IDs, counts and original-column hashes; valid ownership and foreign keys, no orphans, migration idempotency, and verified production migration authority. The older backup cannot substitute. Production data remains unchanged. | Deployment and live test |
| Legacy deployment/restart isolation / release owner and administrator | Approve and execute a News-only maintenance plan after refreshed ownership mapping. Hold legacy sync, queued releases and boot/failure reactivation; retire only the verified legacy News tree. Review candidate and independent rollback launch contexts. | No collecting legacy/duplicate runtime; both candidate and rollback demonstrably paused and credential-free across task principals and inherited environments; reviewed immutable SHA and rollback artifact; protected manual workflow approval; old sync held or replaced with the verified inert launcher. Unrelated applications stay running. | Deployment and live test |
| Billing, allowance and consumers / account owner and provider support | Review current account settings and all-product usage exports without scraping. Confirm entitlement, cycle, pricing, available credits, shared consumers, pending jobs, financial controls and old/new account transition. | Current active status and nonnegative balance; account-specific eligible recurring allowance and renewal/cycle confirmed; actual usage and billing timestamps within the application's freshness limit; shared-pool use reconciled; provider charging/error/over-delivery rules and spending limits understood; historical ledger retained, no assumed reset. Company endpoint eligibility is confirmed before a company test. | Live test; credential/consumer isolation also blocks deployment |

**A — Deployment with scraping disabled: NO-GO.** Requires the deployment gates above and explicit approval of the exact paused release/rollback plan.

**B — First single-source live request: NO-GO.** Additionally requires current billing/consumer evidence, a valid private-canary dry-run, one selected source, one consumed approval, exactly six internal reserved records, one trigger with no automatic retries, and before/after provider billing reconciliation. Over-delivery, errors, invalid records, uncertain outcomes or incomplete windows stop the test. Six reserved records are an internal estimate, not a guaranteed maximum provider charge.

## Git review and development push

Private audit/tmp files and operational reports stay local and ignored. Existing historical private files are removed only from the latest tree; this does not erase their earlier Git history. No history rewrite or force-push is authorized. Local commit guards block private artifacts, dotenv variants and key exports; the push guard blocks production refs and tags. These guards do not replace a content review or repository-side protections.

The installed legacy poller follows `main`; an explicit push solely to `fix/linkedin-release-readiness` does not satisfy its branch filter. Never use a broad or mirror push. After reviewing the prepared local commit, use:

```powershell
git branch --show-current
git show --stat --oneline HEAD
git status --short
git -c remote.origin.mirror=false push --no-follow-tags origin HEAD:refs/heads/fix/linkedin-release-readiness
```

No push is performed by release preparation. Do not push/merge `main` or dispatch a release until the server's legacy deployment path is held under separate approval. Application directory swaps do not replace that old poller automatically.

## Local checks

Run the backend tests with mocked HTTP and the supported isolated PostgreSQL test identity, then frontend source-selection tests, TypeScript and the production build. Run both Windows deployment fixture scripts and Git guard tests. Inspect the final staged blobs, verify no private/generated artifacts are added, and compare preserved-file hashes after index cleanup. Current private verification evidence records the actual executed results; it is not part of the release commit.
