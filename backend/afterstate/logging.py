"""Structured JSON logging with correlation, workspace and run identifiers."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from afterstate.config import Settings
from afterstate.security.redact import redact_text, redact_value

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
workspace_id_var: ContextVar[str | None] = ContextVar("workspace_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
stage_var: ContextVar[str | None] = ContextVar("stage", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, var in (
            ("correlation_id", correlation_id_var),
            ("workspace_id", workspace_id_var),
            ("run_id", run_id_var),
            ("stage", stage_var),
        ):
            value = var.get()
            if value:
                payload[key] = value
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact_value(extras))
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return super().format(record)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if settings.log_json else _TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def bind(**values: str | None) -> Iterator[None]:
    """Bind context variables (run_id=..., workspace_id=..., correlation_id=..., stage=...)."""
    vars_by_name = {
        "correlation_id": correlation_id_var,
        "workspace_id": workspace_id_var,
        "run_id": run_id_var,
        "stage": stage_var,
    }
    tokens = [(vars_by_name[k], vars_by_name[k].set(v)) for k, v in values.items()]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
