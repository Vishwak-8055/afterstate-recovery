"""Lexical gatekeeper for user-supplied validation SQL.

This is defence in depth and a usability feature (reject bad rules when they are saved).
The authoritative controls are server-side: the validator role is not a superuser, every query
runs inside a READ ONLY transaction with a statement timeout, and the restored database is a
disposable cluster reachable only through a private unix socket.
"""

from __future__ import annotations

import re

MAX_SQL_CHARS = 20_000

_FORBIDDEN_WORDS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|"
    r"lock|vacuum|analyze|reindex|cluster|refresh|set|reset|listen|notify|unlisten|prepare|"
    r"deallocate|discard|commit|rollback|begin|start|savepoint|release|abort|into|comment|"
    r"security|import|load|checkpoint|explain)\b",
    re.I,
)
_FORBIDDEN_FUNCS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_read_file|"
    r"pg_read_binary_file|pg_ls_\w+|pg_stat_file|lo_import|lo_export|lo_unlink|lo_create|dblink\w*|"
    r"set_config|pg_advisory\w*|nextval|setval|pg_create_restore_point|pg_switch_wal|"
    r"pg_logical\w*|pg_replication\w*|pg_file_\w+|query_to_xml\w*|xpath\w*|"
    r"pg_promote|pg_wal_replay\w*)\s*\(",
    re.I,
)
_LEADING = re.compile(r"^[\s(]*(select|with|values|table)\b", re.I)
_TAG = re.compile(r"\$([A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")


class UnsafeSql(ValueError):  # noqa: N818
    """Raised when SQL cannot be accepted as a read-only validation query."""


def _skeleton(sql: str) -> str:
    """Return the SQL with comments removed and every literal replaced by an empty literal."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise UnsafeSql("unterminated block comment")
            out.append(" ")
        elif c == "'":
            prev = out[-1] if out else ""
            before = out[-2] if len(out) > 1 else ""
            escape = prev in ("e", "E") and not (before.isalnum() or before == "_")
            i += 1
            while True:
                if i >= n:
                    raise UnsafeSql("unterminated string literal")
                ch = sql[i]
                if escape and ch == "\\":
                    i += 2
                    continue
                if ch == "'":
                    if sql.startswith("''", i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise UnsafeSql("unterminated quoted identifier")
                if sql[i] == '"':
                    if sql.startswith('""', i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append('""')
        elif c == "$" and (m := _TAG.match(sql, i)):
            tag = m.group(0)
            end = sql.find(tag, i + len(tag))
            if end == -1:
                raise UnsafeSql("unterminated dollar-quoted string")
            i = end + len(tag)
            out.append("''")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def validate_readonly_sql(sql: str) -> str:
    """Validate and normalise a single read-only statement. Returns SQL without a trailing ';'."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSql("SQL is empty")
    if len(sql) > MAX_SQL_CHARS:
        raise UnsafeSql(f"SQL is longer than {MAX_SQL_CHARS} characters")
    if "\x00" in sql:
        raise UnsafeSql("SQL contains a NUL byte")

    skeleton = _skeleton(sql).strip()
    while skeleton.endswith(";"):
        skeleton = skeleton[:-1].rstrip()
    if not skeleton:
        raise UnsafeSql("SQL is empty")
    if ";" in skeleton:
        raise UnsafeSql("only a single statement is allowed")
    if not _LEADING.match(skeleton):
        raise UnsafeSql("statement must start with SELECT, WITH, VALUES or TABLE")
    if (m := _FORBIDDEN_WORDS.search(skeleton)) is not None:
        raise UnsafeSql(f"keyword '{m.group(1).upper()}' is not allowed in a read-only validation query")
    if (m := _FORBIDDEN_FUNCS.search(skeleton)) is not None:
        raise UnsafeSql(f"function '{m.group(1)}' is not allowed in a validation query")

    normalised = sql.strip()
    while normalised.endswith(";"):
        normalised = normalised[:-1].rstrip()
    return normalised
