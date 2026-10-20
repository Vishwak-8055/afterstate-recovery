"""Artifact verification: existence, checksum, format, readability, compatibility, integrity.

Backups are untrusted input. Nothing in an archive is executed here: ``pg_restore --list`` only
prints the table of contents, and the deep scan writes the generated SQL script to /dev/null.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from afterstate.domain.checks import CheckResult
from afterstate.domain.enums import ArtifactFormat, FailureClass, Outcome
from afterstate.domain.failures import classify_tool_failure
from afterstate.engine.pgtools import PgTools
from afterstate.engine.process import base_env, run_command

_HEADER = {
    "dbname": re.compile(r"^;\s+dbname:\s*(.*)$"),
    "toc_entries": re.compile(r"^;\s+TOC Entries:\s*(\d+)"),
    "compression": re.compile(r"^;\s+Compression:\s*(.*)$"),
    "dump_format_version": re.compile(r"^;\s+Dump Version:\s*(.*)$"),
    "server_version": re.compile(r"^;\s+Dumped from database version:\s*(.*)$"),
    "tool_version": re.compile(r"^;\s+Dumped by pg_dump version:\s*(.*)$"),
    "created": re.compile(r"^;\s+Archive created at\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),
}

CHECK_KEYS: list[tuple[str, str]] = [
    ("artifact.exists", "Artifact exists and is within the size limit"),
    ("artifact.checksum", "SHA-256 checksum matches the recorded value"),
    ("artifact.format", "Backup format is supported"),
    ("artifact.readable", "Archive table of contents is readable"),
    ("artifact.version_compatible", "PostgreSQL version is compatible with the restore tooling"),
    ("artifact.integrity", "Archive can be read completely (deep scan)"),
]


@dataclass
class ArtifactInspection:
    path: str
    size_bytes: int | None = None
    sha256: str | None = None
    format: ArtifactFormat = ArtifactFormat.UNKNOWN
    supported: bool = False
    server_version: str | None = None
    tool_version: str | None = None
    source_database: str | None = None
    archive_created_at: datetime | None = None
    toc_entries: int | None = None
    compression: str | None = None
    dump_format_version: str | None = None
    deep_scanned: bool = False
    failure_class: FailureClass | None = None
    failure_message: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.failure_class is None

    def as_details(self) -> dict[str, object]:
        return {
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "format": self.format.value,
            "server_version": self.server_version,
            "tool_version": self.tool_version,
            "source_database": self.source_database,
            "archive_created_at": self.archive_created_at.isoformat() if self.archive_created_at else None,
            "toc_entries": self.toc_entries,
            "compression": self.compression,
            "dump_format_version": self.dump_format_version,
            "deep_scanned": self.deep_scanned,
        }


def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def sniff_format(path: Path) -> ArtifactFormat:
    with path.open("rb") as fh:
        head = fh.read(512)
    if head.startswith(b"PGDMP"):
        return ArtifactFormat.CUSTOM
    if head[257:262] == b"ustar":
        return ArtifactFormat.TAR
    if head.startswith(b"\x1f\x8b"):
        return ArtifactFormat.UNKNOWN  # gzip: most likely a compressed plain-SQL dump
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127)
    if (
        head
        and printable / len(head) > 0.95
        and (b"PostgreSQL database dump" in head or head.lstrip().startswith(b"--"))
    ):
        return ArtifactFormat.PLAIN_SQL
    return ArtifactFormat.UNKNOWN


def parse_toc_header(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(";"):
            if found:
                break
            continue
        for key, pattern in _HEADER.items():
            m = pattern.match(line)
            if m and key not in found:
                found[key] = m.group(1).strip()
    return found


def major_of(version_text: str | None) -> int | None:
    if not version_text:
        return None
    m = re.match(r"\s*(\d+)", version_text)
    return int(m.group(1)) if m else None


def inspect_artifact(
    path: Path,
    tools: PgTools,
    *,
    recorded_sha256: str | None,
    max_bytes: int,
    deep_scan: bool = True,
    cancel: threading.Event | None = None,
) -> ArtifactInspection:
    import time

    started = time.monotonic()
    insp = ArtifactInspection(path=str(path))
    results: dict[str, CheckResult] = {}

    def record(key: str, outcome: Outcome, message: str, expected: object = None, actual: object = None) -> None:
        name = dict(CHECK_KEYS)[key]
        results[key] = CheckResult(key, name, "artifact", outcome, True, message, expected, actual)

    def fail(
        key: str, klass: FailureClass, message: str, expected: object = None, actual: object = None
    ) -> ArtifactInspection:
        record(key, Outcome.FAIL, message, expected, actual)
        insp.failure_class, insp.failure_message = klass, message
        return finish()

    def finish() -> ArtifactInspection:
        blocker = next((k for k, _ in CHECK_KEYS if k in results and results[k].outcome is Outcome.FAIL), None)
        for key, name in CHECK_KEYS:
            if key not in results:
                results[key] = CheckResult(
                    key,
                    name,
                    "artifact",
                    Outcome.SKIPPED,
                    True,
                    f"Not evaluated because '{dict(CHECK_KEYS)[blocker]}' did not pass."
                    if blocker
                    else "Not evaluated.",
                )
        insp.checks = [results[k] for k, _ in CHECK_KEYS]
        insp.duration_s = time.monotonic() - started
        return insp

    # 1. existence and size
    if not path.is_file():
        return fail("artifact.exists", FailureClass.ARTIFACT_MISSING, "The artifact file is missing from storage.")
    size = path.stat().st_size
    insp.size_bytes = size
    if size == 0:
        return fail("artifact.exists", FailureClass.ARTIFACT_CORRUPT, "The artifact file is empty.", actual=0)
    if size > max_bytes:
        return fail(
            "artifact.exists",
            FailureClass.RESOURCE_LIMIT,
            f"The artifact is {size} bytes, above the {max_bytes} byte limit.",
            max_bytes,
            size,
        )
    record("artifact.exists", Outcome.PASS, f"{size} bytes present.", actual=size)

    # 2. checksum
    insp.sha256 = sha256_file(path)
    if recorded_sha256 and insp.sha256 != recorded_sha256.lower():
        return fail(
            "artifact.checksum",
            FailureClass.ARTIFACT_CHECKSUM_MISMATCH,
            "The file no longer matches the checksum recorded at registration; it was modified or corrupted.",
            recorded_sha256.lower(),
            insp.sha256,
        )
    record(
        "artifact.checksum",
        Outcome.PASS,
        "Matches the recorded checksum." if recorded_sha256 else "Checksum computed; no earlier value was recorded.",
        recorded_sha256,
        insp.sha256,
    )

    # 3. format
    insp.format = sniff_format(path)
    if insp.format is ArtifactFormat.PLAIN_SQL:
        return fail(
            "artifact.format",
            FailureClass.FORMAT_UNSUPPORTED,
            "Plain-SQL dumps are executable scripts processed by psql (which supports shell escapes), so they "
            "are not accepted. Use pg_dump -Fc (custom) or -Ft (tar).",
            ["custom", "tar"],
            insp.format.value,
        )
    if insp.format is ArtifactFormat.UNKNOWN:
        return fail(
            "artifact.format",
            FailureClass.FORMAT_UNSUPPORTED,
            "The file is not a recognised PostgreSQL archive. Supported: pg_dump custom (-Fc) and tar (-Ft).",
            ["custom", "tar"],
            "unknown",
        )
    insp.supported = True
    record("artifact.format", Outcome.PASS, f"{insp.format.value} archive.", ["custom", "tar"], insp.format.value)

    # 4. table of contents
    listing = run_command(
        [str(tools.pg_restore), "--list", str(path)],
        env=base_env(),
        timeout_s=120,
        cancel=cancel,
        tail_chars=2_000_000,
    )
    if not listing.ok:
        klass, reason = classify_tool_failure(
            returncode=listing.returncode,
            stderr=listing.stderr_tail,
            timed_out=listing.timed_out,
            cancelled=listing.cancelled,
        )
        if klass is FailureClass.RESTORE_FAILED:
            klass = FailureClass.ARTIFACT_CORRUPT
        return fail(
            "artifact.readable", klass, f"{reason} ({_one_line(listing.stderr_tail)})", actual=listing.returncode
        )
    header = parse_toc_header(listing.stdout_tail)
    insp.source_database = header.get("dbname")
    insp.server_version = header.get("server_version")
    insp.tool_version = header.get("tool_version")
    insp.compression = header.get("compression")
    insp.dump_format_version = header.get("dump_format_version")
    insp.toc_entries = int(header["toc_entries"]) if "toc_entries" in header else None
    if "created" in header:
        insp.archive_created_at = datetime.strptime(header["created"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    record("artifact.readable", Outcome.PASS, f"{insp.toc_entries} table-of-contents entries.", actual=insp.toc_entries)

    # 5. version compatibility
    dump_major = major_of(insp.server_version)
    if dump_major is not None and dump_major > tools.major:
        return fail(
            "artifact.version_compatible",
            FailureClass.VERSION_INCOMPATIBLE,
            f"The backup was taken on PostgreSQL {dump_major}; the restore environment runs {tools.major}. "
            "A restore target must be the same or a newer major version.",
            f"<= {tools.major}",
            dump_major,
        )
    record(
        "artifact.version_compatible",
        Outcome.PASS,
        f"Backup from PostgreSQL {dump_major or 'unknown'}, restoring on {tools.major}.",
        f"<= {tools.major}",
        dump_major,
    )

    # 6. deep scan: read and decompress every data block without any database
    if deep_scan:
        scan = run_command(
            [str(tools.pg_restore), "-f", "/dev/null", str(path)],
            env=base_env(),
            timeout_s=max(120.0, size / (8 * 1024 * 1024)),
            cancel=cancel,
        )
        insp.deep_scanned = True
        if not scan.ok:
            klass, reason = classify_tool_failure(
                returncode=scan.returncode, stderr=scan.stderr_tail, timed_out=scan.timed_out, cancelled=scan.cancelled
            )
            if klass is FailureClass.RESTORE_FAILED:
                klass = FailureClass.ARTIFACT_CORRUPT
            return fail(
                "artifact.integrity", klass, f"{reason} ({_one_line(scan.stderr_tail)})", actual=scan.returncode
            )
        record("artifact.integrity", Outcome.PASS, "Every data block was read and decompressed successfully.")
    else:
        record("artifact.integrity", Outcome.SKIPPED, "Deep scan was not requested.")
    return finish()


def _one_line(text: str, limit: int = 240) -> str:
    flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat[:limit] if flat else "no diagnostic output"
