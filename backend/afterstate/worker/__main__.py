from __future__ import annotations

import argparse
import signal
import threading
import uuid

from afterstate.config import get_settings
from afterstate.logging import configure_logging
from afterstate.services import build_services
from afterstate.worker.runner import Worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="afterstate.worker")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the worker loop (default)")
    sub.add_parser("once", help="process all queued runs, run one maintenance pass, then exit")
    ex = sub.add_parser("execute", help="execute one already-claimed run (used by the Recovery Lab)")
    ex.add_argument("--run-id", required=True)
    ex.add_argument("--owner", required=True)
    ex.add_argument("--lease-seconds", type=int, default=60)
    ex.add_argument("--heartbeat-seconds", type=float, default=10.0)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)
    worker = Worker(build_services(settings))
    cmd = args.cmd or "run"
    if cmd == "execute":
        worker.execute(
            uuid.UUID(args.run_id),
            owner=args.owner,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
        )
    elif cmd == "once":
        worker.maintenance_once()
        worker.drain()
    else:
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        worker.run_forever(stop)


if __name__ == "__main__":
    main()
