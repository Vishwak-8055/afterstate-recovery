from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from afterstate.db.models import (
    BackupArtifact,
    CleanupTask,
    RecoveryProject,
    RecoveryRun,
    RunEvent,
    RunEvidence,
    RunStage,
    ValidationResult,
)
from afterstate.domain.enums import Trigger
from afterstate.engine.pipeline import Services
from afterstate.runs import enqueue_run
from afterstate.worker.runner import Worker
from sqlalchemy import select


def submit(
    svc: Services,
    project_id: uuid.UUID,
    artifact_id: uuid.UUID,
    *,
    fault: dict[str, Any] | None = None,
    lab_scenario: str | None = None,
    trigger: Trigger = Trigger.MANUAL,
    **kw: Any,
) -> uuid.UUID:
    with svc.sf() as s, s.begin():
        project = s.get(RecoveryProject, project_id)
        artifact = s.get(BackupArtifact, artifact_id)
        assert project is not None and artifact is not None
        _ = list(project.rules)
        run = enqueue_run(
            s,
            svc.settings,
            project=project,
            artifact=artifact,
            trigger=trigger,
            fault=fault,
            lab_scenario=lab_scenario,
            **kw,
        )
        return run.id


def drain(svc: Services) -> int:
    return Worker(svc).drain()


@dataclass
class Snap:
    run: RecoveryRun
    stages: dict[str, str] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)  # name -> outcome
    results: list[ValidationResult] = field(default_factory=list)
    events: list[RunEvent] = field(default_factory=list)
    evidence: dict[str, RunEvidence] = field(default_factory=dict)
    tasks: list[CleanupTask] = field(default_factory=list)

    @property
    def state(self) -> str:
        return self.run.state.value

    def outcome_of(self, needle: str) -> str:
        hits = [
            r.outcome.value for r in self.results if needle.lower() in r.name.lower() or needle.lower() in r.key.lower()
        ]
        assert hits, f"no check matching {needle!r}; have {[r.name for r in self.results]}"
        return hits[0]

    def dump(self) -> str:
        bad = [
            f"{r.outcome.value:12s} {r.key:34s} {r.name[:44]:44s} {r.message[:90]}"
            for r in self.results
            if r.outcome.value != "PASS"
        ]
        return json.dumps(
            {
                "state": self.state,
                "failure": [self.run.failure_class, self.run.failure_message],
                "stages": self.stages,
                "non_pass": bad,
                "last_events": [e.message for e in self.events[-8:]],
            },
            indent=1,
        )


def snapshot(svc: Services, run_id: uuid.UUID) -> Snap:
    with svc.sf() as s:
        run = s.get(RecoveryRun, run_id)
        assert run is not None
        s.expunge(run)
        results = list(
            s.scalars(
                select(ValidationResult).where(ValidationResult.run_id == run_id).order_by(ValidationResult.position)
            )
        )
        stages = {
            st.name.value: st.state.value
            for st in s.scalars(select(RunStage).where(RunStage.run_id == run_id).order_by(RunStage.id))
        }
        events = list(s.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)))
        evidence = {e.kind: e for e in s.scalars(select(RunEvidence).where(RunEvidence.run_id == run_id))}
        tasks = list(s.scalars(select(CleanupTask).where(CleanupTask.run_id == run_id)))
        for obj in (*results, *events, *evidence.values(), *tasks):
            s.expunge(obj)
    return Snap(run, stages, {r.name: r.outcome.value for r in results}, results, events, evidence, tasks)


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2, message: str = "condition"):  # type: ignore[no-untyped-def]
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def processes_mentioning(fragment: str) -> list[int]:
    """PIDs of live processes whose command line contains fragment (used to prove nothing leaked)."""
    import os
    from pathlib import Path

    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if fragment in cmd and state != "Z":
                found.append(int(entry.name))
    return found
