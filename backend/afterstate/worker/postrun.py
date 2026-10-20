"""Actions after a run settles: audit trail, retry policy and failure notifications."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from afterstate.audit import Actor, record
from afterstate.db.base import utcnow
from afterstate.db.models import (
    BackupArtifact,
    Notification,
    RecoveryProject,
    RecoveryRun,
    RunEvent,
    Schedule,
    Workspace,
)
from afterstate.domain.enums import RETRYABLE_FAILURES, FailureClass, RunState, Trigger
from afterstate.domain.runconfig import RunConfig
from afterstate.domain.state_machine import TERMINAL_STATES
from afterstate.engine.pipeline import Services
from afterstate.runs import RunSubmissionError, enqueue_run

log = logging.getLogger("afterstate.postrun")
_MARK = "post-run actions completed"


def post_run(svc: Services, run_id: uuid.UUID) -> None:
    with svc.sf() as s, s.begin():
        run = s.get(RecoveryRun, run_id)
        if run is None or run.state not in TERMINAL_STATES:
            return
        if s.scalar(
            select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id, RunEvent.message == _MARK)
        ):
            return  # idempotent: already handled
        cfg = RunConfig.model_validate(run.config)
        ws = s.get(Workspace, run.workspace_id)
        assert ws is not None
        state = run.state
        record(
            s,
            Actor.system("worker"),
            f"run.{state.value.lower()}",
            workspace_id=run.workspace_id,
            target_type="run",
            target_id=run.id,
            outcome="success" if state is RunState.SUCCEEDED else "failure",
            details={
                "project": cfg.project_name,
                "failure_class": run.failure_class,
                "attempt": run.attempt,
                "trigger": run.trigger.value,
            },
        )

        retry_scheduled = False
        klass = FailureClass(run.failure_class) if run.failure_class else None
        if state is RunState.FAILED and klass in RETRYABLE_FAILURES and run.attempt < cfg.max_attempts:
            project = s.get(RecoveryProject, run.project_id)
            artifact = s.get(BackupArtifact, run.artifact_id)
            if (
                project is not None
                and project.archived_at is None
                and artifact is not None
                and artifact.deleted_at is None
            ):
                try:
                    schedule = s.get(Schedule, run.schedule_id) if run.schedule_id else None
                    child = enqueue_run(
                        s,
                        svc.settings,
                        project=project,
                        artifact=artifact,
                        trigger=Trigger.RETRY,
                        created_by=run.created_by,
                        schedule=schedule,
                        parent=run,
                        attempt=run.attempt + 1,
                        lab_scenario=run.lab_scenario,
                        not_before=utcnow() + timedelta(seconds=cfg.retry_backoff_seconds),
                        max_attempts=cfg.max_attempts,
                        retry_backoff=cfg.retry_backoff_seconds,
                    )
                    s.add(
                        RunEvent(
                            run_id=run.id,
                            level="INFO",
                            kind="system",
                            message=f"retryable failure ({klass.value if klass else ''}): attempt {child.attempt} queued as run {child.id}",
                        )
                    )
                    retry_scheduled = True
                except RunSubmissionError as exc:
                    s.add(RunEvent(run_id=run.id, level="WARN", kind="system", message=f"retry not queued: {exc}"))

        if not retry_scheduled and state in (RunState.FAILED, RunState.NEEDS_ATTENTION):
            kind = "run_failed" if state is RunState.FAILED else "run_needs_attention"
            title = f"{cfg.project_name}: recovery {'failed' if state is RunState.FAILED else 'not verified'}"
            body = (run.summary or "")[:500]
            payload = {
                "event": kind,
                "workspace_id": str(ws.id),
                "run_id": str(run.id),
                "project": cfg.project_name,
                "state": state.value,
                "failure_class": run.failure_class,
                "attempt": run.attempt,
            }
            s.add(
                Notification(
                    workspace_id=ws.id,
                    run_id=run.id,
                    kind=kind,
                    channel="in_app",
                    title=title,
                    body=body,
                    payload=payload,
                    status="delivered",
                    delivered_at=utcnow(),
                )
            )
            if ws.webhook_url:
                s.add(
                    Notification(
                        workspace_id=ws.id,
                        run_id=run.id,
                        kind=kind,
                        channel="webhook",
                        title=title,
                        body=body,
                        payload=payload,
                        status="pending",
                        next_attempt_at=utcnow(),
                    )
                )
        s.add(RunEvent(run_id=run.id, level="INFO", kind="system", message=_MARK))
