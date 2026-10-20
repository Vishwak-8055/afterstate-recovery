"""Controlled pg_restore execution.

The restore always runs as the unprivileged restore role against the disposable cluster, with
``--exit-on-error`` (a partially restored database is never presented as a recovery), and with a
table-of-contents policy that skips object classes needing superuser or reaching outside the
database (event triggers, foreign servers, replication objects, procedural languages).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from afterstate.domain.enums import FailureClass
from afterstate.domain.failures import classify_tool_failure
from afterstate.engine.environment import RestoreTarget
from afterstate.engine.pgtools import PgTools
from afterstate.engine.process import CommandResult, base_env, run_command
from afterstate.security.redact import Redactor

_ENTRY = re.compile(r"^(\d+);\s+(\d+)\s+(\d+)\s+(.*)$")
_PROGRESS_SERIAL = re.compile(r"^pg_restore: (creating|processing data for table|executing)\b")
_PROGRESS_PARALLEL = re.compile(r"^pg_restore: finished item\b")

#: Object classes the restore policy skips. Each entry is matched as a prefix of the TOC description.
SKIPPED_CLASSES: tuple[str, ...] = (
    "EVENT TRIGGER",
    "PUBLICATION",
    "SUBSCRIPTION",
    "FOREIGN DATA WRAPPER",
    "FOREIGN TABLE",
    "SERVER",
    "USER MAPPING",
    "PROCEDURAL LANGUAGE",
    "ACL",
    "DEFAULT ACL",
    "COMMENT - EXTENSION plpgsql",
    "EXTENSION - plpgsql",
    "COMMENT - PROCEDURAL LANGUAGE",
)


@dataclass
class FilteredToc:
    text: str
    total: int
    selected: int
    skipped: list[str] = field(default_factory=list)
    skipped_count: int = 0


def filter_toc(toc_listing: str) -> FilteredToc:
    out: list[str] = []
    selected = total = skipped_count = 0
    skipped: list[str] = []
    for line in toc_listing.splitlines():
        m = _ENTRY.match(line)
        if not m:
            out.append(line)
            continue
        total += 1
        description = m.group(4)
        if any(description == c or description.startswith(c + " ") for c in SKIPPED_CLASSES):
            out.append(";" + line)
            skipped_count += 1
            if len(skipped) < 50:
                skipped.append(description[:120])
        else:
            out.append(line)
            selected += 1
    return FilteredToc("\n".join(out) + "\n", total, selected, skipped, skipped_count)


@dataclass
class RestoreResult:
    command: CommandResult | None
    items_total: int
    items_done: int
    skipped_count: int
    skipped: list[str]
    failure_class: FailureClass | None = None
    failure_message: str | None = None
    duration_s: float = 0.0
    argv_display: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failure_class is None


def run_restore(
    *,
    tools: PgTools,
    artifact_path: Path,
    target: RestoreTarget,
    workdir: Path,
    jobs: int,
    timeout_s: float,
    cancel: threading.Event,
    redactor: Redactor,
    emit: Callable[[str, str], None],
    on_progress: Callable[[int, int], None],
    watch: Callable[[], str | None] | None = None,
) -> RestoreResult:
    env = base_env({"PGPASSWORD": target.password})

    listing = run_command(
        [str(tools.pg_restore), "--list", str(artifact_path)],
        env=env,
        timeout_s=120,
        cancel=cancel,
        tail_chars=4_000_000,
    )
    if not listing.ok:
        klass, reason = classify_tool_failure(
            returncode=listing.returncode,
            stderr=listing.stderr_tail,
            timed_out=listing.timed_out,
            cancelled=listing.cancelled,
        )
        return RestoreResult(listing, 0, 0, 0, [], klass, reason)
    toc = filter_toc(listing.stdout_tail)
    toc_path = workdir / "restore.toc"
    toc_path.write_text(toc.text)
    if toc.skipped_count:
        emit(
            "WARN",
            f"restore policy skipped {toc.skipped_count} object(s): "
            + "; ".join(toc.skipped[:5])
            + (" ..." if toc.skipped_count > 5 else ""),
        )

    argv = [
        str(tools.pg_restore),
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.user,
        "--dbname",
        target.database,
        "--no-password",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--verbose",
        "--use-list",
        str(toc_path),
    ]
    if jobs > 1:
        argv.append(f"--jobs={jobs}")
    argv.append(str(artifact_path))
    display = list(argv)

    done = 0
    pattern = _PROGRESS_PARALLEL if jobs > 1 else _PROGRESS_SERIAL

    def on_line(stream: str, line: str) -> None:
        nonlocal done
        if pattern.match(line):
            done += 1
            on_progress(done, toc.selected)
        level = "ERROR" if "error" in line.lower() and "pg_restore" in line else "INFO"
        emit(level, line)

    result = run_command(
        argv, env=env, timeout_s=timeout_s, cancel=cancel, on_line=on_line, watch=watch, redactor=redactor
    )
    on_progress(done, toc.selected)
    out = RestoreResult(
        result, toc.selected, done, toc.skipped_count, toc.skipped, duration_s=result.duration_s, argv_display=display
    )
    if not result.ok:
        klass, reason = classify_tool_failure(
            returncode=result.returncode,
            stderr=result.stderr_tail,
            timed_out=result.timed_out,
            limit_exceeded=result.killed_reason == "disk_limit",
            cancelled=result.cancelled,
        )
        out.failure_class, out.failure_message = klass, reason
    return out
