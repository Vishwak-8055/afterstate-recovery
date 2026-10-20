from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Email = Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320, strip_whitespace=True)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_In):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    display_name: Name


class LoginIn(_In):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class WorkspaceIn(_In):
    name: Name


class WorkspacePatch(_In):
    name: Name | None = None
    max_concurrent_runs: Annotated[int, Field(ge=1, le=8)] | None = None
    retention_days: Annotated[int, Field(ge=7, le=3650)] | None = None
    webhook_url: Annotated[str, StringConstraints(max_length=500)] | None = None
    clear_webhook: bool = False


class MemberIn(_In):
    email: Email
    role: Literal["owner", "admin", "operator", "viewer"]


class MemberPatch(_In):
    role: Literal["owner", "admin", "operator", "viewer"]


class ArtifactPatch(_In):
    name: Name | None = None
    label: Annotated[str, StringConstraints(max_length=120)] | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None
    backup_created_at: datetime | None = None


class ProjectIn(_In):
    name: Name
    description: Annotated[str, StringConstraints(max_length=2000)] = ""
    artifact_id: uuid.UUID | None = None
    artifact_label: Annotated[str, StringConstraints(max_length=120)] | None = None
    restore_options: dict[str, Any] = Field(default_factory=dict)
    restore_timeout_seconds: Annotated[int, Field(ge=1, le=86_400)] = 1800
    validation_timeout_seconds: Annotated[int, Field(ge=1, le=86_400)] = 600
    rto_seconds: Annotated[int, Field(ge=1)] | None = None
    rpo_seconds: Annotated[int, Field(ge=1)] | None = None
    max_test_age_days: Annotated[int, Field(ge=1, le=3650)] = 30
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 2
    app_suite: str | None = None
    rules: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class RunIn(_In):
    artifact_id: uuid.UUID | None = None


class ScheduleIn(_In):
    name: Name
    project_id: uuid.UUID
    cron_expr: Annotated[str, StringConstraints(min_length=9, max_length=100)]
    timezone: Annotated[str, StringConstraints(max_length=64)] = "UTC"
    enabled: bool = True
    missed_run_policy: Literal["skip", "run_once"] = "skip"
    retry_max_attempts: Annotated[int, Field(ge=0, le=3)] = 1
    retry_backoff_seconds: Annotated[int, Field(ge=30, le=86_400)] = 300


class SchedulePatch(_In):
    name: Name | None = None
    cron_expr: Annotated[str, StringConstraints(min_length=9, max_length=100)] | None = None
    timezone: Annotated[str, StringConstraints(max_length=64)] | None = None
    enabled: bool | None = None
    missed_run_policy: Literal["skip", "run_once"] | None = None
    retry_max_attempts: Annotated[int, Field(ge=0, le=3)] | None = None
    retry_backoff_seconds: Annotated[int, Field(ge=30, le=86_400)] | None = None
