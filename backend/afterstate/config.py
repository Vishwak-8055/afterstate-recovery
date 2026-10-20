"""Runtime configuration. Every setting is an environment variable prefixed AFTERSTATE_."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DB = "postgresql+psycopg://afterstate:afterstate@localhost:5432/afterstate"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AFTERSTATE_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = _DEFAULT_DB

    # Storage and scratch space. scratch_dir must be short: PostgreSQL unix socket paths are
    # limited to ~107 bytes and every restore environment lives under it.
    artifact_dir: Path = Path("./var/artifacts")
    scratch_dir: Path = Path("/tmp/afterstate-scratch")  # noqa: S108 - deliberate short default; production sets a dedicated volume
    max_artifact_bytes: int = Field(default=2 * 1024**3, ge=1024)
    max_json_body_bytes: int = Field(default=1_048_576, ge=1024)

    # Sessions and web security
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60)
    session_cookie_secure: bool = False
    allow_registration: bool = True
    allowed_origins: list[str] = Field(default_factory=list)
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)
    rate_limit_api_per_minute: int = Field(default=600, ge=10)

    # PostgreSQL tooling used by the recovery engine
    pg_bin_dir: Path | None = None

    # Recovery engine limits
    max_environment_bytes: int = Field(default=8 * 1024**3, ge=1024**2)
    default_restore_timeout_seconds: int = Field(default=1800, ge=1)
    default_validation_timeout_seconds: int = Field(default=600, ge=1)
    environment_shared_buffers_mb: int = Field(default=128, ge=16)
    max_log_events_per_run: int = Field(default=3000, ge=100)

    # Worker
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=60, ge=3)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0)
    scheduler_tick_seconds: float = Field(default=5.0, gt=0)
    cleanup_retry_delay_seconds: int = Field(default=30, ge=1)
    max_concurrent_runs_default: int = Field(default=2, ge=1)

    # Lab and fault injection. Faults are only ever honoured for runs created by the Recovery Lab.
    lab_enabled: bool = True
    fault_injection_enabled: bool = True

    # Notifications
    webhook_allow_private_hosts: bool = False

    log_level: str = "INFO"
    log_json: bool = True

    @model_validator(mode="after")
    def _production_must_fail_closed(self) -> Settings:
        if self.environment == "production":
            problems = []
            if not self.session_cookie_secure:
                problems.append("AFTERSTATE_SESSION_COOKIE_SECURE must be true in production")
            if self.database_url == _DEFAULT_DB:
                problems.append("AFTERSTATE_DATABASE_URL must not use the default credentials")
            if self.webhook_allow_private_hosts:
                problems.append("AFTERSTATE_WEBHOOK_ALLOW_PRIVATE_HOSTS must be false in production")
            if problems:
                raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
