"""Reference application used by the predefined 'reference-shop' smoke-test suite.

It is a tiny read-only HTTP service over the restored shop schema. The recovery engine starts it
bound to 127.0.0.1 on an ephemeral port, runs the configured HTTP checks against it, and stops it.
It receives the database password through the environment, never on the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg


def make_handler(args: argparse.Namespace) -> type[BaseHTTPRequestHandler]:
    def connect() -> psycopg.Connection[tuple[Any, ...]]:
        return psycopg.connect(
            host=args.socket_dir,
            port=5432,
            user=args.user,
            password=os.environ["PGPASSWORD"],
            dbname=args.db,
            connect_timeout=3,
            application_name="afterstate-reference-app",
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "afterstate-reference-app"

        def log_message(self, fmt: str, *a: Any) -> None:  # silence default stderr logging
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            try:
                with connect() as conn:
                    if self.path == "/healthz":
                        conn.execute("SELECT 1")
                        self._send(200, {"status": "ok", "database": "reachable"})
                    elif self.path == "/api/customers/count":
                        row = conn.execute("SELECT count(*) FROM customers").fetchone()
                        self._send(200, {"customers": row[0] if row else 0})
                    elif self.path == "/api/orders/recent":
                        rows = conn.execute(
                            "SELECT id, status, total_cents FROM orders ORDER BY placed_at DESC, id DESC LIMIT 5"
                        ).fetchall()
                        self._send(200, {"orders": [{"id": r[0], "status": r[1], "total_cents": r[2]} for r in rows]})
                    elif self.path == "/api/ledger/summary":
                        row = conn.execute(
                            "SELECT coalesce(sum(CASE WHEN kind='charge' THEN amount_cents ELSE -amount_cents END),0) FROM billing.ledger_entries"
                        ).fetchone()
                        self._send(200, {"net_cents": int(row[0]) if row else 0})
                    else:
                        self._send(404, {"error": "not found"})
            except Exception as exc:  # the smoke test must see a 5xx, not a hung socket
                self._send(503, {"error": type(exc).__name__})

    return Handler


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--socket-dir", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--ready-file", required=True)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(args))
    Path(args.ready_file).write_text(str(server.server_address[1]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
