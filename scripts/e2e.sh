#!/usr/bin/env bash
# Starts a throwaway platform database, the API, a worker and the web app, runs the Playwright suite, then tears everything down.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/backend/.venv/bin/python}"
LOGS="${E2E_LOGS:-/tmp/afterstate-e2e}"; mkdir -p "$LOGS"
API_PID=""; WEB_PID=""
cleanup() {
  # each service runs in its own process group (setsid); signal the whole group so no child outlives the run
  for pid in "$WEB_PID" "$API_PID"; do [ -n "$pid" ] && kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 3
  for pid in "$WEB_PID" "$API_PID"; do [ -n "$pid" ] && kill -KILL -- "-$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
for port in 3000 8000; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then echo "port $port is already in use; refusing to test against a stale server" >&2; exit 1; fi
done
wait_for() { for _ in $(seq 1 90); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 1; done; echo "timed out waiting for $1" >&2; tail -20 "$2" >&2; return 1; }
setsid bash -c "cd '$ROOT/backend' && exec '$PY' -m afterstate.cli demo --port 8000" >"$LOGS/api.log" 2>&1 & API_PID=$!
wait_for http://127.0.0.1:8000/api/v1/health/live "$LOGS/api.log"
# Run the same standalone server the Docker image runs (build first: `npm run build` in web/).
[ -f "$ROOT/web/.next/standalone/server.js" ] || { echo "run 'npm run build' in web/ first" >&2; exit 1; }
rm -rf "$LOGS/standalone" && cp -r "$ROOT/web/.next/standalone" "$LOGS/standalone"
mkdir -p "$LOGS/standalone/.next" && cp -r "$ROOT/web/.next/static" "$LOGS/standalone/.next/static" && cp -r "$ROOT/web/public" "$LOGS/standalone/public"
PORT=3000 HOSTNAME=127.0.0.1 setsid bash -c "cd '$LOGS/standalone' && exec node server.js" >"$LOGS/web.log" 2>&1 & WEB_PID=$!
wait_for http://127.0.0.1:3000/ "$LOGS/web.log"
cd "$ROOT/web" && npx playwright test "$@"
