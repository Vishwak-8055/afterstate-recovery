"""Artifact registration: verify an uploaded backup and record it with its evidence."""

from __future__ import annotations

import uuid
from datetime import datetime

from afterstate.db.base import utcnow
from afterstate.db.models import ArtifactVerification, BackupArtifact
from afterstate.domain.enums import ArtifactStatus, Outcome
from afterstate.engine.inspect import ArtifactInspection, inspect_artifact
from afterstate.engine.pipeline import Services
from afterstate.storage.base import StoredObject


class ChecksumMismatch(Exception):  # noqa: N818
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__("the uploaded file does not match the expected SHA-256")
        self.expected, self.actual = expected, actual


def verify_and_record(
    svc: Services, artifact_id: uuid.UUID, *, kind: str, triggered_by: uuid.UUID | None
) -> tuple[BackupArtifact, ArtifactVerification, ArtifactInspection]:
    """Inspect the stored file (checksum vs recorded, format, TOC, version, deep scan) and persist the result."""
    started = utcnow()
    with svc.sf() as s:
        art = s.get(BackupArtifact, artifact_id)
        assert art is not None
        key, recorded = art.storage_key, art.sha256
    insp = inspect_artifact(
        svc.storage.resolve(key), svc.tools, recorded_sha256=recorded, max_bytes=svc.settings.max_artifact_bytes
    )
    with svc.sf() as s, s.begin():
        art = s.get(BackupArtifact, artifact_id)
        assert art is not None
        ver = ArtifactVerification(
            artifact_id=artifact_id,
            kind=kind,
            outcome=Outcome.PASS if insp.ok else Outcome.FAIL,
            started_at=started,
            finished_at=utcnow(),
            duration_ms=int(insp.duration_s * 1000),
            sha256=insp.sha256,
            size_bytes=insp.size_bytes,
            format=insp.format.value,
            failure_class=insp.failure_class.value if insp.failure_class else None,
            error=insp.failure_message,
            details={
                **insp.as_details(),
                "checks": [
                    {"key": c.key, "name": c.name, "outcome": c.outcome.value, "message": c.message}
                    for c in insp.checks
                ],
            },
            triggered_by=triggered_by,
        )
        s.add(ver)
        art.last_verified_at = utcnow()
        art.status = ArtifactStatus.VERIFIED if insp.ok else ArtifactStatus.REJECTED
        art.status_detail = "All verification checks passed." if insp.ok else (insp.failure_message or "")[:500]
        if insp.format.value != "unknown":
            art.format = insp.format
        if insp.ok:
            art.dump_server_version, art.dump_tool_version = insp.server_version, insp.tool_version
            art.source_database, art.archive_created_at = insp.source_database, insp.archive_created_at
            art.toc_entries, art.compression = insp.toc_entries, insp.compression
        s.flush()
        s.refresh(art)
        s.expunge_all()
        return art, ver, insp


def register_stored_artifact(
    svc: Services,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    stored: StoredObject,
    name: str,
    label: str,
    original_filename: str,
    expected_sha256: str | None = None,
    backup_created_at: datetime | None = None,
    notes: str = "",
    lab_scenario: str | None = None,
) -> tuple[BackupArtifact, ArtifactVerification, ArtifactInspection]:
    if expected_sha256 and expected_sha256.lower() != stored.sha256:
        raise ChecksumMismatch(expected_sha256.lower(), stored.sha256)
    with svc.sf() as s, s.begin():
        art = BackupArtifact(
            workspace_id=workspace_id,
            label=label,
            name=name,
            original_filename=original_filename[:255],
            storage_key=stored.key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            expected_sha256=expected_sha256.lower() if expected_sha256 else None,
            backup_created_at=backup_created_at,
            notes=notes,
            created_by=user_id,
            lab_scenario=lab_scenario,
        )
        s.add(art)
        s.flush()
        artifact_id = art.id
    return verify_and_record(svc, artifact_id, kind="upload", triggered_by=user_id)
