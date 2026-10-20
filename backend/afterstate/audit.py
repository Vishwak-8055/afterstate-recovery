"""Audit trail for sensitive actions, run outcomes and administrative operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from afterstate.db.models import AuditEvent
from afterstate.security.redact import redact_value


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID | None
    label: str
    ip: str | None = None
    correlation_id: str | None = None

    @staticmethod
    def system(name: str) -> Actor:
        return Actor(None, f"system:{name}")


def record(
    session: Session,
    actor: Actor,
    action: str,
    *,
    workspace_id: uuid.UUID | None,
    target_type: str = "",
    target_id: str | uuid.UUID | None = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
            actor_label=actor.label[:320],
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            outcome=outcome,
            ip=actor.ip,
            correlation_id=actor.correlation_id,
            details=redact_value(details or {}),
        )
    )
