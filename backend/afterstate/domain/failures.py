"""Failure classification from tool output and exit status."""

from __future__ import annotations

import re
import signal

from afterstate.domain.enums import FailureClass

_CORRUPT = re.compile(
    r"could not read from input file|could not uncompress|incorrect data check|"
    r"input file is too short|does not appear to be a valid archive|"
    r"unexpected end of file|invalid compressed data|corrupt|unexpected block ID|"
    r"could not find block ID|expected \d+ bytes|file offset|invalid.*header",
    re.I,
)
_VERSION = re.compile(r"unsupported version \([\d.]+\) in file header|unsupported .* version", re.I)
_CONNECTION = re.compile(
    r"connection to server .* failed|could not connect|server closed the connection|"
    r"terminating connection|no such file or directory.*\.s\.PGSQL|connection refused|"
    r"the database system is (starting up|shutting down)",
    re.I,
)
_PRIVILEGE = re.compile(r"permission denied|must be superuser|must be owner", re.I)


def classify_tool_failure(
    *,
    returncode: int | None,
    stderr: str,
    timed_out: bool = False,
    limit_exceeded: bool = False,
    cancelled: bool = False,
) -> tuple[FailureClass, str]:
    """Map a failed pg_restore invocation to (class, human readable reason)."""
    if cancelled:
        return FailureClass.CANCELLED, "Run was cancelled while the tool was executing."
    if timed_out:
        return FailureClass.RESTORE_TIMEOUT, "The restore exceeded its configured time limit and was terminated."
    if limit_exceeded:
        return (
            FailureClass.RESOURCE_LIMIT,
            "The restore exceeded the disk limit for its environment and was terminated.",
        )
    if returncode is not None and (returncode < 0 or returncode in (128 + signal.SIGSEGV, 128 + signal.SIGABRT)):
        sig = -returncode if returncode < 0 else returncode - 128
        return (
            FailureClass.ARTIFACT_CORRUPT,
            f"pg_restore crashed (signal {sig}) while reading the archive; the file is malformed or hostile.",
        )
    if _VERSION.search(stderr):
        return (
            FailureClass.VERSION_INCOMPATIBLE,
            "The archive was written by a newer pg_dump than the restore tooling supports.",
        )
    if _CORRUPT.search(stderr):
        return FailureClass.ARTIFACT_CORRUPT, "The archive is truncated or corrupt and cannot be read completely."
    if _CONNECTION.search(stderr):
        return FailureClass.ENVIRONMENT_UNAVAILABLE, "The disposable database became unavailable during restore."
    if _PRIVILEGE.search(stderr):
        return (
            FailureClass.RESTORE_FAILED,
            "Restore needs privileges the unprivileged restore role does not have "
            "(for example an untrusted extension or superuser-only object).",
        )
    return FailureClass.RESTORE_FAILED, "pg_restore reported an error and stopped."
