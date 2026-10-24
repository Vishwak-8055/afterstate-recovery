# Delivery report

Version 0.1.0. Everything below was executed in the build environment (Ubuntu 24.04, 1 CPU, PostgreSQL 16.15, Python 3.12, Node 22) unless marked **not executed**.

## Implemented
* **Recovery engine** (`backend/afterstate/engine`): artifact verification (6 checks incl. full-archive read), disposable socket-only PostgreSQL clusters, unprivileged restore with TOC policy, three validation stages (structure, data, application), RTO/RPO evaluation, verified cleanup, checksummed JSON + HTML reports.
* **Durable orchestration** (`worker/`): leases, heartbeats, fencing, reaper, retries, per-workspace concurrency, timezone-aware cron scheduler with overlap and missed-run policies, webhook notifications (SSRF-guarded).
* **API** (48 operations): sessions, CSRF, RBAC (4 roles), tenant isolation, audited denials, streaming uploads, projects and rules, run control, comparison, overview, schedules, audit explorer, Lab.
* **Web app** (17 routes): landing, auth, onboarding, overview, backup registry + detail, project editor with rule builder and plan preview, run history, execution workspace, report, compare, Recovery Lab, schedules, audit, settings. Dark graphite and light ivory themes.
* **Recovery Lab:** 10 scenarios on real archives; faults only for Lab runs; safe reset.
* CLI (`afterstate migrate | api | worker | dataset-checksum | demo`), Dockerfiles, Compose, Caddy sample, CI workflow, lockfiles, docs.

## Verification results
| Check | Result |
|---|---|
| Backend tests (real PostgreSQL, real `pg_restore`) | **164 passed**: 99 unit, 1 end-to-end pipeline, 23 failure-mode, 17 environment-security, 24 API |
| Frontend unit tests (formatters, RBAC mirror, rule serialisation, WCAG AA of both themes from the real token file) | 15 passed |
| Browser E2E (Playwright, live stack, standalone server) | 7 passed |
| `mypy --strict` / `ruff check` / `ruff format --check` / `eslint` / `tsc` | clean (68 source files; 82 formatted) |
| Migrations: apply, `alembic check` (no model drift), downgrade, re-apply, enum CHECKs | pass |
| Production build (`next build`, standalone output) | pass |
| `pip-audit` (runtime lock) / `npm audit --omit=dev` | no known vulnerabilities / 0 |
| Mutation checks (tenant filter removed, CSRF check removed) | the tests failed, as they must |
| **Not executed:** `docker build`, `docker compose up`, GitHub Actions run, Caddy | YAML parses, scripts syntax-checked only |

## Real recovery demonstration
A synthetic shop database (2,000 customers, 6,000 orders, 13,500 items, 6,000 ledger rows; 31 archive items, 217 KiB archive) is dumped with `pg_dump -Fc`, verified, restored into a disposable cluster as an unprivileged role, and passes 30+ checks: schema, tables, columns, constraints, indexes, extension, counts, two business invariants, a complete 2,000-row dataset comparison, a recovery-point probe, four HTTP smoke checks against a reference application, with RTO and RPO met. The whole run takes single-digit seconds on the 1-CPU sandbox. **No large-scale benchmark was performed.**

## Failure-injection results (all produced the expected, asserted outcome)
corrupted archive (FAILED / ARTIFACT_CORRUPT, no environment created) · truncated archive · TOC corruption that segfaults `pg_restore` (classified corrupt) · plain-SQL dump (FORMAT_UNSUPPORTED) · checksum drift · missing file · missing schema (FAILED, independent checks still reported) · broken business invariants · slow restore (RESTORE_TIMEOUT, process killed, environment removed) · restored database killed before validation (ENVIRONMENT_UNAVAILABLE, nothing falsely passed) · query failure (INCONCLUSIVE → NEEDS_ATTENTION) · skipped required check (never success) · missed RTO · unknowable RPO · first cleanup fails (NEEDS_CLEANUP then settles) · cancellation mid-restore · **worker `SIGKILL`ed mid-restore: orphaned PostgreSQL found and removed from durable state, run settled FAILED/WORKER_INTERRUPTED, retry succeeded** · stale lease holder fenced out.

## Security checks performed
Restore role probed with 11 escape attempts; validator cannot write and ignores database-level setting overrides; function shadowing cannot lie to validation queries; no network listener and 0700 directories; cross-tenant ID guessing; RBAC with audited denials; CSRF and Origin checks; per-account and per-IP rate limits; account-enumeration resistance; request-size limits; secret and row-data redaction; tamper-evident evidence; cleanup refuses unowned paths; SSRF guard; fault injection ignored outside the Lab; dependency audits. See [threat-model.md](threat-model.md) for residual risks.

## Defects found by the verification process and fixed
Duplicate/colliding enum CHECK constraints in the generated migration · reaper insert violated a NOT NULL column · redaction leaked `Authorization: Bearer` tokens and `PGPASSWORD=` values · request-validation (422) error handler would have crashed with a `TypeError` (found by strict typing, now tested) · the validator session could be altered by a hostile backup's `ALTER DATABASE ... SET` (now pinned) · every tab and "Add rule" button submitted the project form (found by the browser E2E) · the Lab fixture cache never expired, so RPO scenarios would fail after 48 h on a long-lived install (found when the cache aged in the sandbox) · API container had no scratch space for Lab fixtures (Compose) · light-theme secondary text missed WCAG AA by 0.002 · my E2E orchestrator leaked server processes (now process-group cleanup and a stale-port guard).

## Git
Local repository on `main`, no remote configured, **nothing pushed or deployed** (no credentials or authorization). Commits are authored as `AFTERSTATE Build <build@afterstate.invalid>`; amend before publishing. No secrets are committed (`.env` is ignored; `.env.example` has placeholders).

## Deployment
No URL: nothing was deployed. Local use and Compose instructions are in [operations.md](operations.md). Hosted deployment needs: a Docker host (or equivalent) that supports a PostgreSQL instance, a worker that can run PostgreSQL server binaries as non-root with scratch disk, shared storage between API and worker, and TLS.

## Known limitations
See [limitations.md](limitations.md). Highlights: PostgreSQL logical backups only (tested on 16); process-level isolation; Docker/CI/Caddy unexecuted here; no retention purge, no S3 backend, no OpenTelemetry export, no MFA/SSO; no large-scale benchmark or screen-reader audit.
