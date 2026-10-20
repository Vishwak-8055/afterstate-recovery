"""Secret and sensitive-content redaction applied to logs, events, reports and audit details.

PostgreSQL tools echo row data in error messages (``CONTEXT: COPY customers, line 3: "..."``).
Backups are untrusted and may hold personal data, so that text must never reach logs or the UI.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s@]+:)([^@\s]+)(@)", re.I), r"\1[REDACTED]\3"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]+=*"), r"\1 [REDACTED]"),
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie)[A-Za-z0-9_\-]*)"
            r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+(?:\s+[A-Za-z0-9._~+/\-]+=*)?)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # PostgreSQL error text that echoes record contents
    (re.compile(r'(CONTEXT:\s+COPY\s+\S+,\s+line\s+\d+:\s*)".*', re.I), r'\1"[REDACTED ROW DATA]"'),
    (re.compile(r"(Key \([^)]*\)=)\(.*?\)"), r"\1([REDACTED])"),
    (re.compile(r"(Failing row contains )\(.*\)"), r"\1([REDACTED])"),
    (re.compile(r"(Command was: )((?:INSERT|UPDATE|DELETE)\b).*", re.I), r"\1\2 [STATEMENT REDACTED]"),
]

_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|csrf|webhook_url|signature)",
    re.I,
)


class Redactor:
    """Redacts patterns plus exact secret values known at runtime (e.g. generated passwords)."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        self._known: set[str] = {s for s in known_secrets if s and len(s) >= 6}

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 6:
            self._known.add(secret)

    def apply(self, text: str) -> str:
        for secret in self._known:
            text = text.replace(secret, REDACTED)
        return redact_text(text)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure, masking values stored under sensitive keys."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _SENSITIVE_KEYS.search(str(k)) else redact_value(v, _depth=_depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact_value(v, _depth=_depth + 1) for v in value]
    return value
