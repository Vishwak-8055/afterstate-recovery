"""Disposable PostgreSQL restore environments.

Each environment is a private cluster owned by exactly one recovery run:

* lives in its own 0700 directory under the scratch root, marked with the owning run id;
* has NO network listener (listen_addresses=''); the only door is a unix socket inside that
  directory, so no other host or container can reach it;
* the backup is restored by an unprivileged role (not a superuser), so SQL embedded in a hostile
  archive cannot run programs, read server files, create untrusted extensions or event triggers;
* validation uses a separate read-only role;
* every OS resource is recorded as a cleanup task before it is created, and destruction is
  idempotent and verified.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql

from afterstate.engine.pgtools import PgTools
from afterstate.engine.process import base_env, kill_process_group, run_command
from afterstate.security.redact import Redactor

MARKER = ".afterstate-env"
ADMIN_USER = "afterstate_admin"
RESTORE_USER = "afterstate_restore"
VALIDATOR_USER = "afterstate_validator"
DB_NAME = "restored"
PORT = 5432


class EnvironmentError_(RuntimeError):  # noqa: N801
    """The disposable database could not be created or started."""


class UnsafeCleanup(RuntimeError):
    """Refused to delete a path that is not a verified AFTERSTATE environment."""


@dataclass(frozen=True)
class RestoreTarget:
    host: str
    port: int
    database: str
    user: str
    password: str


def env_dir_for(scratch_root: Path, run_id: UUID) -> Path:
    return scratch_root / f"e-{run_id.hex[:12]}"


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _cmdline_mentions(pid: int, needle: str) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return needle in raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie is dead for our purposes
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class LocalClusterEnvironment:
    def __init__(
        self,
        scratch_root: Path,
        run_id: UUID,
        tools: PgTools,
        *,
        shared_buffers_mb: int = 128,
        redactor: Redactor | None = None,
    ) -> None:
        self.scratch_root = Path(scratch_root)
        self.run_id = run_id
        self.tools = tools
        self.shared_buffers_mb = shared_buffers_mb
        self.redactor = redactor or Redactor()
        self.path = env_dir_for(self.scratch_root, run_id)
        self.pgdata = self.path / "pgdata"
        self.sockdir = self.path / "s"
        self.restore_password = secrets.token_urlsafe(24)
        self.validator_password = secrets.token_urlsafe(24)
        self.redactor.add(self.restore_password)
        self.redactor.add(self.validator_password)
        self._proc: subprocess.Popen[bytes] | None = None

    # ---- lifecycle -------------------------------------------------------------------------
    def create(self) -> None:
        sock = self.sockdir / f".s.PGSQL.{PORT}"
        if len(str(sock)) > 100:
            raise EnvironmentError_(
                "scratch directory path is too long for a PostgreSQL unix socket; use a shorter AFTERSTATE_SCRATCH_DIR"
            )
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.mkdir(mode=0o700)
        self.sockdir.mkdir(mode=0o700)
        (self.path / MARKER).write_text(
            json.dumps(
                {"run_id": str(self.run_id), "created_at": datetime.now(UTC).isoformat(), "owner_pid": os.getpid()}
            )
        )
        initdb_args = [
            str(self.tools.initdb),
            "-D",
            str(self.pgdata),
            "-U",
            ADMIN_USER,
            "--auth-local=trust",
            "--auth-host=reject",
            "-E",
            "UTF8",
            "--no-instructions",
            "--locale=C.UTF-8",
        ]
        res = run_command(initdb_args, env=base_env(), timeout_s=120, redactor=self.redactor)
        if not res.ok:
            shutil.rmtree(self.pgdata, ignore_errors=True)
            initdb_args[-1] = "--locale=C"
            res = run_command(initdb_args, env=base_env(), timeout_s=120, redactor=self.redactor)
        if not res.ok:
            raise EnvironmentError_(f"initdb failed (rc={res.returncode}): {res.stderr_tail[-400:]}")
        (self.pgdata / "pg_hba.conf").write_text(
            f"local all {ADMIN_USER} trust\nlocal all all scram-sha-256\nhost all all all reject\n"
        )
        with (self.pgdata / "postgresql.conf").open("a") as conf:
            conf.write(
                "\n# --- AFTERSTATE disposable restore environment ---\n"
                "listen_addresses = ''\n"
                f"unix_socket_directories = '{self.sockdir}'\n"
                "unix_socket_permissions = 0700\n"
                f"port = {PORT}\n"
                "max_connections = 20\n"
                f"shared_buffers = {self.shared_buffers_mb}MB\n"
                "dynamic_shared_memory_type = mmap\n"
                "huge_pages = off\n"
                "fsync = off\nsynchronous_commit = off\nfull_page_writes = off\n"
                "wal_level = minimal\nmax_wal_senders = 0\narchive_mode = off\n"
                "max_wal_size = 2GB\ncheckpoint_timeout = 30min\n"
                "autovacuum = off\n"
                "max_locks_per_transaction = 256\n"
                "temp_file_limit = 2GB\n"
                "password_encryption = scram-sha-256\n"
                "ssl = off\nshared_preload_libraries = ''\n"
                "log_min_messages = warning\nlog_line_prefix = '%m [%p] '\n"
            )

    def start(self) -> None:
        log = (self.path / "postgres.log").open("ab")
        try:
            self._proc = subprocess.Popen(
                [str(self.tools.postgres), "-D", str(self.pgdata)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=base_env(),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        (self.path / "postmaster.owner").write_text(str(self._proc.pid))
        import resource

        with contextlib.suppress(ProcessLookupError, OSError):
            resource.prlimit(self._proc.pid, resource.RLIMIT_CORE, (0, 0))
        deadline = time.monotonic() + 90
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise EnvironmentError_(
                    f"postgres exited during startup (rc={self._proc.returncode}): {self._log_tail()}"
                )
            try:
                with self.admin_conn("postgres", timeout=2):
                    break
            except psycopg.OperationalError as exc:
                last = exc
                time.sleep(0.1)
        else:
            raise EnvironmentError_(f"postgres did not become ready in time: {last}")
        self._provision()

    def _provision(self) -> None:
        with self.admin_conn("postgres") as conn:

            def ident(n: str) -> sql.Identifier:
                return sql.Identifier(n)

            role_opts = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
            conn.execute(
                sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                    ident(RESTORE_USER), role_opts, sql.Literal(self.restore_password)
                )
            )
            conn.execute(
                sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                    ident(VALIDATOR_USER), role_opts, sql.Literal(self.validator_password)
                )
            )
            conn.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(ident(VALIDATOR_USER)))
            conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident(VALIDATOR_USER)))
            conn.execute(
                sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '120s'").format(ident(VALIDATOR_USER))
            )
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    ident(DB_NAME), ident(RESTORE_USER)
                )
            )
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident(DB_NAME)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident(DB_NAME), ident(VALIDATOR_USER)))

    # ---- connections -----------------------------------------------------------------------
    def admin_conn(self, dbname: str = DB_NAME, *, timeout: int = 5) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(
            host=str(self.sockdir), port=PORT, user=ADMIN_USER, dbname=dbname, connect_timeout=timeout, autocommit=True
        )

    def validator_conn(self, *, timeout: int = 5) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(
            host=str(self.sockdir),
            port=PORT,
            user=VALIDATOR_USER,
            password=self.validator_password,
            dbname=DB_NAME,
            connect_timeout=timeout,
            application_name="afterstate-validator",
            # A hostile backup can ALTER DATABASE ... SET to change session defaults. Startup options outrank
            # database-level settings, so the validator's view of strings, search path and writes is fixed.
            options="-c standard_conforming_strings=on -c search_path=public -c default_transaction_read_only=on -c row_security=on",
        )

    def restore_target(self) -> RestoreTarget:
        return RestoreTarget(str(self.sockdir), PORT, DB_NAME, RESTORE_USER, self.restore_password)

    # ---- introspection ---------------------------------------------------------------------
    def disk_usage_bytes(self) -> int:
        total = 0
        stack = [str(self.path)]
        while stack:
            with contextlib.suppress(OSError), os.scandir(stack.pop()) as it:
                for entry in it:
                    with contextlib.suppress(OSError):
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
        return total

    def database_size_bytes(self) -> int | None:
        try:
            with self.admin_conn("postgres") as conn:
                row = conn.execute("SELECT pg_database_size(%s)", (DB_NAME,)).fetchone()
                return int(row[0]) if row else None  # type: ignore[call-overload]
        except psycopg.Error:
            return None

    def postmaster_pid(self) -> int | None:
        return self._proc.pid if self._proc else _read_pid(self.pgdata / "postmaster.pid")

    def postmaster_alive(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        pid = self.postmaster_pid()
        return bool(pid and _pid_alive(pid))

    def kill_postmaster(self) -> None:
        """SIGKILL the whole PostgreSQL process group (used for fault injection and cleanup)."""
        pid = self.postmaster_pid()
        if pid:
            import signal

            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            if self._proc is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._proc.wait(timeout=10)

    def _log_tail(self, n: int = 600) -> str:
        try:
            return self.redactor.apply((self.path / "postgres.log").read_text(errors="replace")[-n:])
        except OSError:
            return ""

    def describe(self) -> dict[str, object]:
        return {
            "kind": "local_cluster",
            "network": "none (unix socket only, 0700 directory)",
            "restore_role": {"name": RESTORE_USER, "superuser": False},
            "validator_role": {"name": VALIDATOR_USER, "superuser": False, "read_only": True},
            "postgresql": self.tools.version,
            "run_id": str(self.run_id),
        }

    # ---- teardown --------------------------------------------------------------------------
    def destroy(self) -> list[str]:
        self._proc = None
        return self.destroy_path(self.path, self.scratch_root, expect_run_id=self.run_id)

    @staticmethod
    def destroy_path(path: Path, scratch_root: Path, *, expect_run_id: UUID | None = None) -> list[str]:
        """Idempotent, verified teardown by path. Refuses anything that is not a marked environment
        directly beneath the scratch root, so a tampered cleanup record cannot delete arbitrary files."""
        root = Path(scratch_root).resolve()
        target = Path(path)
        if target.is_symlink() or target.resolve().parent != root:
            raise UnsafeCleanup(f"{path} is not directly inside the scratch root")
        notes: list[str] = []
        if not target.exists():
            return ["environment directory already removed"]
        marker = target / MARKER
        try:
            meta = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise UnsafeCleanup(f"{path} has no valid ownership marker") from exc
        if expect_run_id is not None and meta.get("run_id") != str(expect_run_id):
            raise UnsafeCleanup("environment is owned by a different run")

        candidates = [
            (_read_pid(target / "pgdata" / "postmaster.pid"), "postgres"),
            (_read_pid(target / "postmaster.owner"), "postgres"),
            (_read_pid(target / "app.pid"), "application"),
        ]
        seen: set[int] = set()
        for pid, label in candidates:
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            if _pid_alive(pid) and _cmdline_mentions(pid, str(target)):
                kill_process_group(pid, grace_s=3.0)
                deadline = time.monotonic() + 10
                while _pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _pid_alive(pid):
                    raise UnsafeCleanup(f"{label} process {pid} did not exit")
                notes.append(f"terminated {label} process {pid}")
        shutil.rmtree(target, ignore_errors=False)
        if target.exists():
            raise UnsafeCleanup(f"{path} still exists after removal")
        notes.append("removed environment directory")
        return notes
