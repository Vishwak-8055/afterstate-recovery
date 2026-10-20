"""Recovery reports. The document is assembled strictly from persisted facts about the run
(stages, measured metrics, check results, cleanup tasks, evidence checksums); nothing is estimated."""

from __future__ import annotations

import json
import platform
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from afterstate import __version__
from afterstate.db.models import (
    ArtifactVerification,
    BackupArtifact,
    CleanupTask,
    RecoveryRun,
    RunEvent,
    RunEvidence,
    RunStage,
    ValidationResult,
)
from afterstate.domain.enums import Outcome, RunState, StageName
from afterstate.security.redact import redact_value

SCHEMA = "afterstate.report/v1"
_HEADLINES = {
    RunState.SUCCEEDED: "Recovery verified",
    RunState.FAILED: "Recovery failed",
    RunState.NEEDS_ATTENTION: "Recovery not verified: needs attention",
    RunState.CANCELLED: "Run cancelled",
    RunState.NEEDS_CLEANUP: "Result recorded; cleanup incomplete",
}


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def build_report(
    sf: sessionmaker[Session], run_id: uuid.UUID, *, state_override: RunState | None = None
) -> dict[str, Any]:
    with sf() as s:
        run = s.get(RecoveryRun, run_id)
        if run is None:
            raise LookupError(run_id)
        artifact = s.get(BackupArtifact, run.artifact_id)
        stages = [
            x
            for x in s.scalars(select(RunStage).where(RunStage.run_id == run_id).order_by(RunStage.id))
            if x.name is not StageName.REPORT
        ]
        results = list(
            s.scalars(
                select(ValidationResult).where(ValidationResult.run_id == run_id).order_by(ValidationResult.position)
            )
        )
        tasks = list(
            s.scalars(select(CleanupTask).where(CleanupTask.run_id == run_id).order_by(CleanupTask.created_at))
        )
        evidence = list(s.scalars(select(RunEvidence).where(RunEvidence.run_id == run_id).order_by(RunEvidence.kind)))
        events = list(
            s.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.kind.in_(("state", "fault", "system")))
                .order_by(RunEvent.id)
                .limit(200)
            )
        )
        problems = list(
            s.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.level.in_(("WARN", "ERROR")), RunEvent.kind == "log")
                .order_by(RunEvent.id)
                .limit(20)
            )
        )
        verification = s.scalars(
            select(ArtifactVerification)
            .where(ArtifactVerification.run_id == run_id)
            .order_by(ArtifactVerification.started_at.desc())
        ).first()

    state = state_override or run.state
    cfg = run.config or {}
    counts = {o.value: 0 for o in Outcome}
    for r in results:
        counts[r.outcome.value] += 1
    required_open = [r for r in results if r.required and r.outcome is not Outcome.PASS]
    user_rules = [
        r
        for r in results
        if r.category in ("structure", "data", "application") and not r.key.startswith(("structure.baseline", "db."))
    ]
    partial = sum(1 for r in results if r.coverage == "partial" and r.outcome is Outcome.PASS)
    complete = sum(
        1 for r in results if r.coverage == "complete" and r.category == "data" and r.outcome is Outcome.PASS
    )

    limitations: list[str] = []
    if not any(
        r.key not in ("db.connectivity", "db.non_empty") and r.category in ("structure", "data", "application")
        for r in results
    ):
        limitations.append(
            "No project-specific validation rules were configured. Only baseline checks ran (restore completed, database reachable, at least one user table), so correctness of the restored data is not demonstrated."
        )
    if partial and not complete:
        limitations.append(
            "Data checks in this run are partial (counts or invariants). No complete dataset comparison was configured."
        )
    rpo = (run.objectives or {}).get("RPO", {})
    if rpo.get("status") == "UNKNOWN":
        limitations.append("RPO could not be measured: " + str(rpo.get("detail", "")))
    limitations.append(
        "The restore environment uses the C.UTF-8 locale; behaviour that depends on collation order may differ from production."
    )
    limitations.append(
        "This report records what was measured in this run. It does not guarantee that a future recovery will succeed."
    )

    return {
        "schema": SCHEMA,
        "generated_at": _iso(datetime.now(UTC)),
        "headline": _HEADLINES.get(state, state.value),
        "run": {
            "id": str(run.id),
            "project": {"id": cfg.get("project_id"), "name": cfg.get("project_name")},
            "state": state.value,
            "settled_state": run.settled_state.value if run.settled_state else None,
            "summary": run.summary,
            "failure_class": run.failure_class,
            "failure_message": run.failure_message,
            "trigger": run.trigger.value,
            "attempt": run.attempt,
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            "queued_at": _iso(run.queued_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "lab_scenario": run.lab_scenario,
        },
        "artifact": {
            "id": str(run.artifact_id),
            "name": artifact.name if artifact else None,
            "sha256": artifact.sha256 if artifact else None,
            "size_bytes": artifact.size_bytes if artifact else None,
            "format": artifact.format.value if artifact else None,
            "source_database": artifact.source_database if artifact else None,
            "dump_server_version": artifact.dump_server_version if artifact else None,
            "dump_tool_version": artifact.dump_tool_version if artifact else None,
            "backup_created_at": _iso(artifact.backup_created_at) if artifact else None,
            "archive_created_at": _iso(artifact.archive_created_at) if artifact else None,
            "verified_in_run": verification.outcome.value if verification else None,
        },
        "software": {
            "afterstate": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            **{
                k: v
                for k, v in (run.environment or {}).items()
                if k in ("postgresql", "pg_restore", "kind", "network", "restore_role", "validator_role")
            },
        },
        "stages": [
            {
                "name": st.name.value,
                "state": st.state.value,
                "started_at": _iso(st.started_at),
                "finished_at": _iso(st.finished_at),
                "duration_ms": st.duration_ms,
                "error": st.error,
                "details": st.details,
            }
            for st in stages
        ],
        "metrics": run.metrics or {},
        "objectives": run.objectives or {},
        "checks": [
            {
                "position": r.position,
                "key": r.key,
                "name": r.name,
                "category": r.category,
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
        "summary": {
            "counts": counts,
            "required_not_passing": len(required_open),
            "project_rules_evaluated": len(user_rules),
            "complete_dataset_comparisons_passed": complete,
            "partial_checks_passed": partial,
        },
        "warnings": [p.message for p in problems],
        "timeline": [{"at": _iso(e.ts), "kind": e.kind, "level": e.level, "message": e.message} for e in events],
        "cleanup": {
            "state": run.cleanup_state.value,
            "tasks": [
                {
                    "resource_type": t.resource_type,
                    "resource": Path(t.resource_ref).name,
                    "state": t.state.value,
                    "attempts": t.attempts,
                    "error": t.last_error,
                }
                for t in tasks
            ],
        },
        "evidence": [
            {"kind": e.kind, "sha256": e.sha256, "size_bytes": e.size_bytes, "content_type": e.content_type}
            for e in evidence
            if not e.kind.startswith("report")
        ],
        "limitations": limitations,
    }


def to_json_bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(redact_value(doc), indent=2, sort_keys=False, default=str, ensure_ascii=False).encode("utf-8")


_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _compact(value: Any, limit: int = 220) -> str:
    if value is None:
        return "-"
    text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(", ", ": "))
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _duration(ms: Any) -> str:
    if ms is None:
        return "-"
    ms = int(ms)
    return (
        f"{ms} ms"
        if ms < 1000
        else f"{ms / 1000:.2f} s"
        if ms < 60_000
        else f"{ms // 60_000} min {(ms % 60_000) / 1000:.0f} s"
    )


def _bytes(n: Any) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


_env.filters.update(compact=_compact, duration=_duration, human_bytes=_bytes)


def render_html(doc: dict[str, Any]) -> str:
    return _env.get_template("report.html").render(r=doc, version=__version__)


def persist_report(
    sf: sessionmaker[Session], store: Any, storage: Any, run_id: uuid.UUID, *, state_override: RunState | None = None
) -> None:
    """Build the report from persisted facts and store it as checksummed evidence (replaces any previous version)."""
    from afterstate.storage.local import new_evidence_key

    doc = build_report(sf, run_id, state_override=state_override)
    jkey, hkey = new_evidence_key(run_id, "report.json"), new_evidence_key(run_id, "report.html")
    js = storage.write_bytes(jkey, to_json_bytes(doc))
    hs = storage.write_bytes(hkey, render_html(doc).encode("utf-8"))
    store.add_evidence(run_id, "report_json", jkey, js.sha256, js.size_bytes, "application/json")
    store.add_evidence(run_id, "report_html", hkey, hs.sha256, hs.size_bytes, "text/html; charset=utf-8")
