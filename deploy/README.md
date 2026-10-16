# Windows release controls

Production deployment remains **NO-GO** until the external evidence in [the consolidated checklist](../docs/release-readiness.md) is accepted. No production modification or provider activation is authorized by these instructions.

`deploy.yml` runs CI on development pushes and pull requests. `release-production.yml` requires a deliberate manual dispatch and a protected production environment; it records a tested SHA and does not deploy. Repository administrators must configure required reviewers, prevent self-approval and restrict the release branch. YAML alone does not configure these protections.

The installed legacy sync follows successful verification on `main`. Development branch pushes do not match that branch filter. Do not push or merge `main`, dispatch a release, or re-enable legacy deployment until its approved maintenance transition. The candidate `sync.ps1` is inert. An application directory swap does not replace root-level sync launchers; the administrator must keep the existing task held or separately install and hash-verify the reviewed inert launcher.

The deployment entrypoint requires `-ReleasePermit` referencing a private, ACL-reviewed JSON permit bound to the clean checkout's exact SHA. It must contain owner approval, an expiry within 24 hours and evidence-backed Boolean gates for runtime inventory, fresh backup rehearsal, held restart paths, isolated consumers, paused rollback and protected workflow approval. Consumption is durable and one-use. Never commit a permit or create one merely to satisfy a check.

Before mutation and before candidate/rollback startup, all collection, LinkedIn-fetch, canary and snapshot-import flags must explicitly be false. The application environment must contain no usable provider credential. Ambiguous dotenv syntax, exported or multiline overrides and duplicate collection settings fail closed. Current-console environment checks do not prove another task principal or launcher has the same settings; administrator evidence is required.

Only the News API and web tasks are managed. Occupied ports or surviving child processes require administrator ownership review; the script does not force-kill processes by port/name or start another instance. Unrelated applications, runners and workers must remain untouched. No execution-policy bypass is requested.

The deployment stages backend/frontend and changed dependencies separately, then swaps only code/environment directories. It does not restore, migrate backwards or reset the production database, account ledger or stored posts. A partial switch restores only directories actually backed up. Keep an independently reviewed paused rollback artifact outside automatic cleanup.

Local checks (mocked process/task calls and throwaway directories only):

```powershell
powershell.exe -NoProfile -NonInteractive -File deploy/test-deploy.ps1
powershell.exe -NoProfile -NonInteractive -File deploy/test-release-control.ps1
node --test deploy/check-push.test.cjs
```

Private server bindings, diagnostic exports, backup locations, credentials and operational runbooks are retained outside the release commit. A reviewed administrator runbook supplies the actual approved server-console command after all gates pass.
