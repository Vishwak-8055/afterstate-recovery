"""Starts a predefined application suite against the restored database, inside the environment."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from dataclasses import dataclass

from afterstate.engine.environment import DB_NAME, VALIDATOR_USER, LocalClusterEnvironment
from afterstate.engine.process import base_env, kill_process_group

SUITES: dict[str, str] = {
    "reference-shop": "Read-only reference web service over the shop demo schema (customers, orders, billing ledger).",
}


class AppStartError(RuntimeError):
    pass


@dataclass
class RunningApp:
    pid: int
    port: int
    host: str = "127.0.0.1"


class AppRunner:
    def __init__(self, env: LocalClusterEnvironment, suite: str) -> None:
        if suite not in SUITES:
            raise AppStartError(f"unknown application suite '{suite}'")
        self.env = env
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self, timeout_s: float = 20.0) -> RunningApp:
        ready = self.env.path / "app.port"
        ready.unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "afterstate.engine.demoapp",
                "--socket-dir",
                str(self.env.sockdir),
                "--db",
                DB_NAME,
                "--user",
                VALIDATOR_USER,
                "--ready-file",
                str(ready),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=base_env({"PGPASSWORD": self.env.validator_password}),
            start_new_session=True,
            close_fds=True,
        )
        (self.env.path / "app.pid").write_text(str(self.proc.pid))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AppStartError(f"application exited during startup (rc={self.proc.returncode})")
            if ready.exists() and ready.read_text().strip().isdigit():
                return RunningApp(self.proc.pid, int(ready.read_text().strip()))
            time.sleep(0.05)
        self.stop()
        raise AppStartError("application did not become ready in time")

    def stop(self) -> None:
        if self.proc is not None:
            kill_process_group(self.proc.pid, grace_s=2.0)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
            self.proc = None


#: Checks a suite brings with it when a project selects the suite but defines no HTTP rules itself.
DEFAULT_SUITE_CHECKS: dict[str, list[dict[str, object]]] = {
    "reference-shop": [
        {
            "kind": "http_check",
            "name": "Application health endpoint",
            "path": "/healthz",
            "expected_status": 200,
            "max_latency_ms": 2000,
            "body_contains": "reachable",
        },
        {
            "kind": "http_check",
            "name": "Customer count endpoint",
            "path": "/api/customers/count",
            "expected_status": 200,
            "max_latency_ms": 2000,
            "body_contains": "customers",
        },
        {
            "kind": "http_check",
            "name": "Recent orders endpoint",
            "path": "/api/orders/recent",
            "expected_status": 200,
            "max_latency_ms": 2000,
            "body_contains": "orders",
        },
        {
            "kind": "http_check",
            "name": "Ledger summary endpoint",
            "path": "/api/ledger/summary",
            "expected_status": 200,
            "max_latency_ms": 2000,
            "body_contains": "net_cents",
        },
    ]
}
