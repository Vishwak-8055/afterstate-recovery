"""Worker process: claims queued runs, executes them with a heartbeat lease, and performs the
periodic maintenance that keeps the platform self-healing (reaper, scheduler, cleanup, webhooks)."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text

from afterstate import __version__
from afterstate.db.base import utcnow
from afterstate.db.models import RecoveryRun, RunEvent, RunStage, WorkerHeartbeat
from afterstate.domain.enums import RunState, StageName, StageState
from afterstate.domain.state_machine import ACTIVE_STATES, TERMINAL_STATES, assert_transition
from afterstate.engine.pipeline import RunPipeline, Services
from afterstate.engine.store import EventRecord, LeaseLost, RunStore
from afterstate.worker import notify, reaper, scheduler
from afterstate.worker.postrun import post_run

log = logging.getLogger("afterstate.worker")


class LeaseKeeper(threading.Thread):
    """Extends the lease while a run executes, relays cancellation, and detects lease loss."""

    def __init__(
        self,
        store: RunStore,
        run_id: uuid.UUID,
        owner: str,
        lease_seconds: int,
        interval: float,
        cancel: threading.Event,
        lost: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name=f"lease-{str(run_id)[:8]}")
        self.store, self.run_id, self.owner = store, run_id, owner
        self.lease_seconds, self.interval, self.cancel, self.lost = lease_seconds, interval, cancel, lost
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                owner_ok, cancel_requested = self.store.touch_lease(self.run_id, self.owner, self.lease_seconds)
            except Exception:
                log.exception("heartbeat failed; will retry")
                continue
            if not owner_ok:
                log.error("lease for run %s lost", self.run_id)
                self.lost.set()
                return
            if cancel_requested:
                self.cancel.set()


class Worker:
    def __init__(self, svc: Services, *, worker_id: str | None = None) -> None:
        self.svc = svc
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.started_at = utcnow()
        self.current_run: uuid.UUID | None = None
        self._last = {"reap": 0.0, "schedule": 0.0, "cleanup": 0.0, "notify": 0.0, "beat": 0.0}

    # ---- registration / health -------------------------------------------------------------
    def beat(self) -> None:
        with self.svc.sf() as s, s.begin():
            row = s.get(WorkerHeartbeat, self.worker_id)
            if row is None:
                row = WorkerHeartbeat(
                    worker_id=self.worker_id,
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    version=__version__,
                    started_at=self.started_at,
                )
                s.add(row)
            row.last_seen_at, row.current_run_id = utcnow(), self.current_run

    # ---- claiming --------------------------------------------------------------------------
    def claim_next(self) -> uuid.UUID | None:
        """Atomically claim the oldest eligible queued run, honouring the per-workspace concurrency limit."""
        settings = self.svc.settings
        now = utcnow()
        with self.svc.sf() as s, s.begin():
            candidates = list(
                s.scalars(
                    select(RecoveryRun)
                    .where(
                        RecoveryRun.state == RunState.QUEUED,
                        RecoveryRun.not_before <= now,
                        RecoveryRun.cancel_requested.is_(False),
                    )
                    .order_by(RecoveryRun.queued_at)
                    .limit(10)
                    .with_for_update(skip_locked=True)
                )
            )
            for run in candidates:
                s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": str(run.workspace_id)})
                from afterstate.db.models import Workspace

                ws = s.get(Workspace, run.workspace_id)
                limit = ws.max_concurrent_runs if ws else settings.max_concurrent_runs_default
                active = s.scalar(
                    select(func.count())
                    .select_from(RecoveryRun)
                    .where(RecoveryRun.workspace_id == run.workspace_id, RecoveryRun.state.in_(ACTIVE_STATES))
                )
                if (active or 0) >= limit:
                    continue
                assert_transition(RunState.QUEUED, RunState.PREPARING)
                run.state, run.state_entered_at, run.started_at = RunState.PREPARING, now, now
                run.lease_owner, run.heartbeat_at = self.worker_id, now
                run.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
                s.add(
                    RunEvent(
                        run_id=run.id,
                        ts=now,
                        level="INFO",
                        kind="state",
                        message=f"QUEUED -> PREPARING (claimed by {self.worker_id})",
                    )
                )
                return run.id
        return None

    # ---- execution -------------------------------------------------------------------------
    def execute(
        self,
        run_id: uuid.UUID,
        *,
        owner: str | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: float | None = None,
    ) -> RunState | None:
        owner = owner or self.worker_id
        s = self.svc.settings
        cancel, lost = threading.Event(), threading.Event()
        keeper = LeaseKeeper(
            self.svc.store,
            run_id,
            owner,
            lease_seconds or s.worker_lease_seconds,
            heartbeat_seconds or s.worker_heartbeat_seconds,
            cancel,
            lost,
        )
        keeper.start()
        self.current_run = run_id
        final: RunState | None = None
        try:
            final = RunPipeline(self.svc, run_id, owner, cancel, lost).execute()
        except LeaseLost:
            log.warning("run %s was taken over by another actor; this worker stopped touching it", run_id)
        except Exception:
            log.exception("pipeline crashed unexpectedly for run %s; the reaper will recover it", run_id)
        finally:
            keeper.stop()
            self.current_run = None
        if final in TERMINAL_STATES:
            post_run(self.svc, run_id)
        return final

    def process_one(self) -> bool:
        run_id = self.claim_next()
        if run_id is None:
            return False
        with self.svc.sf() as s:
            run = s.get(RecoveryRun, run_id)
            fault = dict(run.fault or {}) if run is not None and run.lab_scenario else {}
        if fault.get("kill_worker_during_restore") and self.svc.settings.fault_injection_enabled:
            self._execute_then_kill(run_id)
        else:
            self.execute(run_id)
        return True

    def _execute_then_kill(self, run_id: uuid.UUID) -> None:
        """Recovery Lab 'interrupted worker': hand the run to a real child worker process and SIGKILL it
        mid-restore. Nothing is simulated; the reaper must recover the run from durable state."""
        store = self.svc.store
        child_owner = f"{self.worker_id}/child-{uuid.uuid4().hex[:6]}"
        lease = 6
        store.set_fields(
            run_id, self.worker_id, lease_owner=child_owner, lease_expires_at=utcnow() + timedelta(seconds=lease)
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k.startswith("AFTERSTATE_") or k in ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV")
        }
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "afterstate.worker",
                "execute",
                "--run-id",
                str(run_id),
                "--owner",
                child_owner,
                "--lease-seconds",
                str(lease),
                "--heartbeat-seconds",
                "1",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 120
        reached = False
        while time.monotonic() < deadline and child.poll() is None:
            with self.svc.sf() as s:
                stage = s.scalar(
                    select(RunStage.state).where(RunStage.run_id == run_id, RunStage.name == StageName.RESTORE)
                )
                fault_event = s.scalar(
                    select(func.count())
                    .select_from(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.kind == "fault")
                )
            if stage == StageState.RUNNING and fault_event:
                reached = True
                break
            time.sleep(0.2)
        if reached and child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            store.add_events(
                run_id,
                None,
                [
                    EventRecord(
                        "ERROR",
                        f"FAULT INJECTED: worker process {child.pid} killed with SIGKILL during RESTORING (Recovery Lab). Its disposable PostgreSQL is now orphaned; the reaper must find it from durable state.",
                        "restore",
                        "fault",
                        {"fault": "kill_worker_during_restore", "pid": child.pid},
                    )
                ],
            )
            log.warning("lab: killed child worker %s for run %s", child.pid, run_id)
        else:
            child.wait(timeout=30)

    # ---- maintenance -----------------------------------------------------------------------
    def maintenance_once(self) -> None:
        now = time.monotonic()
        s = self.svc.settings

        def due(name: str, every: float) -> bool:
            return now - self._last[name] >= every

        try:
            if due("beat", 5):
                self._last["beat"] = now
                self.beat()
            if due("reap", 2):
                self._last["reap"] = now
                reaper.recover_interrupted_runs(self.svc)
            if due("schedule", s.scheduler_tick_seconds):
                self._last["schedule"] = now
                scheduler.tick(self.svc)
            if due("cleanup", 5):
                self._last["cleanup"] = now
                reaper.retry_pending_cleanups(self.svc)
            if due("notify", 5):
                self._last["notify"] = now
                notify.dispatch_pending(self.svc)
        except Exception:
            log.exception("maintenance pass failed; will retry")

    def run_forever(self, stop: threading.Event) -> None:
        log.info("worker %s started (AFTERSTATE %s)", self.worker_id, __version__)

        def maintenance() -> None:
            while not stop.wait(1.0):
                self.maintenance_once()

        threading.Thread(target=maintenance, name="maintenance", daemon=True).start()
        while not stop.is_set():
            try:
                if not self.process_one():
                    stop.wait(self.svc.settings.worker_poll_seconds)
            except Exception:
                log.exception("worker loop error")
                stop.wait(2.0)
        log.info("worker %s stopping", self.worker_id)

    def drain(self, *, max_runs: int = 100) -> int:
        """Process queued runs until none are eligible (used by tests, CI and the demo)."""
        done = 0
        while done < max_runs and self.process_one():
            done += 1
        return done
