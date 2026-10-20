"""Check results and the rules that turn them into a run verdict."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from afterstate.domain.enums import (
    Coverage,
    FailureClass,
    ObjectiveStatus,
    Outcome,
    RunState,
)


@dataclass
class CheckResult:
    key: str
    name: str
    category: str  # artifact | restore | structure | data | application
    outcome: Outcome
    required: bool = True
    message: str = ""
    expected: Any = None
    actual: Any = None
    coverage: Coverage | None = None
    duration_ms: int | None = None
    rule_id: str | None = None


@dataclass(frozen=True)
class ObjectiveResult:
    name: str  # "RTO" | "RPO"
    status: ObjectiveStatus
    target_seconds: int | None
    measured_seconds: float | None
    detail: str
    basis: str | None = None  # where the measurement came from


@dataclass(frozen=True)
class Verdict:
    state: RunState
    failure_class: FailureClass | None
    summary: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def decide_verdict(
    *,
    cancelled: bool,
    execution_failure: FailureClass | None,
    execution_failure_message: str | None,
    checks: list[CheckResult],
    objectives: list[ObjectiveResult],
) -> Verdict:
    """Single source of truth for 'did recovery succeed?'.

    A run is SUCCEEDED only if nothing required failed, was skipped, or stayed inconclusive, and
    every configured objective was demonstrably met. Anything less is never reported as success.
    """
    if cancelled:
        return Verdict(RunState.CANCELLED, FailureClass.CANCELLED, "Run was cancelled before completion.")

    warnings = [
        f"{c.name}: {c.outcome.value}{' - ' + c.message if c.message else ''}"
        for c in checks
        if not c.required and c.outcome is not Outcome.PASS
    ]

    if execution_failure is not None:
        return Verdict(
            RunState.FAILED,
            execution_failure,
            execution_failure_message or f"Recovery failed: {execution_failure.value}.",
            reasons=[execution_failure_message or execution_failure.value],
            warnings=warnings,
        )

    required = [c for c in checks if c.required]
    failed = [c for c in required if c.outcome is Outcome.FAIL]
    if failed:
        return Verdict(
            RunState.FAILED,
            FailureClass.VALIDATION_FAILED,
            f"{len(failed)} required check(s) failed.",
            reasons=[f"{c.name}: {c.message}" for c in failed],
            warnings=warnings,
        )

    incomplete = [c for c in required if c.outcome in (Outcome.SKIPPED, Outcome.INCONCLUSIVE)]
    if incomplete:
        return Verdict(
            RunState.NEEDS_ATTENTION,
            FailureClass.VALIDATION_INCONCLUSIVE,
            f"{len(incomplete)} required check(s) could not be completed, so recovery is not verified.",
            reasons=[f"{c.name}: {c.outcome.value} - {c.message}" for c in incomplete],
            warnings=warnings,
        )

    missed = [o for o in objectives if o.status is ObjectiveStatus.MISSED]
    if missed:
        return Verdict(
            RunState.NEEDS_ATTENTION,
            FailureClass.OBJECTIVE_MISSED,
            "Verification passed but a recovery objective was missed.",
            reasons=[f"{o.name}: {o.detail}" for o in missed],
            warnings=warnings,
        )
    unknown = [o for o in objectives if o.status is ObjectiveStatus.UNKNOWN]
    if unknown:
        return Verdict(
            RunState.NEEDS_ATTENTION,
            FailureClass.OBJECTIVE_UNKNOWN,
            "Verification passed but a configured objective could not be measured.",
            reasons=[f"{o.name}: {o.detail}" for o in unknown],
            warnings=warnings,
        )

    return Verdict(
        RunState.SUCCEEDED,
        None,
        "All required checks passed and all configured objectives were met.",
        warnings=warnings,
    )


def summarize_outcomes(checks: list[CheckResult]) -> dict[str, int]:
    counts = {o.value: 0 for o in Outcome}
    for c in checks:
        counts[c.outcome.value] += 1
    return counts
