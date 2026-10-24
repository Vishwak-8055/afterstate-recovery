# AFTERSTATE

**Prove recovery before it matters.**

AFTERSTATE restores your real PostgreSQL backups into disposable environments, verifies that the recovered database is
structurally and logically correct, measures how long recovery takes and how much data it would cost, and keeps checksummed
evidence another engineer can reproduce. Recovery is treated as an engineering capability that must be tested, measured and
evidenced, not merely documented.

> **Scope of the first release.** PostgreSQL logical backups only (`pg_dump -Fc` custom and `-Ft` tar). Verified against
> PostgreSQL 16. It does not claim support for other databases, point-in-time recovery, or regulatory compliance. Read
> [docs/limitations.md](docs/limitations.md) before relying on it.

## What it does

| Stage | What happens | Evidence |
|---|---|---|
| Artifact verification | SHA-256 against the recorded value, format sniffing, table of contents, version compatibility, full read of every data block | 6 checks per run |
| Disposable environment | Private PostgreSQL cluster, **no network listener** (unix socket in a 0700 directory), unprivileged restore role | Cleanup task recorded *before* creation |
| Restore | `pg_restore --exit-on-error` as the unprivileged role, policy-filtered TOC, timeout, disk cap, live progress from real events | Sanitized execution log |
| Verification | Structure, constraints, indexes, extensions, counts, SQL invariants, complete dataset checksums, recovery-point probe, application smoke tests, run as a **read-only** role | PASS / FAIL / SKIPPED / INCONCLUSIVE per check |
| Judgement | RTO from a monotonic clock; RPO from evidence or reported *unknown* | Objectives with their basis |
| Cleanup and report | Verified teardown, JSON + HTML report with checksums | `report.json`, `report.html` |

A run is `SUCCEEDED` only if **every required check passed and every configured objective was demonstrably met**. A skipped or
inconclusive required check is `NEEDS_ATTENTION`, never success.

## Quick start (no Docker)

Requires Linux, Python 3.12, Node 22 and PostgreSQL 14+ server and client binaries (tested with 16). Run as a **non-root** user:
PostgreSQL refuses to run as root, and the engine should not either.

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.lock && pip install --no-deps -e .
cd ../web && npm ci

# terminal 1: throwaway platform database + API + worker (prints a demo login)
cd backend && afterstate demo
# terminal 2: the web app (proxies /api to http://127.0.0.1:8000)
cd web && npm run dev          # http://localhost:3000
```

Sign in with the printed credentials and open **Recovery Lab** to watch the real engine restore, reject, time out and recover.

## Deploy with Docker Compose

```bash
cp .env.example .env            # set POSTGRES_PASSWORD (URL-safe) and PUBLIC_ORIGIN
docker compose up -d --build    # db, migrate (one-shot), api, worker, web
docker compose --profile proxy up -d   # optional: Caddy with automatic TLS for PUBLIC_HOST
```

The Compose file, Dockerfiles and CI workflow were written and structurally validated but **not executed** in the
environment this repository was built in (no Docker daemon). Run `docker compose config -q` and the CI pipeline first.
Full guidance: [docs/operations.md](docs/operations.md).

## Developer commands

| Command | Does |
|---|---|
| `make setup` | venv + dependencies from lockfiles |
| `make lint` / `make typecheck` | ruff + eslint / mypy `--strict` + tsc |
| `make test-unit` / `make test-integration` / `make web-test` | backend unit / real-PostgreSQL integration + API / frontend |
| `make migrations-check` | apply to empty DB, drift check, downgrade round trip |
| `make e2e` | Playwright against a live stack it starts and stops itself |
| `make audit` | pip-audit + npm audit |
| `make web-build` | production build |

`make test-integration` and `make e2e` start PostgreSQL clusters and must not run as root.

## Documentation

[Architecture](docs/architecture.md) · [Recovery engine reference](docs/recovery-engine.md) · [Threat model](docs/threat-model.md) ·
[Operations and deployment](docs/operations.md) · [Environment variables](docs/environment.md) · [Known limitations](docs/limitations.md) ·
[Architecture decisions](docs/adr/) · [Delivery report](docs/DELIVERY_REPORT.md)

## Repository layout

```
backend/   FastAPI service, worker, recovery engine, migrations, tests (Python 3.12)
  afterstate/engine     inspect, environment, restore, validators, pipeline, report
  afterstate/worker     claim, lease, reaper, scheduler, notifications
  afterstate/api        routers, deps (auth, RBAC), middleware
  afterstate/lab        fixtures and scenario catalog
web/       Next.js app (App Router, strict TypeScript, Tailwind) and Playwright E2E
deploy/    reverse proxy sample    docs/  architecture, ADRs, threat model, operations
scripts/   e2e orchestrator, fixture and docs generators
```
