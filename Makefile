SHELL := /bin/bash
BIN := backend/.venv/bin
.DEFAULT_GOAL := help
.PHONY: help setup lint typecheck test test-unit test-integration web-test web-build e2e migrations-check audit demo env-docs up down

help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## create the Python venv and install backend and web dependencies from the lockfiles
	python3 -m venv backend/.venv
	$(BIN)/pip install -r backend/requirements-dev.lock
	$(BIN)/pip install --no-deps -e backend
	cd web && npm ci

lint: ## ruff (backend) and eslint (web)
	cd backend && .venv/bin/ruff check afterstate tests scripts && .venv/bin/ruff format --check afterstate tests scripts
	cd web && npm run lint

typecheck: ## mypy --strict (backend) and tsc (web)
	cd backend && .venv/bin/mypy afterstate
	cd web && npm run typecheck

test-unit: ## backend unit tests (no database needed)
	cd backend && .venv/bin/pytest tests/unit -q

test-integration: ## backend integration + API tests against real PostgreSQL (run as a NON-root user)
	cd backend && .venv/bin/pytest tests/integration -q

web-test: ## frontend unit tests (formatters, rule builder, RBAC, WCAG contrast of the design tokens)
	cd web && npm test

test: test-unit test-integration web-test ## everything except the browser E2E suite

web-build: ## production build of the web app
	cd web && npm run build

e2e: ## Playwright against a live stack (starts its own throwaway database, API, worker and web app)
	E2E_DUMP="$$($(BIN)/python scripts/make_fixture_dump.py)" ./scripts/e2e.sh

migrations-check: ## apply migrations to an empty database, check model drift, round-trip downgrade
	cd backend && .venv/bin/python scripts/check_migrations.py

audit: ## dependency vulnerability scans
	cd backend && .venv/bin/pip-audit -r requirements.lock --no-deps --disable-pip
	cd web && npm audit --omit=dev --audit-level=high

env-docs: ## regenerate docs/environment.md from the Settings model
	cd backend && .venv/bin/python ../scripts/gen_env_docs.py

demo: ## API + worker + throwaway database on this machine (no Docker); then run `npm run dev` in ./web
	cd backend && .venv/bin/afterstate demo

up: ## docker compose up (requires .env, see .env.example)
	docker compose up -d --build

down: ## docker compose down
	docker compose down
