"""Command line entry point: ``afterstate <command>``.

migrate             apply database migrations
api                 run the HTTP API (uvicorn)
worker              run the recovery worker
dataset-checksum    compute the expected value for a 'reference dataset checksum' rule from a source database
demo                start a throwaway platform database, the API and a worker (no Docker) for local exploration
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from pathlib import Path

from alembic.config import Config


def _alembic_config() -> Config:

    for base in (Path(__file__).resolve().parents[1], Path.cwd()):
        ini = base / "alembic.ini"
        if ini.is_file():
            cfg = Config(str(ini))
            cfg.set_main_option("script_location", str(base / "alembic"))
            return cfg
    raise SystemExit("alembic.ini not found; run from the backend directory")


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    print("database is at the latest migration")
    return 0


def cmd_dataset_checksum(args: argparse.Namespace) -> int:
    import psycopg

    from afterstate.domain.sqlsafety import UnsafeSql, validate_readonly_sql
    from afterstate.engine.dataset import dataset_checksum

    dsn = args.dsn or os.environ.get("AFTERSTATE_SOURCE_DSN")
    if not dsn:
        print(
            "error: provide --dsn or set AFTERSTATE_SOURCE_DSN (a libpq connection string for the SOURCE database)",
            file=sys.stderr,
        )
        return 2
    sql = Path(args.sql_file).read_text() if args.sql_file else args.sql
    if not sql:
        print("error: provide --sql or --sql-file", file=sys.stderr)
        return 2
    try:
        sql = validate_readonly_sql(sql)
    except UnsafeSql as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if "order by" not in sql.lower():
        print(
            "warning: the query has no ORDER BY; row order, and therefore the checksum, is not guaranteed to be stable",
            file=sys.stderr,
        )
    with psycopg.connect(
        dsn, options="-c default_transaction_read_only=on -c statement_timeout=600000 -c standard_conforming_strings=on"
    ) as conn:
        conn.read_only = True
        digest, rows = dataset_checksum(conn, sql)
    print(json.dumps({"expected_sha256": digest, "expected_rows": rows}, indent=2))
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("afterstate.api.app:app", host=args.host, port=args.port, log_config=None, proxy_headers=False)
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    from afterstate.worker.__main__ import main as worker_main

    sys.argv = ["afterstate.worker", "run"]
    worker_main()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Everything in one process tree, using local PostgreSQL binaries. For exploration only."""
    import signal

    from afterstate.devcluster import PlatformCluster

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # lets `finally` stop the throwaway database
    cluster = PlatformCluster()
    url = cluster.start()
    base = Path(cluster.base)
    os.environ.update(
        AFTERSTATE_DATABASE_URL=url,
        AFTERSTATE_ARTIFACT_DIR=str(base / "artifacts"),
        AFTERSTATE_SCRATCH_DIR=f"/tmp/afs-demo-{secrets.token_hex(3)}",  # noqa: S108 - throwaway demo scratch, unguessable suffix
        AFTERSTATE_LOG_JSON="false",
    )
    try:
        cmd_migrate(args)
        from afterstate.api.deps import get_services
        from afterstate.config import reset_settings_cache
        from afterstate.db.models import Membership, User, Workspace
        from afterstate.domain.enums import Role
        from afterstate.security.passwords import hash_password

        reset_settings_cache()
        password = secrets.token_urlsafe(12)
        svc = get_services()
        with svc.sf() as s, s.begin():
            user = User(
                email="demo@afterstate.local", display_name="Demo Operator", password_hash=hash_password(password)
            )
            ws = Workspace(name="Demo Workspace", slug="demo")
            s.add_all([user, ws])
            s.flush()
            s.add(Membership(workspace_id=ws.id, user_id=user.id, role=Role.OWNER))
        print(
            f"\n  AFTERSTATE demo\n  API      http://127.0.0.1:{args.port}/api/docs\n  Sign in  demo@afterstate.local\n  Password {password}\n  Open the web app (npm run dev in ./web) and try the Recovery Lab.\n",
            flush=True,
        )
        from afterstate.worker.runner import Worker

        stop = threading.Event()
        threading.Thread(target=Worker(svc).run_forever, args=(stop,), daemon=True).start()
        import uvicorn

        uvicorn.run("afterstate.api.app:app", host="127.0.0.1", port=args.port, log_level="warning")
        stop.set()
    finally:
        cluster.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="afterstate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    api = sub.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.set_defaults(func=cmd_api)
    sub.add_parser("worker").set_defaults(func=cmd_worker)
    ds = sub.add_parser(
        "dataset-checksum", help="compute expected_sha256 / expected_rows for a rule from a source database"
    )
    ds.add_argument("--dsn")
    ds.add_argument("--sql")
    ds.add_argument("--sql-file")
    ds.set_defaults(func=cmd_dataset_checksum)
    demo = sub.add_parser("demo")
    demo.add_argument("--port", type=int, default=8000)
    demo.set_defaults(func=cmd_demo)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
