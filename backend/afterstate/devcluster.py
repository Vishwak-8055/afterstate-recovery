"""Throwaway PostgreSQL cluster for the *platform* database (tests, CI-less dev, the demo CLI).

Production uses a managed or containerised PostgreSQL; this exists so the full stack can be run
and tested anywhere PostgreSQL binaries are installed, without Docker. Unix socket only."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg

from afterstate.engine.pgtools import PgTools, locate_pg_tools
from afterstate.engine.process import base_env, run_command


class PlatformCluster:
    def __init__(
        self, tools: PgTools | None = None, *, base_dir: Path | None = None, database: str = "afterstate"
    ) -> None:
        self.tools = tools or locate_pg_tools()
        self.base = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="afs-pg-", dir="/tmp"))
        self.data = self.base / "data"
        self.sock = self.base / "s"
        self.database = database
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"postgresql+psycopg://postgres@/{self.database}?host={self.sock}"

    def start(self) -> str:
        self.sock.mkdir(parents=True, exist_ok=True, mode=0o700)
        res = run_command(
            [
                str(self.tools.initdb),
                "-D",
                str(self.data),
                "-U",
                "postgres",
                "--auth=trust",
                "-E",
                "UTF8",
                "--locale=C.UTF-8",
                "--no-instructions",
            ],
            env=base_env(),
            timeout_s=120,
        )
        if not res.ok:
            raise RuntimeError(f"initdb failed: {res.stderr_tail}")
        with (self.data / "postgresql.conf").open("a") as fh:
            fh.write(
                f"\nlisten_addresses = ''\nunix_socket_directories = '{self.sock}'\nfsync = off\nsynchronous_commit = off\n"
                "full_page_writes = off\nmax_connections = 60\nshared_buffers = 64MB\nlog_min_messages = warning\n"
            )
        log = (self.base / "postgres.log").open("ab")
        self._proc = subprocess.Popen(
            [str(self.tools.postgres), "-D", str(self.data)],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            env=base_env(),
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("platform postgres exited during startup")
            try:
                with psycopg.connect(
                    host=str(self.sock), user="postgres", dbname="postgres", autocommit=True, connect_timeout=2
                ) as conn:
                    conn.execute(f'CREATE DATABASE "{self.database}"')
                return self.url
            except psycopg.OperationalError:
                time.sleep(0.1)
        raise RuntimeError("platform postgres did not start in time")

    def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                import os
                import signal

                os.killpg(self._proc.pid, signal.SIGINT)  # fast shutdown
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=15)
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc = None
        shutil.rmtree(self.base, ignore_errors=True)
