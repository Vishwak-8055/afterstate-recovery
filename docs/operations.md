# Operations and deployment

## Components
`db` (PostgreSQL 16, platform metadata and the job queue), `migrate` (one-shot), `api`, `worker`, `web`, optional `proxy` (Caddy).
API and worker are the same image. They share the artifact volume. Only the worker has the restore scratch volume (and the API a small tmpfs for Lab fixtures).

## Deploy with Docker Compose
```bash
cp .env.example .env              # set POSTGRES_PASSWORD (openssl rand -hex 24) and PUBLIC_ORIGIN
docker compose config -q          # validate
docker compose up -d --build
docker compose --profile proxy up -d   # optional TLS via Caddy for PUBLIC_HOST (ports 80/443)
```
This file set has not been executed by the authors' build environment (no Docker daemon); treat the first deploy as a verification step:
`docker compose ps` should show `migrate` exited 0, `api`/`worker`/`web`/`db` healthy, and `curl localhost:3000/api/v1/health/ready` returning `ready`.
Then sign in, open the Recovery Lab and run *Successful restore*: that exercises the API, worker, storage, database and restore tooling in one go.

* **TLS:** terminate TLS at the proxy profile (or your own). Production mode requires `Secure` cookies, so use HTTPS or `localhost`.
* **Large uploads:** route `/api` straight to the API at the proxy (the sample Caddyfile does, limit 2 GB). The Next.js `/api` rewrite is for development and small files.
* **Network:** `db` and `worker` sit on an `internal` network with no internet egress. Webhook delivery happens in the worker: attach it to `edge` if you use webhooks.
* **Sizing:** the worker needs scratch disk of roughly 2-3x the restored database size and enough RAM for `shared_buffers` (default 128 MB) plus `pg_restore`. Set `AFTERSTATE_MAX_ENVIRONMENT_BYTES` accordingly. Run several workers (`docker compose up -d --scale worker=3`) for concurrency; each executes one run at a time.
* **Cost:** self-hosted: one PostgreSQL instance, disk for backups and scratch, and compute while restores run. No third-party services.

## Environment
All settings: [environment.md](environment.md). Production refuses to start with insecure cookies, the default database credentials, or private-address webhooks allowed.

## Migrations
`afterstate migrate` (the `migrate` service). Before the first release the schema is a single revision; later changes are additive. `scripts/check_migrations.py` verifies apply, model drift, and downgrade round trip.

## Health and observability
* `GET /api/v1/health/live`, `GET /api/v1/health/ready` (database, storage, tooling; worker staleness is reported but does not fail readiness).
* Structured JSON logs with `correlation_id` (also returned as `X-Request-ID`), `run_id`, `workspace_id`, `stage`; credentials and row data redacted.
* Workspace settings, *Security and operations*, shows worker heartbeats, queue depth and limits. Runs in `NEEDS_CLEANUP` are surfaced on the overview.
* Not implemented: OpenTelemetry export and a Prometheus endpoint (see limitations).

## Backing up and restoring AFTERSTATE itself
1. **Platform database:** `docker compose exec db pg_dump -U afterstate -Fc afterstate > afterstate-platform.dump` (and keep it somewhere that is not this host).
2. **Artifact volume:** snapshot the `artifacts` volume (backups and report evidence; checksums in the database detect corruption).
3. **Restore:** create a fresh stack, `pg_restore` the platform dump into the new `db` before starting `api`/`worker`, mount the artifact volume, then run `migrate`. Verify by opening a past report: the API refuses evidence whose checksum no longer matches.
You can also point AFTERSTATE at its own platform dump as a recovery project: it is a PostgreSQL custom-format archive.

## Retention
`retention_days` is stored per workspace but **automatic purging is not implemented** in this release. Remove old artifacts through the API; run history and reports are retained.

## Troubleshooting
| Symptom | Cause and fix |
|---|---|
| Integration tests skipped, or "root execution of the PostgreSQL server is not permitted" | Run as a non-root user. |
| `PostgreSQL server and client binaries ... were not found` | Install PostgreSQL 14+ or set `AFTERSTATE_PG_BIN_DIR`. |
| `scratch directory path is too long` | Use a shorter `AFTERSTATE_SCRATCH_DIR` (unix socket paths are limited to about 107 bytes). |
| Run stays QUEUED | No worker is running, or the workspace concurrency limit is reached (Settings, System). |
| Run `FAILED / RESOURCE_LIMIT` | Raise `AFTERSTATE_MAX_ENVIRONMENT_BYTES` and scratch disk. |
| Run `FAILED / RESTORE_FAILED` mentioning privileges | The backup needs a superuser-only object (untrusted extension). It is reported, not worked around. |
| Run `NEEDS_CLEANUP` | The worker retries automatically; use *Retry cleanup* on the run, or check scratch volume permissions. |
| Sign-in loops over HTTP | Production sets `Secure` cookies; use HTTPS or `localhost`. |
| 403 `origin_not_allowed` | Add your public origin to `PUBLIC_ORIGIN` / `AFTERSTATE_ALLOWED_ORIGINS`. |
