"""Immutable snapshot of everything a run needs. Taken at submission so that editing a project
later never changes what an existing run did, and so a run can be reproduced from its record."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from afterstate.domain.rules import Objectives, RestoreOptions


class RuleSnapshot(BaseModel):
    id: str
    position: int
    name: str
    required: bool
    kind: str
    config: dict[str, Any]


class ArtifactRef(BaseModel):
    id: str
    name: str
    storage_key: str
    sha256: str
    size_bytes: int
    format: str
    backup_created_at: datetime | None = None


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    project_name: str
    artifact: ArtifactRef
    restore: RestoreOptions = Field(default_factory=RestoreOptions)
    restore_timeout_seconds: int = 1800
    validation_timeout_seconds: int = 600
    objectives: Objectives = Field(default_factory=Objectives)
    rules: list[RuleSnapshot] = Field(default_factory=list)
    app_suite: str | None = None
    max_attempts: int = 2
    max_environment_bytes: int = 8 * 1024**3
    retry_backoff_seconds: int = 300
