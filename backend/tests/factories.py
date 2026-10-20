from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from afterstate.db.models import BackupArtifact, Membership, RecoveryProject, User, Workspace
from afterstate.domain.enums import ArtifactFormat, ArtifactStatus, Role
from afterstate.engine.inspect import sha256_file, sniff_format
from afterstate.engine.pipeline import Services
from afterstate.projects import replace_rules
from afterstate.storage.local import new_artifact_key


def make_workspace(svc: Services, *, name: str = "Acme Platform", role: Role = Role.OWNER) -> tuple[Workspace, User]:
    with svc.sf() as s, s.begin():
        user = User(email=f"{uuid.uuid4().hex[:8]}@example.test", display_name="Test User", password_hash="x")
        ws = Workspace(name=name, slug=f"ws-{uuid.uuid4().hex[:8]}", max_concurrent_runs=2)
        s.add_all([user, ws])
        s.flush()
        s.add(Membership(workspace_id=ws.id, user_id=user.id, role=role))
    return ws, user


def make_artifact(
    svc: Services,
    ws: Workspace,
    source: Path,
    *,
    label: str = "shop",
    name: str | None = None,
    backup_created_at: datetime | None = None,
    lab_scenario: str | None = None,
) -> BackupArtifact:
    key = new_artifact_key(ws.id)
    target = svc.storage.resolve(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    fmt = sniff_format(target)
    with svc.sf() as s, s.begin():
        art = BackupArtifact(
            workspace_id=ws.id,
            label=label,
            name=name or source.name,
            original_filename=source.name,
            storage_key=key,
            size_bytes=target.stat().st_size,
            sha256=sha256_file(target),
            format=fmt
            if fmt in (ArtifactFormat.CUSTOM, ArtifactFormat.TAR, ArtifactFormat.PLAIN_SQL)
            else ArtifactFormat.UNKNOWN,
            status=ArtifactStatus.UNVERIFIED,
            backup_created_at=backup_created_at,
            lab_scenario=lab_scenario,
        )
        s.add(art)
        s.flush()
        s.expunge(art)
    return art


def make_project(
    svc: Services,
    ws: Workspace,
    artifact: BackupArtifact | None,
    *,
    name: str = "shop-recovery",
    rules: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> RecoveryProject:
    defaults: dict[str, Any] = {"restore_timeout_seconds": 120, "validation_timeout_seconds": 120, "max_attempts": 1}
    defaults.update(fields)
    with svc.sf() as s, s.begin():
        project = RecoveryProject(
            workspace_id=ws.id, name=name, artifact_id=artifact.id if artifact else None, **defaults
        )
        s.add(project)
        s.flush()
        replace_rules(s, project, rules or [])
        s.flush()
        s.refresh(project)
        _ = list(project.rules)
        s.expunge_all()
    return project
