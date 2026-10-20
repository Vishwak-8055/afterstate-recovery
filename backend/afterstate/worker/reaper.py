"""Recovery of interrupted runs and stuck cleanups.

A run whose lease expired belongs to a worker that crashed, was killed, or lost the database. The
reaper takes ownership (any late write from the old worker is fenced out), marks unfinished stages,
removes leaked temporary resources using the cleanup tasks recorded before they were created, and
settles the run. Retryable failures are then re-queued by the normal post-run policy."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select

from afterstate.db.base import utcnow
from afterstate.db.models import RecoveryRun, RunEvent, RunStage, ValidationResult
from afterstate.domain.checks import CheckResult
from afterstate.domain.enums import FailureClass, Outcome, RunCleanupState, RunState, StageName, StageState
from afterstate.domain.rules import rule_category
from afterstate.domain.runconfig import RunConfig
from afterstate.domain.state_machine import ACTIVE_STATES, assert_transition
from afterstate.engine.pipeline import _STAGE_ORDER, Services
from afterstate.engine.report import persist_report
from afterstate.worker.postrun import post_run

log = logging.getLogger("afterstate.reaper")


def recover_interrupted_runs(svc: Services) -> list[uuid.UUID]:
    now = utcnow()
    recovered: list[uuid.UUID] = []
    with svc.sf() as s, s.begin():
        runs = list(
            s.scalars(
                select(RecoveryRun)
                .where(RecoveryRun.state.in_(ACTIVE_STATES), RecoveryRun.lease_expires_at < now)
                .with_for_update(skip_locked=True)
            )
        )
        for run in runs:
            previous = run.state
            assert_transition(previous, RunState.NEEDS_CLEANUP)
            owner = run.lease_owner
            run.state, run.state_entered_at = RunState.NEEDS_CLEANUP, now
            run.settled_state = RunState.FAILED
            run.failure_class = FailureClass.WORKER_INTERRUPTED.value
            run.failure_message = f"The worker executing this run ({owner}) stopped responding during {previous.value}; its lease expired."
            run.summary = "The run was interrupted by a worker failure, so no verdict about the backup could be reached. Temporary resources are being cleaned up."
            run.cleanup_state = RunCleanupState.PENDING
            run.lease_owner, run.lease_expires_at, run.heartbeat_at = None, None, now
            for stage in s.scalars(
                select(RunStage).where(RunStage.run_id == run.id, RunStage.state == StageState.RUNNING)
            ):
                stage.state, stage.finished_at, stage.error = StageState.FAILED, now, "worker interrupted"
            s.add(
                RunEvent(
                    run_id=run.id,
                    ts=now,
                    level="ERROR",
                    kind="system",
                    stage=None,
                    message=f"lease held by {owner} expired during {previous.value}; run recovered by the reaper",
                )
            )
            recovered.append(run.id)
    for run_id in recovered:
        _record_unexecuted(svc, run_id)
        settle_cleanup(svc, run_id)
    return recovered


def _record_unexecuted(svc: Services, run_id: uuid.UUID) -> None:
    """Make the interrupted run's record complete: stages and rules that never ran are SKIPPED, not absent."""
    with svc.sf() as s, s.begin():
        run = s.get(RecoveryRun, run_id)
        assert run is not None
        cfg = RunConfig.model_validate(run.config)
        present_stages = {st.name for st in s.scalars(select(RunStage).where(RunStage.run_id == run_id))}
        for name in [*_STAGE_ORDER, StageName.CLEANUP]:
            if name not in present_stages and name is not StageName.CLEANUP:
                s.add(
                    RunStage(
                        run_id=run_id,
                        name=name,
                        state=StageState.SKIPPED,
                        details={"reason": "not executed: worker interrupted"},
                    )
                )
        results = list(s.scalars(select(ValidationResult).where(ValidationResult.run_id == run_id)))
        seen_rules = {str(r.rule_id) for r in results if r.rule_id}
        position = max((r.position for r in results), default=-1) + 1
        for rule in cfg.rules:
            if rule.id not in seen_rules:
                c = CheckResult(
                    f"{rule_category(rule.kind)}.{rule.kind}",
                    rule.name,
                    rule_category(rule.kind),
                    Outcome.SKIPPED,
                    rule.required,
                    "Not executed: worker interrupted.",
                    rule_id=rule.id,
                )
                s.add(
                    ValidationResult(
                        run_id=run_id,
                        rule_id=uuid.UUID(rule.id),
                        position=position,
                        key=c.key,
                        category=c.category,
                        name=c.name[:300],
                        outcome=c.outcome,
                        required=c.required,
                        message=c.message,
                    )
                )
                position += 1


def settle_cleanup(svc: Services, run_id: uuid.UUID) -> bool:
    """Run pending cleanup tasks for a NEEDS_CLEANUP run; settle the run if everything is confirmed gone."""
    outcome = svc.cleanup.run_for_run(run_id)
    settled = False
    with svc.sf() as s, s.begin():
        run = s.execute(select(RecoveryRun).where(RecoveryRun.id == run_id).with_for_update()).scalar_one()
        if run.state is not RunState.NEEDS_CLEANUP:
            return outcome.ok
        now = utcnow()
        run.heartbeat_at = now  # last cleanup attempt
        for note in outcome.notes:
            s.add(RunEvent(run_id=run_id, ts=now, level="INFO", kind="system", message=f"cleanup: {note}"))
        for err in outcome.errors:
            s.add(RunEvent(run_id=run_id, ts=now, level="ERROR", kind="system", message=f"cleanup failed: {err}"))
        if outcome.ok:
            target = run.settled_state or RunState.FAILED
            assert_transition(RunState.NEEDS_CLEANUP, target)
            run.state, run.state_entered_at, run.finished_at = target, now, run.finished_at or now
            run.cleanup_state = RunCleanupState.DONE
            if target is run.settled_state and run.summary and "cleanup will be retried" in run.summary:
                run.summary = run.summary.replace(
                    " Temporary resources could not be removed; cleanup will be retried.", ""
                )
            s.add(
                RunEvent(
                    run_id=run_id,
                    ts=now,
                    level="INFO",
                    kind="system",
                    message=f"cleanup confirmed; run settled as {target.value}",
                )
            )
            settled = True
        else:
            run.cleanup_state = RunCleanupState.FAILED
    if settled:
        try:
            persist_report(svc.sf, svc.store, svc.storage, run_id)
        except Exception:
            log.exception("could not regenerate report after cleanup settled")
        post_run(svc, run_id)
    return outcome.ok


def retry_pending_cleanups(svc: Services) -> list[uuid.UUID]:
    cutoff = utcnow() - timedelta(seconds=svc.settings.cleanup_retry_delay_seconds)
    with svc.sf() as s:
        ids = list(
            s.scalars(
                select(RecoveryRun.id).where(
                    RecoveryRun.state == RunState.NEEDS_CLEANUP, RecoveryRun.heartbeat_at < cutoff
                )
            )
        )
    for run_id in ids:
        settle_cleanup(svc, run_id)
    return ids
