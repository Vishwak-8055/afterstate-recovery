# Known limitations (read before relying on this)

**Verification status.** In the environment this repository was built in: backend unit/integration/API tests, strict typing, lint, migrations, the web
production build, frontend unit tests and a Playwright suite against a live stack all ran. See [DELIVERY_REPORT.md](DELIVERY_REPORT.md) for counts.
**Not executed there** (no Docker daemon, no GitHub): `docker build`, `docker compose up`, the GitHub Actions workflow, and the Caddy profile. They were
written carefully and structurally validated (YAML parses, scripts syntax-checked) only.

**Scope.** PostgreSQL logical backups (custom and tar). No other database, no WAL/point-in-time recovery, no physical backups. Tested on PostgreSQL 16 only.
**RPO** can only come from a recovery-point probe query or a declared backup time (a lower bound). It is reported *unknown* otherwise.
**Application verification** supports predefined suites only; the shipped suite is `reference-shop` (the demo schema). Running a customer's own application
against the restored database is not supported (it would execute arbitrary code beside restored data).
**Isolation** is process and filesystem level, not namespaces or VMs (see the threat model).
**Platform:** the worker is Linux only (`/proc`, `prlimit`). One run executes per worker process at a time.
**Storage:** local filesystem only; the interface exists for S3-compatible storage but no S3 backend is implemented. Uploads larger than a few MB must bypass the Next.js dev proxy.
**Retention:** configured, not enforced (no purge job).
**Observability:** structured logs, correlation ids and health endpoints; no OpenTelemetry/Prometheus export.
**Security:** no MFA/SSO; rate limiting is per API process; webhook DNS rebinding is not fully closed.
**Locale:** restore environments use the `C.UTF-8` locale; behaviour depending on collation order can differ from production.
**Performance:** no large-scale benchmark was run. Observed on a 1-CPU sandbox: a 217 KiB archive (about 20k rows) is verified end to end in a few seconds;
throughput figures at production sizes are unmeasured. The disposable-cluster approach scales with the restored size (disk, RAM) like any restore.
**Accessibility:** colour contrast of both themes is tested against WCAG AA; keyboard operation and labels were built in, but no screen-reader audit or automated axe scan was performed.
**Design:** the interface was checked visually in both themes at one desktop viewport; responsive layouts below tablet width were not reviewed in a browser.
**Licence:** none has been chosen; add one before publishing.
