"""Verified teardown of temporary resources, driven by durable cleanup tasks.

A task is written *before* its resource is created, so a crash can never leave an environment that
the platform has no record of. Teardown is idempotent and the task is only marked DONE after the
resource is confirmed gone."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from afterstate.db.base import utcnow
from afterstate.db.models import CleanupTask
from afterstate.domain.enums import CleanupTaskState
from afterstate.engine.environment import LocalClusterEnvironment
from afterstate.engine.store import RunStore


@dataclass
class CleanupOutcome:
    ok: bool
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CleanupService:
    def __init__(self, store: RunStore, sf: sessionmaker[Session], scratch_root: Path) -> None:
        self.store, self.sf, self.scratch_root = store, sf, Path(scratch_root)

    def run_for_run(self, run_id: uuid.UUID, *, fail_once: bool = False) -> CleanupOutcome:
        outcome = CleanupOutcome(ok=True)
        for task in self.store.unfinished_cleanup_tasks(run_id):
            error: str | None = None
            try:
                if fail_once and task.attempts == 0:
                    raise RuntimeError("injected fault: cleanup failed on its first attempt (Recovery Lab)")
                if task.resource_type == "environment":
                    outcome.notes += LocalClusterEnvironment.destroy_path(
                        Path(task.resource_ref), self.scratch_root, expect_run_id=run_id
                    )
                else:
                    raise RuntimeError(f"unknown resource type {task.resource_type!r}")
            except Exception as exc:  # cleanup must report, never crash the caller
                error = f"{type(exc).__name__}: {exc}"[:500]
            with self.sf() as s, s.begin():
                row = s.get(CleanupTask, task.id)
                assert row is not None
                row.attempts += 1
                if error is None:
                    row.state, row.completed_at, row.last_error = CleanupTaskState.DONE, utcnow(), None
                else:
                    row.state, row.last_error = CleanupTaskState.FAILED, error
            if error:
                outcome.ok = False
                outcome.errors.append(error)
        return outcome
