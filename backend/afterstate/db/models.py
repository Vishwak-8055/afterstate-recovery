"""Platform metadata schema. All tenant data carries workspace_id; access is always scoped by it."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from afterstate.db.base import Base, enum_type, new_id, utcnow
from afterstate.domain.enums import (
    ArtifactFormat,
    ArtifactStatus,
    CleanupTaskState,
    Outcome,
    Role,
    RunCleanupState,
    RunState,
    StageName,
    StageState,
    Trigger,
)

JSON = JSONB


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=new_id)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=2)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    webhook_url: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"), Index("ix_memberships_user", "user_id"))
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(enum_type(Role))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    user: Mapped[User] = relationship(lazy="joined")


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)


class BackupArtifact(Base):
    __tablename__ = "backup_artifacts"
    __table_args__ = (
        Index("ix_artifacts_ws_created", "workspace_id", "created_at"),
        Index("ix_artifacts_ws_label", "workspace_id", "label"),
    )
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(120), default="")
    name: Mapped[str] = mapped_column(String(200))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(300), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    expected_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    format: Mapped[ArtifactFormat] = mapped_column(enum_type(ArtifactFormat), default=ArtifactFormat.UNKNOWN)
    status: Mapped[ArtifactStatus] = mapped_column(enum_type(ArtifactStatus), default=ArtifactStatus.UNVERIFIED)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    dump_server_version: Mapped[str | None] = mapped_column(String(100), default=None)
    dump_tool_version: Mapped[str | None] = mapped_column(String(100), default=None)
    source_database: Mapped[str | None] = mapped_column(String(200), default=None)
    archive_created_at: Mapped[datetime | None] = mapped_column(default=None)
    backup_created_at: Mapped[datetime | None] = mapped_column(default=None)
    toc_entries: Mapped[int | None] = mapped_column(Integer, default=None)
    compression: Mapped[str | None] = mapped_column(String(40), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    lab_scenario: Mapped[str | None] = mapped_column(String(64), default=None, index=True)


class ArtifactVerification(Base):
    __tablename__ = "artifact_verifications"
    __table_args__ = (Index("ix_artver_artifact_started", "artifact_id", "started_at"),)
    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backup_artifacts.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    kind: Mapped[str] = mapped_column(String(16))  # upload | manual | run
    outcome: Mapped[Outcome] = mapped_column(enum_type(Outcome))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    format: Mapped[str | None] = mapped_column(String(16), default=None)
    failure_class: Mapped[str | None] = mapped_column(String(40), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)


class RecoveryProject(Base):
    __tablename__ = "recovery_projects"
    __table_args__ = (
        Index("uq_projects_ws_name", "workspace_id", "name", unique=True, postgresql_where=text("archived_at IS NULL")),
    )
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("backup_artifacts.id", ondelete="SET NULL"), default=None
    )
    artifact_label: Mapped[str | None] = mapped_column(String(120), default=None)
    restore_options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    restore_timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    validation_timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    rto_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    rpo_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    max_test_age_days: Mapped[int] = mapped_column(Integer, default=30)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    app_suite: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
    lab_scenario: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    rules: Mapped[list[ValidationRule]] = relationship(
        back_populates="project", order_by="ValidationRule.position", cascade="all, delete-orphan"
    )


class ValidationRule(Base):
    __tablename__ = "validation_rules"
    __table_args__ = (Index("ix_rules_project_position", "project_id", "position"),)
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    project: Mapped[RecoveryProject] = relationship(back_populates="rules")


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_due", "enabled", "next_run_at"),)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    cron_expr: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    missed_run_policy: Mapped[str] = mapped_column(String(16), default="skip")  # skip | run_once
    retry_max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    retry_backoff_seconds: Mapped[int] = mapped_column(Integer, default=300)
    next_run_at: Mapped[datetime | None] = mapped_column(default=None)
    last_fired_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RecoveryRun(Base):
    __tablename__ = "recovery_runs"
    __table_args__ = (
        Index("ix_runs_ws_queued", "workspace_id", "queued_at"),
        Index("ix_runs_project_queued", "project_id", "queued_at"),
        Index("ix_runs_state", "state"),
        Index(
            "uq_runs_one_slot_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("state IN ('QUEUED','PREPARING','RESTORING','VALIDATING','FINALIZING')"),
        ),
        Index(
            "uq_runs_idempotency",
            "workspace_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_projects.id", ondelete="RESTRICT"))
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backup_artifacts.id", ondelete="RESTRICT"))
    trigger: Mapped[Trigger] = mapped_column(enum_type(Trigger))
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"), default=None)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_runs.id", ondelete="SET NULL"), default=None
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), default=None)
    state: Mapped[RunState] = mapped_column(enum_type(RunState), default=RunState.QUEUED)
    settled_state: Mapped[RunState | None] = mapped_column(enum_type(RunState), default=None)
    failure_class: Mapped[str | None] = mapped_column(String(40), default=None)
    failure_message: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_state: Mapped[RunCleanupState] = mapped_column(
        enum_type(RunCleanupState), default=RunCleanupState.NOT_REQUIRED
    )
    queued_at: Mapped[datetime] = mapped_column(default=utcnow)
    not_before: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    state_entered_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(100), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    objectives: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fault: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    lab_scenario: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    stages: Mapped[list[RunStage]] = relationship(order_by="RunStage.id", cascade="all, delete-orphan")


class RunStage(Base):
    __tablename__ = "run_stages"
    __table_args__ = (UniqueConstraint("run_id", "name"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[StageName] = mapped_column(enum_type(StageName))
    state: Mapped[StageState] = mapped_column(enum_type(StageState), default=StageState.PENDING)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_runs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    level: Mapped[str] = mapped_column(String(8))
    stage: Mapped[str | None] = mapped_column(String(40), default=None)
    kind: Mapped[str] = mapped_column(String(16), default="log")  # log | stage | state | fault | system
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)


class ValidationResult(Base):
    __tablename__ = "validation_results"
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_runs.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(300))
    outcome: Mapped[Outcome] = mapped_column(enum_type(Outcome))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    expected: Mapped[Any] = mapped_column(JSON, nullable=True, default=None)
    actual: Mapped[Any] = mapped_column(JSON, nullable=True, default=None)
    coverage: Mapped[str | None] = mapped_column(String(12), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RunEvidence(Base):
    __tablename__ = "run_evidence"
    __table_args__ = (UniqueConstraint("run_id", "kind"),)
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CleanupTask(Base):
    __tablename__ = "cleanup_tasks"
    __table_args__ = (Index("ix_cleanup_state", "state"),)
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_runs.id", ondelete="CASCADE"), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))  # environment | app_process
    resource_ref: Mapped[str] = mapped_column(String(500))
    state: Mapped[CleanupTaskState] = mapped_column(enum_type(CleanupTaskState), default=CleanupTaskState.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)


class ScheduleFiring(Base):
    __tablename__ = "schedule_firings"
    __table_args__ = (Index("ix_firings_schedule_for", "schedule_id", "scheduled_for"),)
    id: Mapped[uuid.UUID] = _pk()
    schedule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"))
    scheduled_for: Mapped[datetime]
    fired_at: Mapped[datetime] = mapped_column(default=utcnow)
    outcome: Mapped[str] = mapped_column(String(24))  # enqueued | skipped_overlap | skipped_missed | error
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    detail: Mapped[str] = mapped_column(Text, default="")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_ws_id", "workspace_id", "id"),
        Index("ix_audit_ws_action", "workspace_id", "action"),
        Index("ix_audit_ws_actor", "workspace_id", "actor_user_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    actor_label: Mapped[str] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    outcome: Mapped[str] = mapped_column(String(12))  # success | denied | failure
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_ws_created", "workspace_id", "created_at"),
        Index("ix_notifications_pending", "status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    kind: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(16))  # in_app | webhook
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(200))
    pid: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
