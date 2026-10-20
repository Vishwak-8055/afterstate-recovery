from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from afterstate.audit import record
from afterstate.db.base import utcnow
from afterstate.db.models import (
    AuditEvent,
    BackupArtifact,
    CleanupTask,
    RecoveryProject,
    RecoveryRun,
    RunEvent,
    RunEvidence,
    RunStage,
    Schedule,
    ScheduleFiring,
    ValidationResult,
)
from afterstate.domain.enums import RunState, Trigger
from afterstate.domain.state_machine import ACTIVE_STATES, SLOT_STATES, TERMINAL_STATES
from afterstate.lab import service as lab
from afterstate.lab.scenarios import BY_KEY, SCENARIOS
from afterstate.runs import ActiveRunExists, enqueue_run, resolve_artifact
from afterstate.security.rbac import Permission, role_allows
from afterstate.worker.scheduler import InvalidSchedule, next_fire_after, validate_schedule

from .. import serializers as ser
from ..deps import WorkspaceCtx, get_db, get_services, require
from ..errors import ApiError, not_found
from ..schemas import RunIn, ScheduleIn, SchedulePatch
from .registry import _artifact_or_404, _project_or_404, paginate

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}")


def _run_or_404(db: Session, ws_id: uuid.UUID, run_id: uuid.UUID) -> RecoveryRun:
    run = db.scalar(select(RecoveryRun).where(RecoveryRun.id == run_id, RecoveryRun.workspace_id == ws_id))
    if run is None:
        raise not_found("Recovery run")
    return run


# ------------------------------------------------------------------------------- runs -------------
@router.post("/projects/{project_id}/runs", status_code=202)
def start_run(
    project_id: uuid.UUID,
    body: RunIn,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=100),
    ctx: WorkspaceCtx = Depends(require(Permission.RUN_START)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    svc = get_services()
    project = _project_or_404(db, ctx.id, project_id)
    if idempotency_key:
        existing = db.scalar(
            select(RecoveryRun).where(
                RecoveryRun.workspace_id == ctx.id, RecoveryRun.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return {**ser.run_summary(existing), "idempotent_replay": True}
    artifact = _artifact_or_404(db, ctx.id, body.artifact_id) if body.artifact_id else resolve_artifact(db, project)
    if artifact is None:
        raise ApiError(
            409,
            "no_artifact",
            "This project has no backup artifact to test. Select one on the project or register a backup with the project's label.",
        )
    try:
        run = enqueue_run(
            db,
            svc.settings,
            project=project,
            artifact=artifact,
            trigger=Trigger.MANUAL,
            created_by=ctx.auth.user.id,
            idempotency_key=idempotency_key,
        )
    except ActiveRunExists as exc:
        active = db.scalar(
            select(RecoveryRun.id)
            .where(RecoveryRun.project_id == project.id, RecoveryRun.state.in_(SLOT_STATES))
            .limit(1)
        )
        raise ApiError(409, "active_run_exists", str(exc), {"run_id": str(active) if active else None}) from exc
    record(
        db,
        ctx.actor,
        "run.start",
        workspace_id=ctx.id,
        target_type="run",
        target_id=run.id,
        details={"project": project.name, "artifact": artifact.name, "trigger": "manual"},
    )
    db.commit()
    return ser.run_summary(run)


@router.get("/runs")
def list_runs(
    project_id: uuid.UUID | None = None,
    state: list[RunState] | None = Query(None),
    trigger: Trigger | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_lab: bool = True,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(RecoveryRun).where(RecoveryRun.workspace_id == ctx.id)
    if project_id:
        stmt = stmt.where(RecoveryRun.project_id == project_id)
    if state:
        stmt = stmt.where(RecoveryRun.state.in_(state))
    if trigger:
        stmt = stmt.where(RecoveryRun.trigger == trigger)
    if since:
        stmt = stmt.where(RecoveryRun.queued_at >= since)
    if until:
        stmt = stmt.where(RecoveryRun.queued_at <= until)
    if not include_lab:
        stmt = stmt.where(RecoveryRun.lab_scenario.is_(None))
    rows, total = paginate(db, stmt.order_by(RecoveryRun.queued_at.desc()), page, page_size)
    return {"items": [ser.run_summary(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/runs/compare")
def compare_runs(
    a: uuid.UUID, b: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ra, rb = _run_or_404(db, ctx.id, a), _run_or_404(db, ctx.id, b)

    def checks(r: RecoveryRun) -> dict[str, dict[str, Any]]:
        return {
            f"{x.key}|{x.name}": {"outcome": x.outcome.value, "message": x.message}
            for x in db.scalars(select(ValidationResult).where(ValidationResult.run_id == r.id))
        }

    ca, cb = checks(ra), checks(rb)
    deltas = {}
    for k in (
        "recovery_seconds",
        "restore_seconds",
        "validation_seconds",
        "total_seconds",
        "restore_throughput_mb_s",
        "restored_database_bytes",
        "artifact_size_bytes",
    ):
        va, vb = (ra.metrics or {}).get(k), (rb.metrics or {}).get(k)
        if isinstance(va, int | float) and isinstance(vb, int | float):
            deltas[k] = {
                "a": va,
                "b": vb,
                "delta": round(vb - va, 3),
                "percent": round((vb - va) / va * 100, 1) if va else None,
            }
        else:
            deltas[k] = {"a": va, "b": vb, "delta": None, "percent": None}
    changes = [
        {
            "check": k.split("|", 1)[1],
            "key": k.split("|", 1)[0],
            "a": ca.get(k, {}).get("outcome"),
            "b": cb.get(k, {}).get("outcome"),
        }
        for k in sorted(set(ca) | set(cb))
        if ca.get(k, {}).get("outcome") != cb.get(k, {}).get("outcome")
    ]
    return {
        "a": ser.run_summary(ra),
        "b": ser.run_summary(rb),
        "metric_deltas": deltas,
        "check_changes": changes,
        "unchanged_checks": len(set(ca) & set(cb)) - len([c for c in changes if c["a"] and c["b"]]),
        "same_artifact": ra.artifact_id == rb.artifact_id,
        "same_project": ra.project_id == rb.project_id,
        "objectives": {"a": ra.objectives, "b": rb.objectives},
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    run = _run_or_404(db, ctx.id, run_id)
    stages = db.scalars(select(RunStage).where(RunStage.run_id == run.id).order_by(RunStage.id)).all()
    results = db.scalars(
        select(ValidationResult).where(ValidationResult.run_id == run.id).order_by(ValidationResult.position)
    ).all()
    tasks = db.scalars(select(CleanupTask).where(CleanupTask.run_id == run.id).order_by(CleanupTask.created_at)).all()
    evidence = db.scalars(select(RunEvidence).where(RunEvidence.run_id == run.id)).all()
    children = db.scalars(select(RecoveryRun.id).where(RecoveryRun.parent_run_id == run.id)).all()
    cfg = run.config or {}
    return {
        **ser.run_summary(run),
        "metrics": run.metrics,
        "objectives": run.objectives,
        "environment": run.environment,
        "config": {
            "restore": cfg.get("restore"),
            "restore_timeout_seconds": cfg.get("restore_timeout_seconds"),
            "validation_timeout_seconds": cfg.get("validation_timeout_seconds"),
            "objectives": cfg.get("objectives"),
            "app_suite": cfg.get("app_suite"),
            "max_attempts": cfg.get("max_attempts"),
            "rule_count": len(cfg.get("rules", [])),
            "artifact": {
                k: (cfg.get("artifact") or {}).get(k) for k in ("id", "name", "sha256", "size_bytes", "format")
            },
        },
        "stages": [
            {
                "name": s.name.value,
                "state": s.state.value,
                "started_at": ser.iso(s.started_at),
                "finished_at": ser.iso(s.finished_at),
                "duration_ms": s.duration_ms,
                "error": s.error,
                "details": s.details,
            }
            for s in stages
        ],
        "results": [
            {
                "position": r.position,
                "key": r.key,
                "category": r.category,
                "name": r.name,
                "outcome": r.outcome.value,
                "required": r.required,
                "message": r.message,
                "expected": r.expected,
                "actual": r.actual,
                "coverage": r.coverage,
                "duration_ms": r.duration_ms,
            }
            for r in results
        ],
        "cleanup": {
            "state": run.cleanup_state.value,
            "tasks": [
                {
                    "resource_type": t.resource_type,
                    "resource": t.resource_ref.rsplit("/", 1)[-1],
                    "state": t.state.value,
                    "attempts": t.attempts,
                    "error": t.last_error,
                    "completed_at": ser.iso(t.completed_at),
                }
                for t in tasks
            ],
        },
        "evidence": [
            {"kind": e.kind, "sha256": e.sha256, "size_bytes": e.size_bytes, "content_type": e.content_type}
            for e in evidence
        ],
        "retry_of": str(run.parent_run_id) if run.parent_run_id else None,
        "retries": [str(c) for c in children],
        "fault": run.fault if run.lab_scenario else None,
    }


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(300, ge=1, le=1000),
    ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = _run_or_404(db, ctx.id, run_id)
    rows = db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.id > after).order_by(RunEvent.id).limit(limit)
    ).all()
    current = db.scalar(
        select(RunStage.name)
        .where(RunStage.run_id == run.id, RunStage.state == "RUNNING")
        .order_by(RunStage.id.desc())
        .limit(1)
    )
    return {
        "events": [
            {"id": e.id, "ts": ser.iso(e.ts), "level": e.level, "stage": e.stage, "kind": e.kind, "message": e.message}
            for e in rows
        ],
        "next_after": rows[-1].id if rows else after,
        "state": run.state.value,
        "settled_state": run.settled_state.value if run.settled_state else None,
        "progress": run.progress,
        "current_stage": current.value if current else None,
        "terminal": run.state in TERMINAL_STATES,
        "started_at": ser.iso(run.started_at),
        "finished_at": ser.iso(run.finished_at),
    }


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.RUN_CANCEL)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    run = db.scalar(
        select(RecoveryRun).where(RecoveryRun.id == run_id, RecoveryRun.workspace_id == ctx.id).with_for_update()
    )
    if run is None:
        raise not_found("Recovery run")
    now = utcnow()
    if run.state is RunState.QUEUED:
        run.state, run.state_entered_at, run.finished_at, run.cancel_requested = RunState.CANCELLED, now, now, True
        run.failure_class, run.summary = "CANCELLED", "Cancelled before a worker started it."
        db.add(
            RunEvent(
                run_id=run.id,
                ts=now,
                level="WARN",
                kind="state",
                message=f"QUEUED -> CANCELLED (by {ctx.auth.user.email})",
            )
        )
        message = "The run was cancelled before it started."
    elif run.state in ACTIVE_STATES and run.state is not RunState.FINALIZING:
        run.cancel_requested = True
        db.add(
            RunEvent(
                run_id=run.id,
                ts=now,
                level="WARN",
                kind="system",
                message=f"cancellation requested by {ctx.auth.user.email}; the worker will stop at the next safe point and clean up",
            )
        )
        message = "Cancellation requested. The worker will stop at the next safe point and clean up."
    else:
        raise ApiError(409, "not_cancellable", f"A run in state {run.state.value} cannot be cancelled.")
    record(
        db,
        ctx.actor,
        "run.cancel",
        workspace_id=ctx.id,
        target_type="run",
        target_id=run.id,
        details={"state": run.state.value},
    )
    db.commit()
    return {"message": message, "run": ser.run_summary(run)}


@router.post("/runs/{run_id}/cleanup", status_code=202)
def retry_cleanup(
    run_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.RUN_CANCEL)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    run = _run_or_404(db, ctx.id, run_id)
    if run.state is not RunState.NEEDS_CLEANUP:
        raise ApiError(409, "no_cleanup_pending", "This run has no pending cleanup.")
    run.heartbeat_at = utcnow() - timedelta(days=1)  # makes it due on the worker's next cleanup pass
    record(db, ctx.actor, "run.cleanup_retry", workspace_id=ctx.id, target_type="run", target_id=run.id)
    db.commit()
    return {"message": "Cleanup will be retried by the worker within seconds."}


@router.get("/runs/{run_id}/report")
def get_report(
    run_id: uuid.UUID,
    format: Literal["json", "html"] = "html",
    download: bool = False,
    ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)),
    db: Session = Depends(get_db),
) -> Response:
    run = _run_or_404(db, ctx.id, run_id)
    ev = db.scalar(select(RunEvidence).where(RunEvidence.run_id == run.id, RunEvidence.kind == f"report_{format}"))
    if ev is None:
        raise ApiError(
            404, "report_unavailable", "The report is not available yet. It is generated when the run finishes."
        )
    data = get_services().storage.read_bytes(ev.storage_key)
    if hashlib.sha256(data).hexdigest() != ev.sha256:
        raise ApiError(
            500,
            "evidence_integrity_failure",
            "The stored report does not match its recorded checksum and was not served.",
        )
    disposition = "attachment" if download else "inline"
    headers = {
        "Content-Disposition": f'{disposition}; filename="afterstate-report-{str(run.id)[:8]}.{format}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'",
        "X-Evidence-SHA256": ev.sha256,
        "Cache-Control": "private, max-age=60",
    }
    return Response(content=data, media_type=ev.content_type, headers=headers)


@router.get("/runs/{run_id}/evidence/execution_log")
def get_log(
    run_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ)), db: Session = Depends(get_db)
) -> Response:
    run = _run_or_404(db, ctx.id, run_id)
    ev = db.scalar(select(RunEvidence).where(RunEvidence.run_id == run.id, RunEvidence.kind == "execution_log"))
    if ev is None:
        raise ApiError(404, "log_unavailable", "The execution log is not available yet.")
    data = get_services().storage.read_bytes(ev.storage_key)
    if hashlib.sha256(data).hexdigest() != ev.sha256:
        raise ApiError(
            500, "evidence_integrity_failure", "The stored log does not match its recorded checksum and was not served."
        )
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"X-Content-Type-Options": "nosniff", "X-Evidence-SHA256": ev.sha256},
    )


# ----------------------------------------------------------------------------- overview ------------
def _readiness(
    p: RecoveryProject, latest: RecoveryRun | None, last_success: RecoveryRun | None, now: datetime
) -> tuple[str, str]:
    if latest is not None and latest.state in SLOT_STATES:
        return "running", "A recovery test is in progress."
    if latest is not None and latest.state in (RunState.FAILED, RunState.NEEDS_ATTENTION, RunState.NEEDS_CLEANUP):
        return "at_risk", f"The latest run ended {latest.state.value.replace('_', ' ').lower()}" + (
            f" ({latest.failure_class})." if latest.failure_class else "."
        )
    if last_success is None or last_success.finished_at is None:
        return "unverified", "No recovery test has succeeded yet."
    age_days = (now - last_success.finished_at).days
    if age_days > p.max_test_age_days:
        return "stale", f"The last successful test is {age_days} days old; the limit is {p.max_test_age_days}."
    return "verified", f"Verified {age_days} day(s) ago."


@router.get("/overview")
def overview(
    ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    now = utcnow()
    real = RecoveryRun.lab_scenario.is_(None)
    projects = db.scalars(
        select(RecoveryProject)
        .where(
            RecoveryProject.workspace_id == ctx.id,
            RecoveryProject.archived_at.is_(None),
            RecoveryProject.lab_scenario.is_(None),
        )
        .order_by(RecoveryProject.name)
    ).all()
    rows, counts, missed = [], {"verified": 0, "stale": 0, "at_risk": 0, "unverified": 0, "running": 0}, []
    for p in projects:
        latest = db.scalar(
            select(RecoveryRun)
            .where(RecoveryRun.project_id == p.id, RecoveryRun.state != RunState.CANCELLED)
            .order_by(RecoveryRun.queued_at.desc())
            .limit(1)
        )
        last_ok = db.scalar(
            select(RecoveryRun)
            .where(RecoveryRun.project_id == p.id, RecoveryRun.state == RunState.SUCCEEDED)
            .order_by(RecoveryRun.finished_at.desc())
            .limit(1)
        )
        status, reason = _readiness(p, latest, last_ok, now)
        counts[status] += 1
        if latest is not None and any(o.get("status") in ("MISSED",) for o in (latest.objectives or {}).values()):
            missed.append(
                {
                    "project_id": str(p.id),
                    "project": p.name,
                    "run_id": str(latest.id),
                    "objectives": {k: v.get("status") for k, v in latest.objectives.items()},
                }
            )
        rows.append(
            {
                "project_id": str(p.id),
                "name": p.name,
                "readiness": status,
                "reason": reason,
                "latest_run": ser.run_summary(latest) if latest else None,
                "last_success_at": ser.iso(last_ok.finished_at) if last_ok else None,
                "last_success_age_days": (now - last_ok.finished_at).days if last_ok and last_ok.finished_at else None,
                "max_test_age_days": p.max_test_age_days,
            }
        )
    verified_ids = set(
        db.scalars(
            select(RecoveryRun.artifact_id).where(
                RecoveryRun.workspace_id == ctx.id, RecoveryRun.state == RunState.SUCCEEDED
            )
        )
    )
    arts = db.scalars(
        select(BackupArtifact)
        .where(
            BackupArtifact.workspace_id == ctx.id,
            BackupArtifact.deleted_at.is_(None),
            BackupArtifact.lab_scenario.is_(None),
        )
        .order_by(BackupArtifact.created_at.desc())
    ).all()
    unverified = [a for a in arts if a.id not in verified_ids]
    since = now - timedelta(days=30)
    window = db.execute(
        select(RecoveryRun.state, func.count())
        .where(
            RecoveryRun.workspace_id == ctx.id,
            real,
            RecoveryRun.finished_at >= since,
            RecoveryRun.state.in_([RunState.SUCCEEDED, RunState.FAILED, RunState.NEEDS_ATTENTION]),
        )
        .group_by(RecoveryRun.state)
    ).all()
    wc = {s.value: n for s, n in window}
    finished = sum(wc.values())
    series = db.scalars(
        select(RecoveryRun)
        .where(
            RecoveryRun.workspace_id == ctx.id,
            real,
            RecoveryRun.finished_at.is_not(None),
            RecoveryRun.state.in_([RunState.SUCCEEDED, RunState.NEEDS_ATTENTION]),
        )
        .order_by(RecoveryRun.finished_at.desc())
        .limit(30)
    ).all()
    cleanup = int(
        db.scalar(
            select(func.count())
            .select_from(RecoveryRun)
            .where(RecoveryRun.workspace_id == ctx.id, RecoveryRun.state == RunState.NEEDS_CLEANUP)
        )
        or 0
    )
    recent = db.scalars(
        select(RecoveryRun)
        .where(RecoveryRun.workspace_id == ctx.id, real)
        .order_by(RecoveryRun.queued_at.desc())
        .limit(8)
    ).all()
    activity: list[dict[str, Any]] = []
    if role_allows(ctx.membership.role, Permission.AUDIT_READ):
        activity = [
            {
                "id": e.id,
                "at": ser.iso(e.created_at),
                "actor": e.actor_label,
                "action": e.action,
                "outcome": e.outcome,
                "target_type": e.target_type,
                "target_id": e.target_id,
            }
            for e in db.scalars(
                select(AuditEvent).where(AuditEvent.workspace_id == ctx.id).order_by(AuditEvent.id.desc()).limit(10)
            )
        ]
    return {
        "readiness": counts,
        "projects": rows,
        "projects_total": len(projects),
        "unverified_artifacts": {
            "count": len(unverified),
            "items": [
                {"id": str(a.id), "name": a.name, "created_at": ser.iso(a.created_at), "status": a.status.value}
                for a in unverified[:5]
            ],
        },
        "objectives_missed": missed,
        "cleanup_pending": cleanup,
        "failure_rate_30d": {
            "finished": finished,
            "failed": wc.get("FAILED", 0),
            "needs_attention": wc.get("NEEDS_ATTENTION", 0),
            "succeeded": wc.get("SUCCEEDED", 0),
            "rate": round((wc.get("FAILED", 0) + wc.get("NEEDS_ATTENTION", 0)) / finished, 3) if finished else None,
        },
        "series": [
            {
                "run_id": str(r.id),
                "at": ser.iso(r.finished_at),
                "project": (r.config or {}).get("project_name"),
                "state": r.state.value,
                "recovery_seconds": (r.metrics or {}).get("recovery_seconds"),
                "restore_seconds": (r.metrics or {}).get("restore_seconds"),
                "throughput_mb_s": (r.metrics or {}).get("restore_throughput_mb_s"),
            }
            for r in reversed(series)
        ],
        "recent_runs": [ser.run_summary(r) for r in recent],
        "activity": activity,
    }


# --------------------------------------------------------------------------- schedules ------------
@router.get("/schedules")
def list_schedules(
    ctx: WorkspaceCtx = Depends(require(Permission.SCHEDULE_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = db.scalars(select(Schedule).where(Schedule.workspace_id == ctx.id).order_by(Schedule.name)).all()
    names = {p.id: p.name for p in db.scalars(select(RecoveryProject).where(RecoveryProject.workspace_id == ctx.id))}
    return {"items": [{**ser.schedule(s), "project_name": names.get(s.project_id)} for s in rows]}


def _validated(cron: str, tz: str) -> None:
    try:
        validate_schedule(cron, tz)
    except InvalidSchedule as exc:
        raise ApiError(422, "invalid_schedule", str(exc)) from exc


@router.post("/schedules", status_code=201)
def create_schedule(
    body: ScheduleIn, ctx: WorkspaceCtx = Depends(require(Permission.SCHEDULE_WRITE)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _project_or_404(db, ctx.id, body.project_id)
    _validated(body.cron_expr, body.timezone)
    s = Schedule(
        workspace_id=ctx.id,
        project_id=body.project_id,
        name=body.name,
        cron_expr=body.cron_expr.strip(),
        timezone=body.timezone,
        enabled=body.enabled,
        missed_run_policy=body.missed_run_policy,
        retry_max_attempts=body.retry_max_attempts,
        retry_backoff_seconds=body.retry_backoff_seconds,
        created_by=ctx.auth.user.id,
        next_run_at=next_fire_after(body.cron_expr, body.timezone, utcnow()),
    )
    db.add(s)
    db.flush()
    record(
        db,
        ctx.actor,
        "schedule.create",
        workspace_id=ctx.id,
        target_type="schedule",
        target_id=s.id,
        details={"name": s.name, "cron": s.cron_expr, "tz": s.timezone},
    )
    db.commit()
    return ser.schedule(s)


@router.patch("/schedules/{schedule_id}")
def patch_schedule(
    schedule_id: uuid.UUID,
    body: SchedulePatch,
    ctx: WorkspaceCtx = Depends(require(Permission.SCHEDULE_WRITE)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    s = db.scalar(select(Schedule).where(Schedule.id == schedule_id, Schedule.workspace_id == ctx.id))
    if s is None:
        raise not_found("Schedule")
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(s, k, v.strip() if isinstance(v, str) and k == "cron_expr" else v)
    if {"cron_expr", "timezone", "enabled"} & set(data):
        _validated(s.cron_expr, s.timezone)
        s.next_run_at = next_fire_after(s.cron_expr, s.timezone, utcnow()) if s.enabled else None
    record(
        db,
        ctx.actor,
        "schedule.update",
        workspace_id=ctx.id,
        target_type="schedule",
        target_id=s.id,
        details={k: str(v) for k, v in data.items()},
    )
    db.commit()
    return ser.schedule(s)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require(Permission.SCHEDULE_WRITE)),
    db: Session = Depends(get_db),
) -> None:
    s = db.scalar(select(Schedule).where(Schedule.id == schedule_id, Schedule.workspace_id == ctx.id))
    if s is None:
        raise not_found("Schedule")
    record(
        db,
        ctx.actor,
        "schedule.delete",
        workspace_id=ctx.id,
        target_type="schedule",
        target_id=s.id,
        details={"name": s.name},
    )
    db.delete(s)
    db.commit()


@router.get("/schedules/{schedule_id}/firings")
def schedule_firings(
    schedule_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require(Permission.SCHEDULE_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.scalar(select(Schedule.id).where(Schedule.id == schedule_id, Schedule.workspace_id == ctx.id)) is None:
        raise not_found("Schedule")
    rows, total = paginate(
        db,
        select(ScheduleFiring)
        .where(ScheduleFiring.schedule_id == schedule_id)
        .order_by(ScheduleFiring.scheduled_for.desc()),
        page,
        page_size,
    )
    return {
        "items": [
            {
                "id": str(f.id),
                "scheduled_for": ser.iso(f.scheduled_for),
                "fired_at": ser.iso(f.fired_at),
                "outcome": f.outcome,
                "run_id": str(f.run_id) if f.run_id else None,
                "detail": f.detail,
            }
            for f in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ------------------------------------------------------------------------------ audit --------------
@router.get("/audit")
def audit_events(
    action: str | None = None,
    outcome: Literal["success", "denied", "failure"] | None = None,
    actor: str | None = None,
    target_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require(Permission.AUDIT_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(AuditEvent).where(AuditEvent.workspace_id == ctx.id)
    if action:
        stmt = stmt.where(AuditEvent.action.ilike(f"{action.strip()[:60]}%"))
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if actor:
        stmt = stmt.where(AuditEvent.actor_label.ilike(f"%{actor.strip()[:100]}%"))
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if since:
        stmt = stmt.where(AuditEvent.created_at >= since)
    if until:
        stmt = stmt.where(AuditEvent.created_at <= until)
    rows, total = paginate(db, stmt.order_by(AuditEvent.id.desc()), page, page_size)
    return {
        "items": [
            {
                "id": e.id,
                "at": ser.iso(e.created_at),
                "actor": e.actor_label,
                "action": e.action,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "outcome": e.outcome,
                "ip": e.ip,
                "correlation_id": e.correlation_id,
                "details": e.details,
            }
            for e in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# -------------------------------------------------------------------------------- lab --------------
def _lab_enabled() -> None:
    if not get_services().settings.lab_enabled:
        raise ApiError(404, "lab_disabled", "The Recovery Lab is disabled on this installation.")


@router.get("/lab/scenarios")
def lab_scenarios(ctx: WorkspaceCtx = Depends(require(Permission.RUN_READ))) -> dict[str, Any]:
    _lab_enabled()
    svc = get_services()
    out = []
    for sc in SCENARIOS:
        st = lab.evaluate(svc, ctx.id, sc)
        out.append(
            {
                "key": sc.key,
                "title": sc.title,
                "summary": sc.summary,
                "initial_conditions": sc.initial_conditions,
                "expected": sc.expected,
                "fault": sc.fault_description,
                "status": st.state,
                "note": st.note,
                "runs": st.runs,
                "multi_run": sc.multi_run,
            }
        )
    return {
        "items": out,
        "isolation": "Every scenario runs the real recovery engine against real PostgreSQL archives in disposable environments. Faults are honoured only for lab runs.",
    }


@router.post("/lab/scenarios/{key}/run", status_code=202)
async def lab_run(key: str, ctx: WorkspaceCtx = Depends(require(Permission.LAB_RUN))) -> dict[str, Any]:
    _lab_enabled()
    if key not in BY_KEY:
        raise not_found("Scenario")
    svc = get_services()
    try:
        run_id = await run_in_threadpool(lab.start_scenario, svc, ctx.id, ctx.auth.user.id, key)
    except lab.LabBusy as exc:
        raise ApiError(409, "lab_busy", str(exc)) from exc
    except ActiveRunExists as exc:
        raise ApiError(409, "lab_busy", str(exc)) from exc
    with svc.sf() as s, s.begin():
        record(
            s,
            ctx.actor,
            "lab.run",
            workspace_id=ctx.id,
            target_type="lab_scenario",
            target_id=None,
            details={"scenario": key, "run_id": str(run_id)},
        )
    return {"run_id": str(run_id)}


@router.post("/lab/scenarios/{key}/reset")
def lab_reset(key: str, ctx: WorkspaceCtx = Depends(require(Permission.LAB_RUN))) -> dict[str, Any]:
    _lab_enabled()
    if key not in BY_KEY:
        raise not_found("Scenario")
    svc = get_services()
    try:
        removed = lab.reset_scenario(svc, ctx.id, key)
    except lab.LabBusy as exc:
        raise ApiError(409, "lab_busy", str(exc)) from exc
    with svc.sf() as s, s.begin():
        record(
            s,
            ctx.actor,
            "lab.reset",
            workspace_id=ctx.id,
            target_type="lab_scenario",
            details={"scenario": key, **removed},
        )
    return {"removed": removed, "note": "Only resources owned by this scenario were deleted."}


# ------------------------------------------------------------------------------- health -------------
health = APIRouter(prefix="/api/v1/health")


@health.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    from sqlalchemy import text

    from afterstate.db.models import WorkerHeartbeat

    svc = get_services()
    checks: dict[str, Any] = {}
    try:
        with svc.sf() as s:
            s.execute(text("SELECT 1"))
            newest = s.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
        checks["database"] = "ok"
        age = (datetime.now(UTC) - newest).total_seconds() if newest else None
        checks["worker"] = {
            "status": "ok" if age is not None and age < 30 else "stale",
            "last_seen_seconds_ago": int(age) if age is not None else None,
        }
    except Exception:
        checks["database"] = "unavailable"
    try:
        key = f"health/{uuid.uuid4().hex}"
        svc.storage.write_bytes(key, b"ok")
        svc.storage.delete(key)
        checks["storage"] = "ok"
    except Exception:
        checks["storage"] = "unavailable"
    checks["postgresql_tools"] = svc.tools.version
    ok = checks.get("database") == "ok" and checks.get("storage") == "ok"
    if not ok:
        response.status_code = 503
    return {
        "status": "ready" if ok else "not_ready",
        "checks": checks,
        "note": "worker staleness is reported but does not fail readiness: the API can serve while the queue waits",
    }
