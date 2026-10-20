"""The recovery pipeline: executes one recovery test end to end and records everything it measures.

    PREPARING   artifact verification -> environment provisioning
    RESTORING   unprivileged pg_restore into a disposable, network-less cluster
    VALIDATING  structural -> data -> application checks, independent of the restore's exit status
    FINALIZING  objectives, verdict, verified cleanup, evidence and report, then the settled state

Every failure path (including cancellation) reaches FINALIZING so evidence is written and resources
are cleaned up before the verdict becomes visible.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from afterstate.config import Settings
from afterstate.db.base import utcnow
from afterstate.db.models import ArtifactVerification, BackupArtifact, RecoveryRun
from afterstate.domain.checks import CheckResult, ObjectiveResult, decide_verdict, summarize_outcomes
from afterstate.domain.enums import (
    APPLICATION_KINDS,
    DATA_KINDS,
    STRUCTURAL_KINDS,
    ArtifactFormat,
    ArtifactStatus,
    FailureClass,
    ObjectiveStatus,
    Outcome,
    RunCleanupState,
    RunState,
    StageName,
    StageState,
)
from afterstate.domain.objectives import evaluate_rpo, evaluate_rto
from afterstate.domain.rules import HttpCheckRule, parse_rule
from afterstate.domain.runconfig import RuleSnapshot, RunConfig
from afterstate.engine.appsuite import DEFAULT_SUITE_CHECKS, AppRunner, AppStartError
from afterstate.engine.cleanup import CleanupOutcome, CleanupService
from afterstate.engine.environment import EnvironmentError_, LocalClusterEnvironment
from afterstate.engine.inspect import ArtifactInspection, inspect_artifact
from afterstate.engine.pgtools import PgTools
from afterstate.engine.report import persist_report
from afterstate.engine.restore import run_restore
from afterstate.engine.store import EventSink, LeaseLost, RunStore
from afterstate.engine.validators import EnvironmentLost, Validator, evaluate_http
from afterstate.logging import bind
from afterstate.security.redact import Redactor
from afterstate.storage.base import ArtifactStorage
from afterstate.storage.local import new_evidence_key

log = logging.getLogger("afterstate.pipeline")


@dataclass
class Services:
    settings: Settings
    sf: sessionmaker[Session]
    store: RunStore
    storage: ArtifactStorage
    tools: PgTools
    cleanup: CleanupService


class StageFailure(Exception):  # noqa: N818
    def __init__(self, klass: FailureClass, message: str) -> None:
        super().__init__(message)
        self.klass, self.message = klass, message


class Cancelled(Exception):  # noqa: N818
    pass


_STAGE_ORDER = [
    StageName.ARTIFACT_VERIFICATION,
    StageName.ENVIRONMENT,
    StageName.RESTORE,
    StageName.STRUCTURAL,
    StageName.DATA,
    StageName.APPLICATION,
]


class RunPipeline:
    def __init__(
        self, svc: Services, run_id: uuid.UUID, owner: str, cancel: threading.Event, lease_lost: threading.Event
    ) -> None:
        self.svc, self.run_id, self.owner, self.cancel, self.lease_lost = svc, run_id, owner, cancel, lease_lost

    # =========================================================================================
    def execute(self) -> RunState:
        svc = self.svc
        with svc.sf() as s:
            run = s.get(RecoveryRun, self.run_id)
            assert run is not None
            self.cfg = RunConfig.model_validate(run.config)
            self.test_started_at = run.started_at or utcnow()
            self.workspace_id = run.workspace_id
            self.fault: dict[str, Any] = (
                dict(run.fault or {}) if (svc.settings.fault_injection_enabled and run.lab_scenario) else {}
            )
            self.attempt = run.attempt

        self.redactor = Redactor()
        self.sink = EventSink(
            svc.store, self.run_id, self.owner, cap=svc.settings.max_log_events_per_run, redactor=self.redactor
        )
        self.timings: dict[StageName, float] = {}
        self.checks: list[CheckResult] = []
        self._persisted = 0
        self.failure: tuple[FailureClass, str] | None = None
        self.cancelled = False
        self.env: LocalClusterEnvironment | None = None
        self.validator: Validator | None = None
        self.validation_seconds = 0.0
        self.metrics: dict[str, Any] = {"artifact_size_bytes": self.cfg.artifact.size_bytes}
        self.recorded: set[StageName] = set()
        self.evaluated: set[str] = set()
        self.restore_skipped: list[str] = []
        self.t0 = time.perf_counter()

        with bind(run_id=str(self.run_id), workspace_id=str(self.workspace_id)):
            self.sink.emit("INFO", f"run picked up by {self.owner} (attempt {self.attempt})", kind="system")
            try:
                self._artifact_stage()
                self._environment_stage()
                self._restore_stage()
                self._validation_stages()
            except Cancelled:
                self.cancelled = True
                self.sink.emit("WARN", "cancellation requested: stopping and cleaning up", kind="system")
            except StageFailure as exc:
                self.failure = (exc.klass, exc.message)
            except LeaseLost:
                self._abandon()
                raise
            except Exception as exc:
                log.exception("unexpected pipeline error")
                self.failure = (
                    FailureClass.INTERNAL_ERROR,
                    f"Unexpected error: {type(exc).__name__}: {self.redactor.apply(str(exc))}"[:400],
                )
            return self._finalize()

    # ---- plumbing --------------------------------------------------------------------------
    def _abort_check(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLost("lease lost")
        if self.cancel.is_set():
            raise Cancelled

    def _abandon(self) -> None:
        """Lease lost: another actor owns the run record now. Free only what this process created."""
        try:
            if self.validator:
                self.validator.close()
            if self.env:
                self.env.kill_postmaster()
        except Exception:
            log.exception("error while abandoning run")

    @contextmanager
    def _stage(self, name: StageName) -> Iterator[dict[str, Any]]:
        self._abort_check()
        store = self.svc.store
        self.sink.stage = name.value
        stage_id = store.begin_stage(self.run_id, self.owner, name)
        self.recorded.add(name)
        label = name.value.replace("_", " ")
        self.sink.emit("INFO", f"{label} started", kind="stage")
        info: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            yield info
        except LeaseLost:
            raise
        except BaseException as exc:
            dur = time.perf_counter() - t0
            self.timings[name] = dur
            cancelled = isinstance(exc, Cancelled)
            msg = "cancelled" if cancelled else self.redactor.apply(str(exc))
            self.sink.emit(
                "WARN" if cancelled else "ERROR",
                f"{label} {'cancelled' if cancelled else 'failed'}: {msg}",
                kind="stage",
            )
            self.sink.flush()
            store.end_stage(
                self.run_id, self.owner, stage_id, StageState.FAILED, int(dur * 1000), details=info, error=msg[:500]
            )
            raise
        dur = time.perf_counter() - t0
        self.timings[name] = dur
        self.sink.emit("INFO", f"{label} finished in {dur:.2f}s", kind="stage")
        self.sink.flush()
        store.end_stage(self.run_id, self.owner, stage_id, StageState.SUCCEEDED, int(dur * 1000), details=info)

    def _add_checks(self, checks: list[CheckResult]) -> None:
        for c in checks:
            self.checks.append(c)
            level = "INFO" if c.outcome is Outcome.PASS else "WARN"
            self.sink.emit(
                level,
                f"check {c.outcome.value}: {c.name}",
                kind="log",
                data={"check": c.key, "outcome": c.outcome.value},
            )
        self.svc.store.add_results(self.run_id, self.owner, checks, self._persisted)
        self._persisted += len(checks)

    # ---- stage 1: artifact ----------------------------------------------------------------
    def _artifact_stage(self) -> None:
        svc, cfg = self.svc, self.cfg
        with self._stage(StageName.ARTIFACT_VERIFICATION) as info:
            path = svc.storage.resolve(cfg.artifact.storage_key)
            insp = inspect_artifact(
                path,
                svc.tools,
                recorded_sha256=cfg.artifact.sha256,
                max_bytes=svc.settings.max_artifact_bytes,
                cancel=self.cancel,
            )
            if self.cancel.is_set():
                raise Cancelled
            self._add_checks(insp.checks)
            self._record_artifact_verification(insp)
            info.update(insp.as_details())
            if insp.size_bytes:
                self.metrics["artifact_size_bytes"] = insp.size_bytes
            if not insp.ok:
                assert insp.failure_class is not None
                raise StageFailure(insp.failure_class, insp.failure_message or insp.failure_class.value)

    def _record_artifact_verification(self, insp: ArtifactInspection) -> None:
        now = utcnow()
        with self.svc.sf() as s, s.begin():
            art = s.get(BackupArtifact, uuid.UUID(self.cfg.artifact.id))
            s.add(
                ArtifactVerification(
                    artifact_id=uuid.UUID(self.cfg.artifact.id),
                    run_id=self.run_id,
                    kind="run",
                    outcome=Outcome.PASS if insp.ok else Outcome.FAIL,
                    started_at=now,
                    finished_at=now,
                    duration_ms=int(insp.duration_s * 1000),
                    sha256=insp.sha256,
                    size_bytes=insp.size_bytes,
                    format=insp.format.value,
                    failure_class=insp.failure_class.value if insp.failure_class else None,
                    error=insp.failure_message,
                    details=insp.as_details(),
                )
            )
            if art is not None and art.deleted_at is None:
                art.last_verified_at = now
                art.status = ArtifactStatus.VERIFIED if insp.ok else ArtifactStatus.REJECTED
                art.status_detail = "Verified during recovery run." if insp.ok else (insp.failure_message or "")[:500]
                if insp.format is not ArtifactFormat.UNKNOWN:
                    art.format = insp.format

    # ---- stage 2: environment -------------------------------------------------------------
    def _environment_stage(self) -> None:
        svc = self.svc
        with self._stage(StageName.ENVIRONMENT) as info:
            env = LocalClusterEnvironment(
                svc.settings.scratch_dir,
                self.run_id,
                svc.tools,
                shared_buffers_mb=svc.settings.environment_shared_buffers_mb,
                redactor=self.redactor,
            )
            self.env = env
            svc.store.add_cleanup_task(
                self.run_id, self.owner, "environment", str(env.path)
            )  # recorded BEFORE creation
            self.sink.emit("INFO", f"cleanup task recorded for {env.path.name}", kind="system")
            try:
                env.create()
                env.start()
            except (EnvironmentError_, OSError) as exc:
                raise StageFailure(
                    FailureClass.ENVIRONMENT_UNAVAILABLE, f"Could not create the disposable database: {exc}"
                ) from exc
            info.update(env.describe())
            svc.store.set_fields(self.run_id, self.owner, environment={**env.describe(), **svc.tools.versions()})
            self.sink.emit("INFO", "disposable PostgreSQL cluster is accepting connections (unix socket only)")
        self._add_checks(
            [
                CheckResult(
                    "environment.provisioned",
                    "Disposable restore environment provisioned",
                    "restore",
                    Outcome.PASS,
                    True,
                    "Private cluster started with unprivileged restore and read-only validator roles.",
                )
            ]
        )

    # ---- stage 3: restore -----------------------------------------------------------------
    def _restore_stage(self) -> None:
        svc, cfg = self.svc, self.cfg
        assert self.env is not None
        env = self.env
        svc.store.transition(self.run_id, RunState.RESTORING, owner=self.owner)
        with self._stage(StageName.RESTORE) as info:
            stall = self.fault.get("stall_restore_seconds")
            if stall:
                self.sink.emit(
                    "WARN",
                    f"FAULT INJECTED: restore paused for up to {stall}s (Recovery Lab)",
                    kind="fault",
                    data={"fault": "stall_restore_seconds", "seconds": stall},
                )
                self.sink.flush()
                waited = 0.0
                while waited < float(stall):
                    self._abort_check()
                    time.sleep(0.25)
                    waited += 0.25

            last = [0.0]

            def on_progress(done: int, total: int) -> None:
                now = time.monotonic()
                if now - last[0] >= 0.4 or done >= total:
                    last[0] = now
                    try:
                        svc.store.set_fields(
                            self.run_id,
                            self.owner,
                            progress={"stage": "restore", "items_done": done, "items_total": total},
                        )
                    except LeaseLost:
                        self.lease_lost.set()

            def watch() -> str | None:
                return "disk_limit" if env.disk_usage_bytes() > cfg.max_environment_bytes else None

            work = env.path / "work"
            work.mkdir(exist_ok=True, mode=0o700)
            result = run_restore(
                tools=svc.tools,
                artifact_path=svc.storage.resolve(cfg.artifact.storage_key),
                target=env.restore_target(),
                workdir=work,
                jobs=cfg.restore.jobs,
                timeout_s=float(cfg.restore_timeout_seconds),
                cancel=self.cancel,
                redactor=self.redactor,
                emit=self.sink.emit,
                on_progress=on_progress,
                watch=watch,
            )
            self.restore_skipped = result.skipped
            seconds = result.duration_s
            info.update(
                items_done=result.items_done,
                items_total=result.items_total,
                skipped_objects=result.skipped_count,
                restore_seconds=round(seconds, 3),
                jobs=cfg.restore.jobs,
                command=[a if "PASSWORD" not in a else "***" for a in result.argv_display],
            )
            self.metrics.update(
                restore_seconds=round(seconds, 3),
                restore_items_done=result.items_done,
                restore_items_total=result.items_total,
                restore_skipped_objects=result.skipped_count,
            )
            if self.cancel.is_set():
                raise Cancelled
            if not result.ok:
                assert result.failure_class is not None
                self._add_checks(
                    [
                        CheckResult(
                            "restore.completed",
                            "pg_restore completed without errors",
                            "restore",
                            Outcome.FAIL,
                            True,
                            result.failure_message or "",
                            actual={"exit_code": result.command.returncode if result.command else None},
                        )
                    ]
                )
                raise StageFailure(result.failure_class, result.failure_message or "restore failed")
            if cfg.artifact.size_bytes and seconds > 0:
                self.metrics["restore_throughput_mb_s"] = round(cfg.artifact.size_bytes / 1_048_576 / seconds, 2)
            self.metrics["restored_database_bytes"] = env.database_size_bytes()
            self.metrics["environment_disk_bytes"] = env.disk_usage_bytes()
            self._add_checks(
                [
                    CheckResult(
                        "restore.completed",
                        "pg_restore completed without errors",
                        "restore",
                        Outcome.PASS,
                        True,
                        f"{result.items_done} of {result.items_total} archive items restored.",
                        actual={"exit_code": 0, "items": result.items_done},
                    )
                ]
            )

    # ---- stage 4: validation --------------------------------------------------------------
    def _rules_of(self, kinds: frozenset[Any]) -> list[RuleSnapshot]:
        wanted = {k.value for k in kinds}
        return [r for r in self.cfg.rules if r.kind in wanted]

    def _validation_stages(self) -> None:
        svc, cfg = self.svc, self.cfg
        assert self.env is not None
        svc.store.transition(self.run_id, RunState.VALIDATING, owner=self.owner)
        self.validator = Validator(
            self.env, budget_s=float(cfg.validation_timeout_seconds), redactor=self.redactor, cancel=self.cancel
        )
        started = time.perf_counter()
        try:
            if self.fault.get("kill_environment_before_validation"):
                self.sink.emit(
                    "WARN",
                    "FAULT INJECTED: SIGKILL sent to the restored database before validation (Recovery Lab)",
                    kind="fault",
                    data={"fault": "kill_environment_before_validation"},
                )
                self.env.kill_postmaster()
            self._validation_group(StageName.STRUCTURAL, self._rules_of(STRUCTURAL_KINDS), baseline=True)
            self._validation_group(StageName.DATA, self._rules_of(DATA_KINDS))
            self._application_stage(self._rules_of(APPLICATION_KINDS))
        finally:
            self.validation_seconds = time.perf_counter() - started
            self.validator.close()

    def _validation_group(self, stage: StageName, rules: list[RuleSnapshot], *, baseline: bool = False) -> None:
        assert self.validator is not None
        with self._stage(stage) as info:
            lost: EnvironmentLost | None = None
            executed = 0
            try:
                if baseline:
                    self._add_checks(self.validator.baseline_checks())
                    executed += 2
                for rule in rules:
                    self._abort_check()
                    result = self.validator.evaluate(
                        rule.id, rule.position, rule.name, rule.kind, rule.config, rule.required
                    )
                    self.evaluated.add(rule.id)
                    self._add_checks([result])
                    executed += 1
            except EnvironmentLost as exc:
                lost = exc
            info["checks_executed"] = executed
            info["rules_configured"] = len(rules)
            if lost is not None:
                raise StageFailure(FailureClass.ENVIRONMENT_UNAVAILABLE, str(lost))

    def _application_stage(self, rules: list[RuleSnapshot]) -> None:
        assert self.env is not None
        cfg, store = self.cfg, self.svc.store
        suite = cfg.app_suite
        if not rules and not suite:
            store.skip_stage(self.run_id, self.owner, StageName.APPLICATION, "no application checks configured")
            self.recorded.add(StageName.APPLICATION)
            return
        with self._stage(StageName.APPLICATION) as info:

            def inconclusive(r: RuleSnapshot, outcome: Outcome, why: str) -> CheckResult:
                return CheckResult(
                    "application.http_check", r.name, "application", outcome, r.required, why, rule_id=r.id or None
                )

            if not suite:
                for r in rules:
                    self.evaluated.add(r.id)
                    self._add_checks(
                        [
                            inconclusive(
                                r,
                                Outcome.SKIPPED,
                                "No application suite is configured for this project, so no application was started.",
                            )
                        ]
                    )
                info["reason"] = "no application suite configured"
                return
            planned = rules or [
                RuleSnapshot(
                    id="",
                    position=i,
                    name=str(d["name"]),
                    required=True,
                    kind="http_check",
                    config={k: v for k, v in d.items() if k != "name"},
                )
                for i, d in enumerate(DEFAULT_SUITE_CHECKS.get(suite, []))
            ]
            runner = AppRunner(self.env, suite)
            try:
                app = runner.start()
            except AppStartError as exc:
                for r in planned:
                    self.evaluated.add(r.id)
                    self._add_checks(
                        [
                            inconclusive(
                                r, Outcome.INCONCLUSIVE, f"The application suite '{suite}' could not be started: {exc}"
                            )
                        ]
                    )
                info["error"] = str(exc)
                return
            try:
                self.sink.emit(
                    "INFO", f"application suite '{suite}' listening on 127.0.0.1:{app.port} (inside the environment)"
                )
                for r in planned:
                    self._abort_check()
                    rule = parse_rule(r.config)
                    assert isinstance(rule, HttpCheckRule)
                    self.evaluated.add(r.id)
                    self._add_checks(
                        [
                            evaluate_http(
                                rule,
                                host=app.host,
                                port=app.port,
                                rule_id=r.id or None,
                                name=r.name,
                                required=r.required,
                            )
                        ]
                    )
                info["suite"] = suite
                info["checks_executed"] = len(planned)
            finally:
                runner.stop()

    # ---- stage 5: finalization ------------------------------------------------------------
    def _skip_unrun(self) -> None:
        reason = (
            "the run was cancelled"
            if self.cancelled
            else (self.failure[1] if self.failure else "an earlier stage did not complete")
        )
        for name in _STAGE_ORDER:
            if name not in self.recorded:
                self.svc.store.skip_stage(self.run_id, self.owner, name, f"not executed: {reason}"[:300])
        present = {c.key for c in self.checks}
        extra: list[CheckResult] = []
        for key, label in (
            ("environment.provisioned", "Disposable restore environment provisioned"),
            ("restore.completed", "pg_restore completed without errors"),
        ):
            if key not in present:
                extra.append(CheckResult(key, label, "restore", Outcome.SKIPPED, True, f"Not executed: {reason}"[:300]))
        for rule in self.cfg.rules:
            if rule.id not in self.evaluated:
                from afterstate.domain.rules import rule_category

                extra.append(
                    CheckResult(
                        f"{rule_category(rule.kind)}.{rule.kind}",
                        rule.name,
                        rule_category(rule.kind),
                        Outcome.SKIPPED,
                        rule.required,
                        f"Not executed: {reason}"[:300],
                        rule_id=rule.id,
                    )
                )
        if extra:
            self._add_checks(extra)

    def _finalize(self) -> RunState:
        store, cfg = self.svc.store, self.cfg
        store.transition(self.run_id, RunState.FINALIZING, owner=self.owner)
        self.sink.stage = None
        self._skip_unrun()

        # cleanup first: the verdict must know whether temporary resources are really gone
        cleanup = self._cleanup_stage()

        completed = self.failure is None and not self.cancelled

        def sums(names: list[StageName]) -> float:
            return sum(self.timings.get(n, 0.0) for n in names)

        recovery_seconds = (
            sums([StageName.ARTIFACT_VERIFICATION, StageName.ENVIRONMENT, StageName.RESTORE]) + self.validation_seconds
        )
        self.metrics.update(
            artifact_verification_seconds=round(sums([StageName.ARTIFACT_VERIFICATION]), 3),
            provisioning_seconds=round(sums([StageName.ENVIRONMENT]), 3),
            validation_seconds=round(self.validation_seconds, 3),
            recovery_seconds=round(recovery_seconds, 3) if completed else None,
            cleanup_seconds=round(sums([StageName.CLEANUP]), 3),
            total_seconds=round(time.perf_counter() - self.t0, 3),
            checks=summarize_outcomes(self.checks),
        )
        if completed:
            rto = evaluate_rto(cfg.objectives.rto_seconds, recovery_seconds)
            rpo = evaluate_rpo(
                cfg.objectives.rpo_seconds,
                test_started_at=self.test_started_at,
                latest_recoverable_at=self.validator.state.latest_recoverable_at if self.validator else None,
                backup_created_at=cfg.artifact.backup_created_at,
            )
        else:

            def not_measured(name: str, target: int | None) -> ObjectiveResult:
                status = ObjectiveStatus.NOT_CONFIGURED if target is None else ObjectiveStatus.UNKNOWN
                return ObjectiveResult(
                    name, status, target, None, "The run did not reach completion, so this objective was not measured."
                )

            rto, rpo = not_measured("RTO", cfg.objectives.rto_seconds), not_measured("RPO", cfg.objectives.rpo_seconds)
        objectives = [rto, rpo]
        verdict = decide_verdict(
            cancelled=self.cancelled,
            execution_failure=self.failure[0] if self.failure else None,
            execution_failure_message=self.failure[1] if self.failure else None,
            checks=self.checks,
            objectives=objectives,
        )
        had_tasks = self.env is not None
        cleanup_state = (
            RunCleanupState.NOT_REQUIRED
            if not had_tasks
            else (RunCleanupState.DONE if cleanup.ok else RunCleanupState.FAILED)
        )
        final_state = verdict.state if cleanup.ok else RunState.NEEDS_CLEANUP
        failure_class = verdict.failure_class.value if verdict.failure_class else None
        summary = verdict.summary + (
            "" if cleanup.ok else " Temporary resources could not be removed; cleanup will be retried."
        )
        store.set_fields(
            self.run_id,
            self.owner,
            metrics=self.metrics,
            objectives={
                o.name: {
                    "status": o.status.value,
                    "target_seconds": o.target_seconds,
                    "measured_seconds": o.measured_seconds,
                    "detail": o.detail,
                    "basis": o.basis,
                }
                for o in objectives
            },
            failure_class=failure_class,
            failure_message="; ".join(verdict.reasons)[:1500] if verdict.reasons else None,
            summary=summary[:1500],
            cleanup_state=cleanup_state,
            settled_state=None if cleanup.ok else verdict.state,
        )
        self._report_stage(final_state)
        self.sink.emit(
            "INFO" if verdict.state is RunState.SUCCEEDED else "WARN",
            f"verdict: {verdict.state.value}. {summary}",
            kind="system",
        )
        self.sink.flush()
        store.transition(self.run_id, final_state, owner=self.owner)
        return final_state

    def _cleanup_stage(self) -> CleanupOutcome:
        store, svc = self.svc.store, self.svc
        if self.env is None:
            store.skip_stage(self.run_id, self.owner, StageName.CLEANUP, "no temporary resources were created")
            return CleanupOutcome(ok=True)
        stage_id = store.begin_stage(self.run_id, self.owner, StageName.CLEANUP)
        self.sink.stage = StageName.CLEANUP.value
        t0 = time.perf_counter()
        outcome = svc.cleanup.run_for_run(self.run_id, fail_once=bool(self.fault.get("fail_cleanup_once")))
        dur = time.perf_counter() - t0
        self.timings[StageName.CLEANUP] = dur
        if self.fault.get("fail_cleanup_once") and not outcome.ok:
            self.sink.emit(
                "WARN",
                "FAULT INJECTED: the first cleanup attempt failed (Recovery Lab)",
                kind="fault",
                data={"fault": "fail_cleanup_once"},
            )
        for note in outcome.notes:
            self.sink.emit("INFO", f"cleanup: {note}")
        for err in outcome.errors:
            self.sink.emit("ERROR", f"cleanup failed: {err}")
        self.sink.flush()
        store.end_stage(
            self.run_id,
            self.owner,
            stage_id,
            StageState.SUCCEEDED if outcome.ok else StageState.FAILED,
            int(dur * 1000),
            details={"notes": outcome.notes, "errors": outcome.errors},
            error="; ".join(outcome.errors)[:500] or None,
        )
        return outcome

    def _report_stage(self, final_state: RunState) -> None:
        store, storage = self.svc.store, self.svc.storage
        stage_id = store.begin_stage(self.run_id, self.owner, StageName.REPORT)
        self.sink.stage = StageName.REPORT.value
        t0 = time.perf_counter()
        try:
            transcript = ("\n".join(self.sink.transcript) + "\n").encode("utf-8")
            key = new_evidence_key(self.run_id, "execution.log")
            stored = storage.write_bytes(key, transcript)
            store.add_evidence(
                self.run_id, "execution_log", key, stored.sha256, stored.size_bytes, "text/plain; charset=utf-8"
            )
            persist_report(self.svc.sf, store, storage, self.run_id, state_override=final_state)
            store.end_stage(
                self.run_id,
                self.owner,
                stage_id,
                StageState.SUCCEEDED,
                int((time.perf_counter() - t0) * 1000),
                details={"evidence": ["execution_log", "report_json", "report_html"]},
            )
        except LeaseLost:
            raise
        except Exception as exc:
            log.exception("report generation failed")
            self.sink.emit("ERROR", f"report generation failed: {type(exc).__name__}: {exc}")
            store.end_stage(
                self.run_id,
                self.owner,
                stage_id,
                StageState.FAILED,
                int((time.perf_counter() - t0) * 1000),
                error=str(exc)[:500],
            )


def path_exists(p: Path) -> bool:  # small helper kept for tests/ops scripts
    return p.exists()
