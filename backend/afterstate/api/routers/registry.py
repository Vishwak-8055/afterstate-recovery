from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from afterstate.artifacts import ChecksumMismatch, register_stored_artifact, verify_and_record
from afterstate.audit import record
from afterstate.db.base import utcnow
from afterstate.db.models import ArtifactVerification, BackupArtifact, RecoveryProject, RecoveryRun
from afterstate.domain.enums import ArtifactStatus, RuleKind
from afterstate.domain.rules import rule_category
from afterstate.domain.state_machine import SLOT_STATES
from afterstate.engine.appsuite import SUITES
from afterstate.engine.restore import SKIPPED_CLASSES
from afterstate.projects import replace_rules
from afterstate.runs import resolve_artifact
from afterstate.security.rbac import Permission
from afterstate.storage.base import ObjectTooLarge
from afterstate.storage.local import new_artifact_key

from .. import serializers as ser
from ..deps import WorkspaceCtx, get_db, get_services, require
from ..errors import ApiError, not_found
from ..schemas import ArtifactPatch, ProjectIn

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}")


def paginate(db: Session, stmt: Any, page: int, size: int) -> tuple[list[Any], int]:
    total = int(db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    rows = db.scalars(stmt.limit(size).offset((page - 1) * size)).all()
    return list(rows), total


def _artifact_or_404(db: Session, ws_id: uuid.UUID, artifact_id: uuid.UUID) -> BackupArtifact:
    art = db.scalar(
        select(BackupArtifact).where(
            BackupArtifact.id == artifact_id, BackupArtifact.workspace_id == ws_id, BackupArtifact.deleted_at.is_(None)
        )
    )
    if art is None:
        raise not_found("Backup artifact")
    return art


# ----------------------------------------------------------------------------- artifacts ----------
@router.post("/artifacts", status_code=201)
async def upload_artifact(
    request: Request,
    name: str = Query(..., min_length=1, max_length=200),
    label: str = Query("", max_length=120),
    filename: str = Query("backup.dump", max_length=255),
    expected_sha256: str | None = Query(None, pattern="^[0-9a-fA-F]{64}$"),
    backup_created_at: datetime | None = Query(None),
    notes: str = Query("", max_length=2000),
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_WRITE)),
) -> dict[str, Any]:
    """Raw-body upload (application/octet-stream). The body is streamed to disk with a hard size limit
    and hashed on the fly; the stored file is then verified before it is ever offered for restore."""
    svc = get_services()
    if not request.headers.get("content-type", "").startswith("application/octet-stream"):
        raise ApiError(
            415,
            "unsupported_media_type",
            "Upload the backup as the raw request body with Content-Type: application/octet-stream.",
        )
    declared = request.headers.get("content-length")
    limit = svc.settings.max_artifact_bytes
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "artifact_too_large", f"Backups are limited to {limit} bytes on this installation.")
    key = new_artifact_key(ctx.id)
    handle = svc.storage.open_upload(key, max_bytes=limit)  # type: ignore[attr-defined]
    try:
        async for chunk in request.stream():
            await run_in_threadpool(handle.write, chunk)
        stored = await run_in_threadpool(handle.commit)
    except ObjectTooLarge as exc:
        handle.abort()
        raise ApiError(
            413, "artifact_too_large", f"Backups are limited to {limit} bytes on this installation."
        ) from exc
    except BaseException:
        handle.abort()
        raise
    if stored.size_bytes == 0:
        svc.storage.delete(key)
        raise ApiError(422, "empty_upload", "The upload was empty.")
    safe_name = Path(filename).name or "backup.dump"

    def register() -> dict[str, Any]:
        try:
            art, _ver, insp = register_stored_artifact(
                svc,
                workspace_id=ctx.id,
                user_id=ctx.auth.user.id,
                stored=stored,
                name=name,
                label=label.strip(),
                original_filename=safe_name,
                expected_sha256=expected_sha256,
                backup_created_at=backup_created_at,
                notes=notes,
            )
        except ChecksumMismatch as exc:
            svc.storage.delete(key)
            raise ApiError(
                422,
                "checksum_mismatch",
                "The uploaded bytes do not match the SHA-256 you declared. Nothing was stored.",
                {"expected": exc.expected, "actual": exc.actual},
            ) from exc
        with svc.sf() as s, s.begin():
            record(
                s,
                ctx.actor,
                "artifact.register",
                workspace_id=ctx.id,
                target_type="artifact",
                target_id=art.id,
                outcome="success" if insp.ok else "failure",
                details={
                    "name": name,
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "status": art.status.value,
                    "failure_class": insp.failure_class.value if insp.failure_class else None,
                },
            )
        return {
            **ser.artifact(art),
            "verification": {
                "ok": insp.ok,
                "failure_class": insp.failure_class.value if insp.failure_class else None,
                "message": insp.failure_message,
                "checks": [
                    {"key": c.key, "name": c.name, "outcome": c.outcome.value, "message": c.message}
                    for c in insp.checks
                ],
            },
        }

    return await run_in_threadpool(register)


@router.get("/artifacts")
def list_artifacts(
    q: str | None = None,
    status: ArtifactStatus | None = None,
    label: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(BackupArtifact).where(BackupArtifact.workspace_id == ctx.id, BackupArtifact.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.where(
            or_(
                BackupArtifact.name.ilike(like),
                BackupArtifact.label.ilike(like),
                BackupArtifact.original_filename.ilike(like),
                BackupArtifact.sha256.ilike(f"{q.strip()[:64]}%"),
            )
        )
    if status:
        stmt = stmt.where(BackupArtifact.status == status)
    if label:
        stmt = stmt.where(BackupArtifact.label == label)
    rows, total = paginate(db, stmt.order_by(BackupArtifact.created_at.desc()), page, page_size)
    ids = [a.id for a in rows]
    verified: dict[uuid.UUID, tuple[int, datetime | None]] = {}
    if ids:
        for aid, ok, last in db.execute(
            select(
                RecoveryRun.artifact_id,
                func.count().filter(RecoveryRun.state == "SUCCEEDED"),
                func.max(RecoveryRun.finished_at),
            )
            .where(RecoveryRun.artifact_id.in_(ids))
            .group_by(RecoveryRun.artifact_id)
        ).all():
            verified[aid] = (ok, last)
    items = [
        {
            **ser.artifact(a),
            "restore_verified": verified.get(a.id, (0, None))[0] > 0,
            "last_restore_at": ser.iso(verified.get(a.id, (0, None))[1]),
        }
        for a in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    art = _artifact_or_404(db, ctx.id, artifact_id)
    vers = db.scalars(
        select(ArtifactVerification)
        .where(ArtifactVerification.artifact_id == art.id)
        .order_by(ArtifactVerification.started_at.desc())
        .limit(25)
    ).all()
    projects = db.scalars(
        select(RecoveryProject).where(
            RecoveryProject.workspace_id == ctx.id,
            RecoveryProject.artifact_id == art.id,
            RecoveryProject.archived_at.is_(None),
        )
    ).all()
    runs = db.scalars(
        select(RecoveryRun).where(RecoveryRun.artifact_id == art.id).order_by(RecoveryRun.queued_at.desc()).limit(10)
    ).all()
    return {
        **ser.artifact(art),
        "verifications": [
            {
                "id": str(v.id),
                "kind": v.kind,
                "outcome": v.outcome.value,
                "started_at": ser.iso(v.started_at),
                "duration_ms": v.duration_ms,
                "sha256": v.sha256,
                "failure_class": v.failure_class,
                "error": v.error,
                "run_id": str(v.run_id) if v.run_id else None,
                "checks": (v.details or {}).get("checks", []),
            }
            for v in vers
        ],
        "projects": [{"id": str(p.id), "name": p.name} for p in projects],
        "runs": [ser.run_summary(r) for r in runs],
    }


@router.post("/artifacts/{artifact_id}/verify")
def verify_artifact(
    artifact_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_VERIFY)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _artifact_or_404(db, ctx.id, artifact_id)
    svc = get_services()
    art, ver, insp = verify_and_record(svc, artifact_id, kind="manual", triggered_by=ctx.auth.user.id)
    record(
        db,
        ctx.actor,
        "artifact.verify",
        workspace_id=ctx.id,
        target_type="artifact",
        target_id=artifact_id,
        outcome="success" if insp.ok else "failure",
        details={"failure_class": insp.failure_class.value if insp.failure_class else None},
    )
    db.commit()
    return {
        **ser.artifact(art),
        "verification": {
            "ok": insp.ok,
            "failure_class": insp.failure_class.value if insp.failure_class else None,
            "message": insp.failure_message,
            "checks": [
                {"key": c.key, "name": c.name, "outcome": c.outcome.value, "message": c.message} for c in insp.checks
            ],
        },
    }


@router.patch("/artifacts/{artifact_id}")
def patch_artifact(
    artifact_id: uuid.UUID,
    body: ArtifactPatch,
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_WRITE)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    art = _artifact_or_404(db, ctx.id, artifact_id)
    changed = {}
    for field in ("name", "label", "notes", "backup_created_at"):
        value = getattr(body, field)
        if value is not None:
            setattr(art, field, value)
            changed[field] = str(value)
    record(
        db, ctx.actor, "artifact.update", workspace_id=ctx.id, target_type="artifact", target_id=art.id, details=changed
    )
    db.commit()
    return ser.artifact(art)


@router.delete("/artifacts/{artifact_id}", status_code=204)
def delete_artifact(
    artifact_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require(Permission.ARTIFACT_DELETE)),
    db: Session = Depends(get_db),
) -> None:
    art = _artifact_or_404(db, ctx.id, artifact_id)
    if db.scalar(
        select(RecoveryRun.id).where(RecoveryRun.artifact_id == art.id, RecoveryRun.state.in_(SLOT_STATES)).limit(1)
    ):
        raise ApiError(
            409, "artifact_in_use", "A recovery run is using this artifact. Wait for it to finish or cancel it."
        )
    art.deleted_at = utcnow()
    for p in db.scalars(select(RecoveryProject).where(RecoveryProject.artifact_id == art.id)):
        p.artifact_id = None
    record(
        db,
        ctx.actor,
        "artifact.delete",
        workspace_id=ctx.id,
        target_type="artifact",
        target_id=art.id,
        details={"name": art.name, "sha256": art.sha256},
    )
    db.commit()
    get_services().storage.delete(art.storage_key)


# ----------------------------------------------------------------------------- projects -----------
RULE_CATALOG: list[dict[str, Any]] = [
    {
        "kind": "schema",
        "label": "Schema exists",
        "stage": "structural",
        "help": "The schema is present in the restored database.",
        "fields": [{"name": "schema", "label": "Schema", "type": "text", "required": True, "placeholder": "billing"}],
    },
    {
        "kind": "table",
        "label": "Table or view exists",
        "stage": "structural",
        "help": "A relation of the given kind exists.",
        "fields": [
            {"name": "schema", "label": "Schema", "type": "text", "default": "public"},
            {"name": "table", "label": "Name", "type": "text", "required": True, "placeholder": "orders"},
            {
                "name": "relation",
                "label": "Kind",
                "type": "select",
                "options": ["table", "view", "materialized_view"],
                "default": "table",
            },
        ],
    },
    {
        "kind": "column",
        "label": "Column definition",
        "stage": "structural",
        "help": "Column exists with the declared type and nullability.",
        "fields": [
            {"name": "schema", "label": "Schema", "type": "text", "default": "public"},
            {"name": "table", "label": "Table", "type": "text", "required": True},
            {"name": "column", "label": "Column", "type": "text", "required": True},
            {"name": "data_type", "label": "Data type", "type": "text", "placeholder": "bigint, text, numeric(12,2)"},
            {"name": "nullable", "label": "Nullable", "type": "tristate"},
        ],
    },
    {
        "kind": "constraint",
        "label": "Constraint",
        "stage": "structural",
        "help": "A primary key, unique, foreign key or check constraint exists.",
        "fields": [
            {"name": "schema", "label": "Schema", "type": "text", "default": "public"},
            {"name": "table", "label": "Table", "type": "text", "required": True},
            {
                "name": "constraint_type",
                "label": "Type",
                "type": "select",
                "options": ["primary_key", "unique", "foreign_key", "check"],
                "required": True,
            },
            {"name": "constraint_name", "label": "Constraint name", "type": "text"},
            {"name": "columns", "label": "Columns", "type": "list", "placeholder": "id"},
            {
                "name": "references",
                "label": "References (foreign keys)",
                "type": "text",
                "placeholder": "public.customers",
            },
        ],
    },
    {
        "kind": "index",
        "label": "Index",
        "stage": "structural",
        "help": "A valid index exists, by name or leading columns.",
        "fields": [
            {"name": "schema", "label": "Schema", "type": "text", "default": "public"},
            {"name": "table", "label": "Table", "type": "text", "required": True},
            {"name": "index_name", "label": "Index name", "type": "text"},
            {"name": "columns", "label": "Leading columns", "type": "list"},
            {"name": "unique", "label": "Unique", "type": "tristate"},
        ],
    },
    {
        "kind": "extension",
        "label": "Extension installed",
        "stage": "structural",
        "help": "The extension is present in the restored database.",
        "fields": [
            {"name": "extension", "label": "Extension", "type": "text", "required": True, "placeholder": "pgcrypto"}
        ],
    },
    {
        "kind": "row_count",
        "label": "Row count",
        "stage": "data",
        "help": "Partial check: the table has the expected number of rows (exact or range).",
        "fields": [
            {"name": "schema", "label": "Schema", "type": "text", "default": "public"},
            {"name": "table", "label": "Table", "type": "text", "required": True},
            {"name": "exact", "label": "Exactly", "type": "number"},
            {"name": "minimum", "label": "At least", "type": "number"},
            {"name": "maximum", "label": "At most", "type": "number"},
        ],
    },
    {
        "kind": "sql_assertion",
        "label": "SQL assertion",
        "stage": "data",
        "help": "Partial check: a read-only query that must return true, no rows, an exact value or a range. Runs as a non-superuser in a read-only transaction.",
        "fields": [
            {
                "name": "sql",
                "label": "Query",
                "type": "textarea",
                "required": True,
                "placeholder": "SELECT NOT EXISTS (SELECT 1 FROM orders WHERE total_cents < 0)",
            },
            {
                "name": "expect",
                "label": "Expect",
                "type": "select",
                "options": ["true", "no_rows", "equals", "between"],
                "default": "true",
            },
            {"name": "value", "label": "Value (equals)", "type": "text"},
            {"name": "minimum", "label": "Minimum (between)", "type": "number"},
            {"name": "maximum", "label": "Maximum (between)", "type": "number"},
        ],
    },
    {
        "kind": "dataset_checksum",
        "label": "Reference dataset checksum",
        "stage": "data",
        "help": "Complete comparison: SHA-256 over every row of an ordered query, compared with a value computed on the source (see `afterstate dataset-checksum`).",
        "fields": [
            {
                "name": "sql",
                "label": "Ordered query",
                "type": "textarea",
                "required": True,
                "placeholder": "SELECT id, email FROM customers ORDER BY id",
            },
            {"name": "expected_sha256", "label": "Expected SHA-256", "type": "text", "required": True},
            {"name": "expected_rows", "label": "Expected rows", "type": "number"},
        ],
    },
    {
        "kind": "recovery_point",
        "label": "Recovery-point probe",
        "stage": "data",
        "help": "A query returning the newest transaction timestamp in the data. Used as RPO evidence; never guessed.",
        "fields": [
            {
                "name": "sql",
                "label": "Query",
                "type": "textarea",
                "required": True,
                "placeholder": "SELECT max(updated_at) FROM orders",
            }
        ],
    },
    {
        "kind": "http_check",
        "label": "Application HTTP check",
        "stage": "application",
        "help": "GET request against the predefined application suite started on the restored database.",
        "fields": [
            {"name": "path", "label": "Path", "type": "text", "required": True, "placeholder": "/healthz"},
            {"name": "expected_status", "label": "Status", "type": "number", "default": 200},
            {"name": "max_latency_ms", "label": "Max latency (ms)", "type": "number", "default": 2000},
            {"name": "body_contains", "label": "Body contains", "type": "text"},
        ],
    },
]


@router.get("/meta")
def meta(ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_READ))) -> dict[str, Any]:
    svc = get_services()
    return {
        "rule_catalog": RULE_CATALOG,
        "app_suites": [{"key": k, "description": v} for k, v in SUITES.items()],
        "restore_skipped_object_classes": list(SKIPPED_CLASSES),
        "postgresql": svc.tools.version,
        "formats": [{"key": "custom", "label": "pg_dump -Fc (custom)"}, {"key": "tar", "label": "pg_dump -Ft (tar)"}],
    }


@router.post("/rules/validate")
def validate_rule(
    body: dict[str, Any], ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_READ))
) -> dict[str, Any]:
    from afterstate.domain.rules import dump_rule, parse_rule

    try:
        rule = parse_rule(body)
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_rule",
            "The rule is not valid.",
            [
                {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                for e in exc.errors(include_url=False, include_context=False, include_input=False)
            ],
        ) from exc
    return {
        "valid": True,
        "normalized": dump_rule(rule),
        "name": rule.display_name(),
        "stage": rule_category(rule.kind),
    }


def _project_or_404(db: Session, ws_id: uuid.UUID, project_id: uuid.UUID) -> RecoveryProject:
    p = db.scalar(
        select(RecoveryProject).where(
            RecoveryProject.id == project_id,
            RecoveryProject.workspace_id == ws_id,
            RecoveryProject.archived_at.is_(None),
        )
    )
    if p is None:
        raise not_found("Recovery project")
    return p


def _apply_project(db: Session, ctx: WorkspaceCtx, project: RecoveryProject, body: ProjectIn) -> None:
    from afterstate.domain.rules import RestoreOptions

    if body.artifact_id is not None:
        _artifact_or_404(db, ctx.id, body.artifact_id)
    if body.app_suite is not None and body.app_suite not in SUITES:
        raise ApiError(422, "unknown_app_suite", f"Unknown application suite '{body.app_suite}'.")
    try:
        options = RestoreOptions.model_validate(body.restore_options).model_dump()
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_restore_options",
            "Restore options are not valid.",
            [
                {"message": e["msg"], "field": ".".join(str(p) for p in e["loc"])}
                for e in exc.errors(include_url=False, include_context=False, include_input=False)
            ],
        ) from exc
    project.name, project.description = body.name, body.description
    project.artifact_id, project.artifact_label = body.artifact_id, (body.artifact_label or None)
    project.restore_options = options
    project.restore_timeout_seconds, project.validation_timeout_seconds = (
        body.restore_timeout_seconds,
        body.validation_timeout_seconds,
    )
    project.rto_seconds, project.rpo_seconds = body.rto_seconds, body.rpo_seconds
    project.max_test_age_days, project.max_attempts, project.app_suite = (
        body.max_test_age_days,
        body.max_attempts,
        body.app_suite,
    )
    try:
        replace_rules(db, project, body.rules)
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_rules",
            "One or more validation rules are not valid.",
            [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors(include_url=False, include_context=False, include_input=False)
            ],
        ) from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_rules", str(exc)) from exc


@router.get("/projects")
def list_projects(
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_READ)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(RecoveryProject).where(RecoveryProject.workspace_id == ctx.id, RecoveryProject.archived_at.is_(None))
    if q:
        stmt = stmt.where(RecoveryProject.name.ilike(f"%{q.strip()[:100]}%"))
    rows, total = paginate(db, stmt.order_by(RecoveryProject.name), page, page_size)
    out = []
    for p in rows:
        latest = db.scalar(
            select(RecoveryRun).where(RecoveryRun.project_id == p.id).order_by(RecoveryRun.queued_at.desc()).limit(1)
        )
        out.append(
            {
                **ser.project(p, rules=False),
                "rule_count": len(p.rules),
                "latest_run": ser.run_summary(latest) if latest else None,
            }
        )
    return {"items": out, "total": total, "page": page, "page_size": page_size}


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectIn, ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_WRITE)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = RecoveryProject(workspace_id=ctx.id, name=body.name, created_by=ctx.auth.user.id)
    db.add(project)
    try:
        _apply_project(db, ctx, project, body)
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "name_in_use", "A recovery project with this name already exists.") from exc
    record(
        db,
        ctx.actor,
        "project.create",
        workspace_id=ctx.id,
        target_type="project",
        target_id=project.id,
        details={"name": project.name, "rules": len(body.rules)},
    )
    db.commit()
    db.refresh(project)
    return ser.project(project)


@router.get("/projects/{project_id}")
def get_project(
    project_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return ser.project(_project_or_404(db, ctx.id, project_id))


@router.put("/projects/{project_id}")
def update_project(
    project_id: uuid.UUID,
    body: ProjectIn,
    ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_WRITE)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project_or_404(db, ctx.id, project_id)
    try:
        _apply_project(db, ctx, project, body)
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "name_in_use", "A recovery project with this name already exists.") from exc
    record(
        db,
        ctx.actor,
        "project.update",
        workspace_id=ctx.id,
        target_type="project",
        target_id=project.id,
        details={"name": project.name, "rules": len(body.rules)},
    )
    db.commit()
    db.refresh(project)
    return ser.project(project)


@router.delete("/projects/{project_id}", status_code=204)
def archive_project(
    project_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_WRITE)), db: Session = Depends(get_db)
) -> None:
    from afterstate.db.models import Schedule

    project = _project_or_404(db, ctx.id, project_id)
    if db.scalar(
        select(RecoveryRun.id).where(RecoveryRun.project_id == project.id, RecoveryRun.state.in_(SLOT_STATES)).limit(1)
    ):
        raise ApiError(409, "project_busy", "A run for this project is queued or in progress.")
    project.archived_at = utcnow()
    for s in db.scalars(select(Schedule).where(Schedule.project_id == project.id)):
        s.enabled = False
    record(
        db,
        ctx.actor,
        "project.archive",
        workspace_id=ctx.id,
        target_type="project",
        target_id=project.id,
        details={"name": project.name},
    )
    db.commit()


@router.get("/projects/{project_id}/plan")
def execution_plan(
    project_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.PROJECT_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """What a run would do right now. Derived from the same configuration the worker will snapshot."""
    p = _project_or_404(db, ctx.id, project_id)
    art = resolve_artifact(db, p)
    warnings: list[str] = []
    if art is None:
        warnings.append(
            "No backup artifact is selected or available. Pin an artifact or set a label that matches registered backups."
        )
    elif art.status is ArtifactStatus.REJECTED:
        warnings.append(
            f"The selected artifact was rejected by verification ({art.status_detail}). The run will fail at artifact verification."
        )
    user_rules = list(p.rules)
    if not user_rules:
        warnings.append(
            "No validation rules are defined. A run can only prove that a restore completes and the database is not empty."
        )
    if any(r.kind == "http_check" for r in user_rules) and not p.app_suite:
        warnings.append(
            "HTTP checks are defined but no application suite is selected; they will be SKIPPED and the run cannot succeed."
        )
    if (
        p.rpo_seconds
        and not any(r.kind == "recovery_point" for r in user_rules)
        and not (art and art.backup_created_at)
    ):
        warnings.append(
            "An RPO is set but there is no recovery-point probe or declared backup creation time, so the RPO will be reported as UNKNOWN."
        )
    if p.rto_seconds and p.rto_seconds < 10:
        warnings.append("An RTO under 10 seconds is shorter than the time needed to provision a database cluster.")
    return {
        "artifact": ser.artifact(art) if art else None,
        "stages": [
            {
                "state": "PREPARING",
                "name": "Artifact verification",
                "detail": "SHA-256 against the recorded value, format, table of contents, version compatibility and a full read of the archive.",
            },
            {
                "state": "PREPARING",
                "name": "Environment provisioning",
                "detail": "Private PostgreSQL cluster in a 0700 directory, unix socket only (no network), unprivileged restore role, read-only validator role.",
            },
            {
                "state": "RESTORING",
                "name": "Restore",
                "detail": f"pg_restore --no-owner --no-privileges --exit-on-error, jobs={p.restore_options.get('jobs', 1)}, timeout {p.restore_timeout_seconds}s.",
            },
            {"state": "VALIDATING", "name": "Structural validation", "detail": "Baseline checks plus structure rules."},
            {
                "state": "VALIDATING",
                "name": "Data validation",
                "detail": "Read-only assertions, counts, dataset comparisons, recovery-point probe.",
            },
            {
                "state": "VALIDATING",
                "name": "Application validation",
                "detail": f"Suite '{p.app_suite}' on the restored database."
                if p.app_suite
                else "Skipped: no application suite selected.",
            },
            {
                "state": "FINALIZING",
                "name": "Cleanup and report",
                "detail": "Verified teardown of every temporary resource, evidence with checksums, JSON and HTML report.",
            },
        ],
        "restore": {
            "command_template": [
                "pg_restore",
                "--host",
                "<private socket>",
                "--username",
                "afterstate_restore",
                "--dbname",
                "restored",
                "--no-password",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--verbose",
                "--use-list",
                "<policy-filtered TOC>",
                "<artifact>",
            ],
            "skipped_object_classes": list(SKIPPED_CLASSES),
            "timeout_seconds": p.restore_timeout_seconds,
            "validation_budget_seconds": p.validation_timeout_seconds,
        },
        "rules": [
            {
                "position": r.position,
                "name": r.name,
                "kind": r.kind,
                "required": r.required,
                "stage": rule_category(r.kind),
            }
            for r in user_rules
        ],
        "baseline_checks": ["Restored database accepts connections", "Restored database contains user tables"],
        "objectives": {
            "rto_seconds": p.rto_seconds,
            "rpo_seconds": p.rpo_seconds,
            "max_test_age_days": p.max_test_age_days,
        },
        "retry": {
            "max_attempts": p.max_attempts,
            "retryable": ["ENVIRONMENT_UNAVAILABLE", "WORKER_INTERRUPTED", "INTERNAL_ERROR"],
        },
        "warnings": warnings,
        "rule_kinds": [k.value for k in RuleKind],
    }
