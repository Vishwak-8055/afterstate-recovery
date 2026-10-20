"""Starting, observing and resetting Recovery Lab scenarios.

Lab resources are real rows and files, marked with lab_scenario. Reset deletes only rows carrying
that marker for the given workspace and scenario; it can never touch a customer project."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from afterstate.artifacts import register_stored_artifact
from afterstate.db.models import BackupArtifact, RecoveryProject, RecoveryRun, RunEvidence
from afterstate.domain.enums import Trigger
from afterstate.domain.state_machine import SLOT_STATES
from afterstate.engine.pipeline import Services
from afterstate.lab.fixtures import FIXTURE_VERSION, FixtureSet, build_fixture_set
from afterstate.lab.scenarios import BY_KEY, Scenario
from afterstate.projects import replace_rules
from afterstate.runs import enqueue_run
from afterstate.storage.local import LocalArtifactStorage, new_artifact_key


class LabError(Exception):
    code = "lab_error"


class LabBusy(LabError):
    code = "lab_busy"


def fixture_set(svc: Services) -> FixtureSet:
    root = Path(svc.settings.artifact_dir) / "lab-fixtures" / f"v{FIXTURE_VERSION}-pg{svc.tools.major}"
    return build_fixture_set(svc.tools, Path(svc.settings.scratch_dir), root)


def reset_scenario(svc: Services, workspace_id: uuid.UUID, key: str) -> dict[str, int]:
    """Delete only this scenario's lab-owned resources in this workspace."""
    if key not in BY_KEY:
        raise LabError(f"unknown scenario {key}")
    with svc.sf() as s, s.begin():
        busy = s.scalar(
            select(RecoveryRun.id)
            .where(
                RecoveryRun.workspace_id == workspace_id,
                RecoveryRun.lab_scenario == key,
                RecoveryRun.state.in_(SLOT_STATES),
            )
            .limit(1)
        )
        if busy:
            raise LabBusy("A run of this scenario is still queued or in progress. Cancel it before resetting.")
        run_ids = list(
            s.scalars(
                select(RecoveryRun.id).where(RecoveryRun.workspace_id == workspace_id, RecoveryRun.lab_scenario == key)
            )
        )
        evidence_keys = (
            list(s.scalars(select(RunEvidence.storage_key).where(RunEvidence.run_id.in_(run_ids)))) if run_ids else []
        )
        artifact_keys = list(
            s.scalars(
                select(BackupArtifact.storage_key).where(
                    BackupArtifact.workspace_id == workspace_id, BackupArtifact.lab_scenario == key
                )
            )
        )
        s.execute(delete(RecoveryRun).where(RecoveryRun.workspace_id == workspace_id, RecoveryRun.lab_scenario == key))
        deleted_projects = s.execute(
            delete(RecoveryProject).where(
                RecoveryProject.workspace_id == workspace_id, RecoveryProject.lab_scenario == key
            )
        )
        projects = cast("CursorResult[Any]", deleted_projects).rowcount
        s.execute(
            delete(BackupArtifact).where(
                BackupArtifact.workspace_id == workspace_id, BackupArtifact.lab_scenario == key
            )
        )
    for k in [*evidence_keys, *artifact_keys]:
        svc.storage.delete(k)
    return {
        "runs": len(run_ids),
        "projects": projects or 0,
        "artifacts": len(artifact_keys),
        "files": len(evidence_keys) + len(artifact_keys),
    }


def start_scenario(svc: Services, workspace_id: uuid.UUID, user_id: uuid.UUID, key: str) -> uuid.UUID:
    scenario = BY_KEY.get(key)
    if scenario is None:
        raise LabError(f"unknown scenario {key}")
    reset_scenario(svc, workspace_id, key)
    fx = fixture_set(svc)
    source = fx.path(scenario.variant)
    storage_key = new_artifact_key(workspace_id)
    assert isinstance(svc.storage, LocalArtifactStorage)
    target = svc.storage.resolve(storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    import hashlib

    digest = hashlib.sha256()
    with target.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    from afterstate.storage.base import StoredObject

    built = datetime.fromisoformat(str(fx.meta["built_at"]))
    art, _ver, _insp = register_stored_artifact(
        svc,
        workspace_id=workspace_id,
        user_id=user_id,
        stored=StoredObject(storage_key, target.stat().st_size, digest.hexdigest()),
        name=f"lab-{key}{source.suffix}",
        label=f"lab-{key}",
        original_filename=source.name,
        backup_created_at=built,
        notes=f"Recovery Lab artifact for scenario '{key}'.",
        lab_scenario=key,
    )
    with svc.sf() as s, s.begin():
        project = RecoveryProject(
            workspace_id=workspace_id,
            name=f"Lab: {scenario.title}",
            description=scenario.summary,
            artifact_id=art.id,
            restore_timeout_seconds=scenario.project.get("restore_timeout_seconds", 120),
            validation_timeout_seconds=120,
            rto_seconds=scenario.project.get("rto_seconds"),
            rpo_seconds=scenario.project.get("rpo_seconds"),
            max_attempts=scenario.project.get("max_attempts", 1),
            app_suite=scenario.project.get("app_suite"),
            created_by=user_id,
            lab_scenario=key,
        )
        s.add(project)
        s.flush()
        replace_rules(s, project, scenario.rules(fx.meta))
        s.flush()
        run = enqueue_run(
            s,
            svc.settings,
            project=project,
            artifact=art,
            trigger=Trigger.LAB,
            created_by=user_id,
            fault=scenario.fault,
            lab_scenario=key,
            max_attempts=scenario.project.get("max_attempts", 1),
            retry_backoff=2,
        )
        return run.id


@dataclass
class ScenarioStatus:
    key: str
    state: str  # not_run | running | matched | mismatch
    runs: list[dict[str, Any]]
    note: str


def evaluate(svc: Services, workspace_id: uuid.UUID, scenario: Scenario) -> ScenarioStatus:
    with svc.sf() as s:
        runs = list(
            s.scalars(
                select(RecoveryRun)
                .where(RecoveryRun.workspace_id == workspace_id, RecoveryRun.lab_scenario == scenario.key)
                .order_by(RecoveryRun.queued_at)
            )
        )
        from afterstate.db.models import CleanupTask

        attempts = {
            r.id: max([t.attempts for t in s.scalars(select(CleanupTask).where(CleanupTask.run_id == r.id))] or [0])
            for r in runs
        }
        summaries: list[dict[str, Any]] = [
            {
                "id": str(r.id),
                "attempt": r.attempt,
                "state": r.state.value,
                "failure_class": r.failure_class,
                "cleanup_state": r.cleanup_state.value,
                "cleanup_attempts": attempts[r.id],
                "settled_state": r.settled_state.value if r.settled_state else None,
            }
            for r in runs
        ]
    if not runs:
        return ScenarioStatus(scenario.key, "not_run", [], "Not run yet.")
    if any(r["state"] in ("QUEUED", "PREPARING", "RESTORING", "VALIDATING", "FINALIZING") for r in summaries):
        return ScenarioStatus(scenario.key, "running", summaries, "In progress.")
    last = summaries[-1]
    if scenario.key == "interrupted_worker":
        ok = (
            len(summaries) >= 2
            and summaries[0]["state"] == "FAILED"
            and summaries[0]["failure_class"] == "WORKER_INTERRUPTED"
            and summaries[0]["cleanup_state"] == "DONE"
            and last["state"] == "SUCCEEDED"
        )
        note = (
            "Attempt 1 was interrupted and cleaned up; attempt 2 succeeded."
            if ok
            else "Waiting for the interrupted attempt to be recovered and retried."
            if len(summaries) < 2 and summaries[0]["state"] in ("NEEDS_CLEANUP",)
            else "Observed outcome differs from the expectation."
        )
    elif scenario.key == "cleanup_failure":
        if last["state"] == "NEEDS_CLEANUP":
            return ScenarioStatus(
                scenario.key,
                "running",
                summaries,
                "Cleanup failed once; the run is NEEDS_CLEANUP until the worker retries cleanup.",
            )
        ok = last["state"] == "SUCCEEDED" and last["cleanup_attempts"] >= 2 and last["cleanup_state"] == "DONE"
        note = (
            "Cleanup failed on the first attempt and was confirmed on retry."
            if ok
            else "Observed outcome differs from the expectation."
        )
    else:
        ok = last["state"] == scenario.expected_state and last["failure_class"] == scenario.expected_failure_class
        note = (
            f"Observed {last['state']}"
            + (f" / {last['failure_class']}" if last["failure_class"] else "")
            + (
                " as expected."
                if ok
                else f", expected {scenario.expected_state}"
                + (f" / {scenario.expected_failure_class}" if scenario.expected_failure_class else "")
                + "."
            )
        )
    return ScenarioStatus(scenario.key, "matched" if ok else "mismatch", summaries, note)
