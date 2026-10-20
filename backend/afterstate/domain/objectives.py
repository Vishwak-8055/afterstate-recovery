"""Recovery objective evaluation (RTO / RPO).

RTO is measured: elapsed monotonic time from the start of artifact verification to the end of
validation (everything required to reach a verified, usable database; cleanup and report
generation are excluded).

RPO is never fabricated. It is evaluated from, in order of reliability:
  1. a recovery-point probe: the newest application timestamp found in the restored data;
  2. the declared backup creation time, which only proves a *minimum* data-loss window.
Without either, the objective is UNKNOWN rather than guessed.
"""

from __future__ import annotations

from datetime import datetime

from afterstate.domain.checks import ObjectiveResult
from afterstate.domain.enums import ObjectiveStatus


def evaluate_rto(target_seconds: int | None, measured_seconds: float | None) -> ObjectiveResult:
    if target_seconds is None:
        return ObjectiveResult(
            "RTO", ObjectiveStatus.NOT_CONFIGURED, None, measured_seconds, "No RTO target configured."
        )
    if measured_seconds is None:
        return ObjectiveResult(
            "RTO", ObjectiveStatus.UNKNOWN, target_seconds, None, "Recovery duration was not measured."
        )
    met = measured_seconds <= target_seconds
    return ObjectiveResult(
        "RTO",
        ObjectiveStatus.MET if met else ObjectiveStatus.MISSED,
        target_seconds,
        round(measured_seconds, 3),
        f"Recovered and verified in {measured_seconds:.1f}s against a {target_seconds}s target.",
        basis="monotonic clock",
    )


def evaluate_rpo(
    target_seconds: int | None,
    *,
    test_started_at: datetime,
    latest_recoverable_at: datetime | None,
    backup_created_at: datetime | None,
) -> ObjectiveResult:
    if target_seconds is None:
        return ObjectiveResult("RPO", ObjectiveStatus.NOT_CONFIGURED, None, None, "No RPO target configured.")

    if latest_recoverable_at is not None:
        exposure = max(0.0, (test_started_at - latest_recoverable_at).total_seconds())
        met = exposure <= target_seconds
        return ObjectiveResult(
            "RPO",
            ObjectiveStatus.MET if met else ObjectiveStatus.MISSED,
            target_seconds,
            round(exposure, 1),
            f"Newest recovered record is {exposure:.0f}s older than the test start (target {target_seconds}s).",
            basis="recovery-point probe query on restored data",
        )

    if backup_created_at is not None:
        age = max(0.0, (test_started_at - backup_created_at).total_seconds())
        if age > target_seconds:
            return ObjectiveResult(
                "RPO",
                ObjectiveStatus.MISSED,
                target_seconds,
                round(age, 1),
                f"The backup is {age:.0f}s old, so at least that much data would be lost; target is {target_seconds}s.",
                basis="declared backup creation time (lower bound)",
            )
        return ObjectiveResult(
            "RPO",
            ObjectiveStatus.UNKNOWN,
            target_seconds,
            round(age, 1),
            "The backup age is within target, but without a recovery-point probe or WAL evidence "
            "the actual data-loss window cannot be proven.",
            basis="declared backup creation time (lower bound)",
        )

    return ObjectiveResult(
        "RPO",
        ObjectiveStatus.UNKNOWN,
        target_seconds,
        None,
        "No recovery-point probe and no backup creation time are available, so RPO cannot be measured.",
    )
