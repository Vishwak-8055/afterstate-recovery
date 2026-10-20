"""Hardened subprocess execution.

* argv lists only: no shell, so no interpolation of untrusted values into commands;
* explicit minimal environment: platform secrets are never inherited by children;
* own session / process group: timeouts and cancellation kill the whole tree;
* streamed, redacted, size-bounded output capture;
* core dumps disabled (a crashing tool must not write backup contents to disk).
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from afterstate.security.redact import Redactor

MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int | None
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    killed_reason: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_lines: int = 0
    stderr_lines: int = 0
    pid: int | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled and not self.killed_reason


def base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": MINIMAL_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PGCONNECT_TIMEOUT": "10"}
    if extra:
        env.update(extra)
    return env


def kill_process_group(pid: int, *, grace_s: float = 2.0) -> None:
    """SIGTERM the group, wait briefly, then SIGKILL. Never raises for already-dead processes."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    cwd: str | Path | None = None,
    cancel: threading.Event | None = None,
    on_line: Callable[[str, str], None] | None = None,
    watch: Callable[[], str | None] | None = None,
    watch_interval_s: float = 1.0,
    redactor: Redactor | None = None,
    tail_chars: int = 16_000,
) -> CommandResult:
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError("executable must be an absolute path")
    if any((not isinstance(a, str)) or "\x00" in a for a in argv):
        raise ValueError("argv must be NUL-free strings")

    started = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
        close_fds=True,
    )
    with contextlib.suppress(ProcessLookupError, OSError):
        resource.prlimit(proc.pid, resource.RLIMIT_CORE, (0, 0))

    tails: dict[str, deque[str]] = {"stdout": deque(), "stderr": deque()}
    sizes = {"stdout": 0, "stderr": 0}
    counts = {"stdout": 0, "stderr": 0}

    def pump(stream_name: str, stream: object) -> None:
        readline = stream.readline  # type: ignore[attr-defined]
        for raw in iter(readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if redactor:
                line = redactor.apply(line)
            counts[stream_name] += 1
            tails[stream_name].append(line)
            sizes[stream_name] += len(line) + 1
            while sizes[stream_name] > tail_chars and len(tails[stream_name]) > 1:
                sizes[stream_name] -= len(tails[stream_name].popleft()) + 1
            if on_line:
                with contextlib.suppress(Exception):
                    on_line(stream_name, line)

    threads = [
        threading.Thread(target=pump, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", proc.stderr), daemon=True),
    ]
    for t in threads:
        t.start()

    result = CommandResult(argv=list(argv), returncode=None, duration_s=0.0, pid=proc.pid)
    deadline = started + timeout_s
    next_watch = started + watch_interval_s
    while proc.poll() is None:
        now = time.monotonic()
        reason: str | None = None
        if now >= deadline:
            result.timed_out = True
            reason = "timeout"
        elif cancel is not None and cancel.is_set():
            result.cancelled = True
            reason = "cancelled"
        elif watch is not None and now >= next_watch:
            next_watch = now + watch_interval_s
            reason = watch()
            if reason:
                result.killed_reason = reason
        if reason:
            kill_process_group(proc.pid)
            break
        time.sleep(0.05)

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    # The tool may have exited while a grandchild still holds the pipes; make sure nothing lingers.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    for t in threads:
        t.join(timeout=5)

    result.returncode = proc.returncode
    result.duration_s = time.monotonic() - started
    result.stdout_tail = "\n".join(tails["stdout"])
    result.stderr_tail = "\n".join(tails["stderr"])
    result.stdout_lines, result.stderr_lines = counts["stdout"], counts["stderr"]
    return result
