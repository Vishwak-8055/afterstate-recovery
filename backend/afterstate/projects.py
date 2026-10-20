"""Project/rule helpers shared by the API, the lab and tests."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from afterstate.db.models import RecoveryProject, ValidationRule
from afterstate.domain.rules import dump_rule, parse_rule


def replace_rules(session: Session, project: RecoveryProject, rules: list[dict[str, Any]]) -> None:
    """Replace a project's rules. Every rule is validated (including read-only SQL checks) before anything is written."""
    parsed = [parse_rule(r) for r in rules]
    if sum(1 for r in parsed if r.kind == "recovery_point") > 1:
        raise ValueError("a project can define at most one recovery_point probe")
    project.rules.clear()
    session.flush()
    for i, rule in enumerate(parsed):
        project.rules.append(
            ValidationRule(
                id=uuid.uuid4(),
                position=i,
                kind=rule.kind,
                name=rule.display_name(),
                required=rule.required,
                config=dump_rule(rule),
            )
        )
