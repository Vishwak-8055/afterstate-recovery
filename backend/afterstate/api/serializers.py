from __future__ import annotations

from datetime import datetime
from typing import Any

from afterstate.db.models import BackupArtifact, RecoveryProject, RecoveryRun, Schedule


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def artifact(a: BackupArtifact) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "label": a.label,
        "original_filename": a.original_filename,
        "size_bytes": a.size_bytes,
        "sha256": a.sha256,
        "expected_sha256": a.expected_sha256,
        "format": a.format.value,
        "status": a.status.value,
        "status_detail": a.status_detail,
        "dump_server_version": a.dump_server_version,
        "dump_tool_version": a.dump_tool_version,
        "source_database": a.source_database,
        "archive_created_at": iso(a.archive_created_at),
        "backup_created_at": iso(a.backup_created_at),
        "toc_entries": a.toc_entries,
        "compression": a.compression,
        "notes": a.notes,
        "last_verified_at": iso(a.last_verified_at),
        "created_at": iso(a.created_at),
        "lab_scenario": a.lab_scenario,
        "deleted": a.deleted_at is not None,
    }


def project(p: RecoveryProject, *, rules: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "artifact_id": str(p.artifact_id) if p.artifact_id else None,
        "artifact_label": p.artifact_label,
        "restore_options": p.restore_options,
        "restore_timeout_seconds": p.restore_timeout_seconds,
        "validation_timeout_seconds": p.validation_timeout_seconds,
        "rto_seconds": p.rto_seconds,
        "rpo_seconds": p.rpo_seconds,
        "max_test_age_days": p.max_test_age_days,
        "max_attempts": p.max_attempts,
        "app_suite": p.app_suite,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
        "lab_scenario": p.lab_scenario,
    }
    if rules:
        out["rules"] = [
            {
                "id": str(r.id),
                "position": r.position,
                "name": r.name,
                "kind": r.kind,
                "required": r.required,
                "config": r.config,
            }
            for r in p.rules
        ]
    return out


def run_summary(r: RecoveryRun) -> dict[str, Any]:
    cfg = r.config or {}
    m = r.metrics or {}
    return {
        "id": str(r.id),
        "project_id": str(r.project_id),
        "project_name": cfg.get("project_name"),
        "artifact_id": str(r.artifact_id),
        "artifact_name": (cfg.get("artifact") or {}).get("name"),
        "trigger": r.trigger.value,
        "attempt": r.attempt,
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        "state": r.state.value,
        "settled_state": r.settled_state.value if r.settled_state else None,
        "failure_class": r.failure_class,
        "failure_message": r.failure_message,
        "summary": r.summary,
        "queued_at": iso(r.queued_at),
        "started_at": iso(r.started_at),
        "finished_at": iso(r.finished_at),
        "cleanup_state": r.cleanup_state.value,
        "lab_scenario": r.lab_scenario,
        "cancel_requested": r.cancel_requested,
        "progress": r.progress,
        "metrics": {
            k: m.get(k)
            for k in (
                "recovery_seconds",
                "restore_seconds",
                "validation_seconds",
                "total_seconds",
                "restore_throughput_mb_s",
                "restored_database_bytes",
                "artifact_size_bytes",
            )
        },
        "objectives": {k: v.get("status") for k, v in (r.objectives or {}).items()},
    }


def schedule(s: Schedule) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "project_id": str(s.project_id),
        "cron_expr": s.cron_expr,
        "timezone": s.timezone,
        "enabled": s.enabled,
        "missed_run_policy": s.missed_run_policy,
        "retry_max_attempts": s.retry_max_attempts,
        "retry_backoff_seconds": s.retry_backoff_seconds,
        "next_run_at": iso(s.next_run_at),
        "last_fired_at": iso(s.last_fired_at),
        "created_at": iso(s.created_at),
    }
