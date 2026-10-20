"""Run submission: building the immutable run snapshot and enqueueing it durably."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from afterstate.config import Settings
from afterstate.db.base import utcnow
from afterstate.db.models import BackupArtifact, RecoveryProject, RecoveryRun, Schedule
from afterstate.domain.enums import RunState, Trigger
from afterstate.domain.rules import Objectives, RestoreOptions
from afterstate.domain.runconfig import ArtifactRef, RuleSnapshot, RunConfig


class RunSubmissionError(Exception):
    code = "run_submission_failed"


class ActiveRunExists(RunSubmissionError):  # noqa: N818
    code = "active_run_exists"


class NoArtifact(RunSubmissionError):
    code = "no_artifact"


def resolve_artifact(session: Session, project: RecoveryProject) -> BackupArtifact | None:
    """The artifact a run of this project would test: the pinned one, else the newest with the label."""
    if project.artifact_id:
        art = session.get(BackupArtifact, project.artifact_id)
        return art if art and art.deleted_at is None and art.workspace_id == project.workspace_id else None
    if project.artifact_label:
        return session.scalars(
            select(BackupArtifact)
            .where(
                BackupArtifact.workspace_id == project.workspace_id,
                BackupArtifact.label == project.artifact_label,
                BackupArtifact.deleted_at.is_(None),
            )
            .order_by(BackupArtifact.created_at.desc())
        ).first()
    return None


def build_run_config(
    project: RecoveryProject,
    artifact: BackupArtifact,
    settings: Settings,
    *,
    max_attempts: int | None = None,
    retry_backoff: int | None = None,
) -> RunConfig:
    return RunConfig(
        project_id=str(project.id),
        project_name=project.name,
        artifact=ArtifactRef(
            id=str(artifact.id),
            name=artifact.name,
            storage_key=artifact.storage_key,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            format=artifact.format.value,
            backup_created_at=artifact.backup_created_at,
        ),
        restore=RestoreOptions.model_validate(project.restore_options or {}),
        restore_timeout_seconds=project.restore_timeout_seconds,
        validation_timeout_seconds=project.validation_timeout_seconds,
        objectives=Objectives(rto_seconds=project.rto_seconds, rpo_seconds=project.rpo_seconds),
        rules=[
            RuleSnapshot(
                id=str(r.id), position=r.position, name=r.name, required=r.required, kind=r.kind, config=r.config
            )
            for r in project.rules
        ],
        app_suite=project.app_suite,
        max_attempts=max_attempts if max_attempts is not None else project.max_attempts,
        max_environment_bytes=settings.max_environment_bytes,
        retry_backoff_seconds=retry_backoff if retry_backoff is not None else 30,
    )


def enqueue_run(
    session: Session,
    settings: Settings,
    *,
    project: RecoveryProject,
    artifact: BackupArtifact,
    trigger: Trigger,
    created_by: uuid.UUID | None = None,
    schedule: Schedule | None = None,
    parent: RecoveryRun | None = None,
    attempt: int = 1,
    fault: dict[str, Any] | None = None,
    lab_scenario: str | None = None,
    idempotency_key: str | None = None,
    not_before: datetime | None = None,
    max_attempts: int | None = None,
    retry_backoff: int | None = None,
) -> RecoveryRun:
    """Insert a QUEUED run. The database enforces 'one queued/active run per project' atomically."""
    now = utcnow()
    config = build_run_config(project, artifact, settings, max_attempts=max_attempts, retry_backoff=retry_backoff)
    run = RecoveryRun(
        workspace_id=project.workspace_id,
        project_id=project.id,
        artifact_id=artifact.id,
        trigger=trigger,
        schedule_id=schedule.id if schedule else None,
        parent_run_id=parent.id if parent else None,
        attempt=attempt,
        idempotency_key=idempotency_key,
        state=RunState.QUEUED,
        queued_at=now,
        not_before=not_before or now,
        state_entered_at=now,
        config=config.model_dump(mode="json"),
        fault=fault,
        created_by=created_by,
        lab_scenario=lab_scenario,
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError as exc:
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or ""
        if "one_slot_per_project" in name:
            raise ActiveRunExists("A recovery run for this project is already queued or in progress.") from exc
        raise
    return run
