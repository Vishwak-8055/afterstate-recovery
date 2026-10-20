"""Durable run state. Every write made by a running pipeline is fenced by its lease owner: if the
lease was lost (worker paused, then reaped), the write fails and the stale pipeline stops."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from afterstate.db.base import utcnow
from afterstate.db.models import CleanupTask, RecoveryRun, RunEvent, RunEvidence, RunStage, ValidationResult
from afterstate.domain.checks import CheckResult
from afterstate.domain.enums import CleanupTaskState, RunState, StageName, StageState
from afterstate.domain.state_machine import ACTIVE_STATES, TERMINAL_STATES, assert_transition
from afterstate.security.redact import Redactor, redact_value

_SETTABLE = {
    "failure_class",
    "failure_message",
    "summary",
    "metrics",
    "objectives",
    "environment",
    "progress",
    "cleanup_state",
    "settled_state",
    "started_at",
    "finished_at",
    "lease_owner",
    "lease_expires_at",
    "heartbeat_at",
    "not_before",
}


class LeaseLost(Exception):  # noqa: N818
    """This worker no longer owns the run; it must stop touching it."""


@dataclass
class EventRecord:
    level: str
    message: str
    stage: str | None = None
    kind: str = "log"
    data: dict[str, Any] | None = None


class RunStore:
    def __init__(self, sf: sessionmaker[Session]) -> None:
        self.sf = sf

    # ---- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _guard(
        s: Session, run_id: uuid.UUID, owner: str | None, *, lock: bool = False, shared: bool = False
    ) -> RecoveryRun:
        stmt = select(RecoveryRun).where(RecoveryRun.id == run_id)
        if lock:
            stmt = stmt.with_for_update()
        elif shared:
            stmt = stmt.with_for_update(read=True)
        run = s.execute(stmt).scalar_one()
        if owner is not None and run.lease_owner != owner:
            raise LeaseLost(f"run {run_id} is now owned by {run.lease_owner!r}, not {owner!r}")
        return run

    # ---- run state -------------------------------------------------------------------------
    def transition(self, run_id: uuid.UUID, to: RunState, *, owner: str | None, **fields: Any) -> RunState:
        bad = set(fields) - _SETTABLE
        if bad:
            raise ValueError(f"fields not settable via transition: {sorted(bad)}")
        with self.sf() as s, s.begin():
            run = self._guard(s, run_id, owner, lock=True)
            assert_transition(run.state, to)
            previous = run.state
            now = utcnow()
            run.state, run.state_entered_at = to, now
            for key, value in fields.items():
                setattr(run, key, value)
            if to in TERMINAL_STATES:
                run.finished_at = run.finished_at or now
                run.lease_owner, run.lease_expires_at = None, None
            s.add(
                RunEvent(
                    run_id=run_id,
                    ts=now,
                    level="INFO",
                    stage=None,
                    kind="state",
                    message=f"{previous.value} -> {to.value}",
                )
            )
            return previous

    def set_fields(self, run_id: uuid.UUID, owner: str | None, **fields: Any) -> None:
        bad = set(fields) - _SETTABLE
        if bad:
            raise ValueError(f"fields not settable: {sorted(bad)}")
        with self.sf() as s, s.begin():
            run = self._guard(s, run_id, owner, shared=True)
            for key, value in fields.items():
                setattr(run, key, value)

    def touch_lease(self, run_id: uuid.UUID, owner: str, lease_seconds: int) -> tuple[bool, bool]:
        """Heartbeat. Returns (still_owner, cancel_requested)."""
        with self.sf() as s, s.begin():
            row = s.execute(
                update(RecoveryRun)
                .where(RecoveryRun.id == run_id, RecoveryRun.lease_owner == owner, RecoveryRun.state.in_(ACTIVE_STATES))
                .values(heartbeat_at=utcnow(), lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
                .returning(RecoveryRun.cancel_requested)
            ).first()
        return (True, bool(row[0])) if row else (False, False)

    # ---- stages ----------------------------------------------------------------------------
    def begin_stage(self, run_id: uuid.UUID, owner: str, name: StageName) -> int:
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            stage = RunStage(run_id=run_id, name=name, state=StageState.RUNNING, started_at=utcnow())
            s.add(stage)
            s.flush()
            return int(stage.id)

    def end_stage(
        self,
        run_id: uuid.UUID,
        owner: str,
        stage_id: int,
        state: StageState,
        duration_ms: int,
        *,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            stage = s.get(RunStage, stage_id)
            assert stage is not None
            stage.state, stage.finished_at, stage.duration_ms = state, utcnow(), duration_ms
            stage.details = redact_value(details or {})
            stage.error = error

    def skip_stage(self, run_id: uuid.UUID, owner: str, name: StageName, reason: str) -> None:
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            s.add(RunStage(run_id=run_id, name=name, state=StageState.SKIPPED, details={"reason": reason}))

    # ---- events and results ----------------------------------------------------------------
    def add_events(self, run_id: uuid.UUID, owner: str | None, events: list[EventRecord]) -> None:
        if not events:
            return
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            s.add_all(
                RunEvent(
                    run_id=run_id,
                    ts=utcnow(),
                    level=e.level,
                    stage=e.stage,
                    kind=e.kind,
                    message=e.message[:1500],
                    data=redact_value(e.data) if e.data else None,
                )
                for e in events
            )

    def add_results(
        self, run_id: uuid.UUID, owner: str | None, results: list[CheckResult], start_position: int
    ) -> None:
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            for i, r in enumerate(results):
                s.add(
                    ValidationResult(
                        run_id=run_id,
                        rule_id=uuid.UUID(r.rule_id) if r.rule_id else None,
                        position=start_position + i,
                        key=r.key,
                        category=r.category,
                        name=r.name[:300],
                        outcome=r.outcome,
                        required=r.required,
                        message=r.message[:2000],
                        expected=redact_value(r.expected),
                        actual=redact_value(r.actual),
                        coverage=r.coverage.value if r.coverage else None,
                        duration_ms=r.duration_ms,
                    )
                )

    def add_evidence(
        self, run_id: uuid.UUID, kind: str, storage_key: str, sha256: str, size: int, content_type: str
    ) -> None:
        with self.sf() as s, s.begin():
            existing = s.execute(
                select(RunEvidence).where(RunEvidence.run_id == run_id, RunEvidence.kind == kind)
            ).scalar_one_or_none()
            if existing:
                existing.storage_key, existing.sha256, existing.size_bytes, existing.content_type = (
                    storage_key,
                    sha256,
                    size,
                    content_type,
                )
                existing.created_at = utcnow()
            else:
                s.add(
                    RunEvidence(
                        run_id=run_id,
                        kind=kind,
                        storage_key=storage_key,
                        sha256=sha256,
                        size_bytes=size,
                        content_type=content_type,
                    )
                )

    # ---- cleanup tasks ---------------------------------------------------------------------
    def add_cleanup_task(
        self, run_id: uuid.UUID, owner: str | None, resource_type: str, resource_ref: str
    ) -> uuid.UUID:
        with self.sf() as s, s.begin():
            self._guard(s, run_id, owner, shared=True)
            task = CleanupTask(run_id=run_id, resource_type=resource_type, resource_ref=resource_ref)
            s.add(task)
            s.flush()
            return task.id

    def unfinished_cleanup_tasks(self, run_id: uuid.UUID) -> list[CleanupTask]:
        with self.sf() as s:
            return list(
                s.scalars(
                    select(CleanupTask)
                    .where(CleanupTask.run_id == run_id, CleanupTask.state != CleanupTaskState.DONE)
                    .order_by(CleanupTask.created_at)
                )
            )


class EventSink:
    """Buffers log events, flushing in small batches. Caps log volume per run so a chatty restore
    cannot exhaust the database, and always keeps structural (stage/state/fault) events."""

    def __init__(self, store: RunStore, run_id: uuid.UUID, owner: str | None, *, cap: int, redactor: Redactor) -> None:
        self.store, self.run_id, self.owner, self.cap, self.redactor = store, run_id, owner, cap, redactor
        self._buf: list[EventRecord] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._logged = 0
        self._truncated = False
        self.stage: str | None = None
        self.transcript: list[str] = []  # sanitized restore output kept for the evidence file
        self._transcript_cap = 20_000

    def emit(self, level: str, message: str, *, kind: str = "log", data: dict[str, Any] | None = None) -> None:
        message = self.redactor.apply(message)[:1000]
        with self._lock:
            if kind == "log":
                if len(self.transcript) < self._transcript_cap:
                    self.transcript.append(f"{level:5s} {message}")
                self._logged += 1
                if self._logged > self.cap:
                    if not self._truncated:
                        self._truncated = True
                        self._buf.append(
                            EventRecord(
                                "WARN",
                                f"log output truncated after {self.cap} lines; the full transcript is in the evidence file",
                                self.stage,
                                "system",
                            )
                        )
                    elif level not in ("ERROR", "WARN"):
                        return
            self._buf.append(EventRecord(level, message, self.stage, kind, data))
            if len(self._buf) >= 25 or time.monotonic() - self._last_flush > 0.5:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        batch, self._buf = self._buf, []
        self._last_flush = time.monotonic()
        if batch:
            self.store.add_events(self.run_id, self.owner, batch)
