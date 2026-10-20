"""Failure-path behaviour of the real recovery engine. Every test uses real pg_dump archives, a real
disposable PostgreSQL and the real worker; faults are injected only through the lab mechanism."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from afterstate.db.models import BackupArtifact, RecoveryRun
from afterstate.lab.fixtures import shop_rules
from afterstate.runs import ActiveRunExists
from afterstate.worker import reaper
from afterstate.worker.runner import Worker
from sqlalchemy import select, update
from tests.factories import make_artifact, make_project, make_workspace
from tests.integration.helpers import drain, processes_mentioning, snapshot, submit, wait_for

pytestmark = pytest.mark.integration


def _scenario(svc, fixtures, variant, rules=None, **project_fields):  # type: ignore[no-untyped-def]
    ws, user = make_workspace(svc)
    art = make_artifact(svc, ws, fixtures.path(variant), backup_created_at=datetime.now(UTC) - timedelta(hours=1))
    project = make_project(
        svc, ws, art, rules=rules if rules is not None else shop_rules(fixtures.meta), **project_fields
    )
    return ws, art, project


def _artifact_status(svc, art_id):  # type: ignore[no-untyped-def]
    with svc.sf() as s:
        return s.get(BackupArtifact, art_id).status.value


# ---------------------------------------------------------------- artifact rejection -------------
@pytest.mark.parametrize("variant", ["corrupt", "truncated"])
def test_damaged_artifacts_are_rejected_before_any_environment_exists(svc, fixtures, variant):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, variant)
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "ARTIFACT_CORRUPT", snap.dump()
    assert snap.stages["artifact_verification"] == "FAILED"
    assert snap.stages["environment_provisioning"] == "SKIPPED" and snap.stages["restore"] == "SKIPPED"
    assert snap.run.cleanup_state.value == "NOT_REQUIRED" and not snap.tasks  # no resource was ever created
    assert snap.outcome_of("artifact.integrity") == "FAIL"
    # every project rule is recorded as SKIPPED, never silently absent
    rules = [r for r in snap.results if r.rule_id]
    assert len(rules) == len(shop_rules(fixtures.meta)) and all(r.outcome.value == "SKIPPED" for r in rules)
    assert _artifact_status(svc, art.id) == "rejected"


def test_toc_corruption_that_crashes_pg_restore_is_classified_corrupt(svc, fixtures, tmp_path):  # type: ignore[no-untyped-def]
    data = bytearray(fixtures.path("good").read_bytes())
    for i in range(300, 340):
        data[i] ^= 0xFF
    bad = tmp_path / "toc-damaged.dump"
    bad.write_bytes(bytes(data))
    ws, user = make_workspace(svc)
    art = make_artifact(svc, ws, bad)
    project = make_project(svc, ws, art, rules=[])
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "ARTIFACT_CORRUPT", snap.dump()
    assert "crashed" in (snap.run.failure_message or "") or "corrupt" in (snap.run.failure_message or "").lower()


def test_plain_sql_dump_is_rejected_as_unsupported_and_never_executed(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "plain")
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "FORMAT_UNSUPPORTED", snap.dump()
    assert "psql" in snap.run.failure_message and snap.stages["environment_provisioning"] == "SKIPPED"


def test_modification_after_registration_is_detected_by_checksum(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good")
    with svc.storage.resolve(art.storage_key).open("ab") as fh:
        fh.write(b"tampered")
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "ARTIFACT_CHECKSUM_MISMATCH", snap.dump()


def test_missing_artifact_file(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good")
    svc.storage.resolve(art.storage_key).unlink()
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "ARTIFACT_MISSING", snap.dump()


# ---------------------------------------------------------------- verification semantics ---------
def test_missing_schema_fails_the_run_while_independent_checks_still_report(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "no_billing")
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "VALIDATION_FAILED", snap.dump()
    assert snap.outcome_of("Billing schema exists") == "FAIL"
    assert snap.outcome_of("Table public.orders exists") == "PASS"  # restore itself worked and is still evidenced
    assert snap.outcome_of("restore.completed") == "PASS"
    assert snap.outcome_of("Ledger balance") == "INCONCLUSIVE"  # the query cannot run without the schema
    assert snap.run.cleanup_state.value == "DONE"


def test_failed_business_invariant_prevents_success(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "broken_invariant")
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "VALIDATION_FAILED", snap.dump()
    assert snap.outcome_of("Order totals") == "FAIL" and snap.outcome_of("Ledger balance") == "FAIL"
    assert snap.outcome_of("Customer reference dataset") == "PASS"  # customers were untouched: checks are independent
    assert snap.run.objectives["RTO"]["status"] == "UNKNOWN" or snap.run.objectives["RTO"]["status"] == "NOT_CONFIGURED"


def test_validation_query_failure_is_inconclusive_never_success(svc, fixtures):  # type: ignore[no-untyped-def]
    rules = [
        {
            "kind": "sql_assertion",
            "name": "References a column that does not exist",
            "sql": "SELECT count(no_such_column) > 0 FROM orders",
        }
    ]
    ws, art, project = _scenario(svc, fixtures, "good", rules=rules)
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "NEEDS_ATTENTION" and snap.run.failure_class == "VALIDATION_INCONCLUSIVE", snap.dump()
    assert snap.outcome_of("References a column") == "INCONCLUSIVE"


def test_required_check_that_is_skipped_does_not_count_as_success(svc, fixtures):  # type: ignore[no-untyped-def]
    rules = [{"kind": "http_check", "name": "Orders endpoint", "path": "/api/orders/recent"}]
    ws, art, project = _scenario(svc, fixtures, "good", rules=rules)  # no application suite configured
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.outcome_of("Orders endpoint") == "SKIPPED"
    assert snap.state == "NEEDS_ATTENTION", snap.dump()


def test_optional_failing_check_is_a_warning_not_a_failure(svc, fixtures):  # type: ignore[no-untyped-def]
    rules = [{"kind": "table", "table": "orders"}, {"kind": "table", "table": "does_not_exist", "required": False}]
    ws, art, project = _scenario(svc, fixtures, "good", rules=rules)
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.outcome_of("does_not_exist") == "FAIL" and snap.state == "SUCCEEDED", snap.dump()


def test_missed_objective_blocks_success(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[{"kind": "table", "table": "orders"}], rto_seconds=1)
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "NEEDS_ATTENTION" and snap.run.failure_class == "OBJECTIVE_MISSED", snap.dump()
    assert snap.run.objectives["RTO"]["status"] == "MISSED"


def test_rpo_is_unknown_not_fabricated_without_evidence(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, user = make_workspace(svc)
    art = make_artifact(svc, ws, fixtures.path("good"))  # no declared backup time
    project = make_project(svc, ws, art, rules=[{"kind": "table", "table": "orders"}], rpo_seconds=3600)
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.run.objectives["RPO"]["status"] == "UNKNOWN" and snap.state == "NEEDS_ATTENTION"
    assert snap.run.failure_class == "OBJECTIVE_UNKNOWN"


# ---------------------------------------------------------------- restore failures ---------------
def test_restore_timeout_terminates_restore_and_cleans_up(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "slow", rules=[], restore_timeout_seconds=4)
    started = time.monotonic()
    run_id = submit(svc, project.id, art.id)
    drain(svc)
    assert time.monotonic() - started < 45  # the 40 s index build was cut off at the limit
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "RESTORE_TIMEOUT", snap.dump()
    assert snap.run.cleanup_state.value == "DONE" and not processes_mentioning("e-" + run_id.hex[:12])


def test_environment_killed_before_validation_is_classified_and_retryable_class(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good")
    run_id = submit(svc, project.id, art.id, fault={"kill_environment_before_validation": True}, lab_scenario="t")
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "ENVIRONMENT_UNAVAILABLE", snap.dump()
    skipped = [r for r in snap.results if r.rule_id and r.outcome.value == "SKIPPED"]
    assert len(skipped) == len(shop_rules(fixtures.meta))  # nothing was falsely reported as verified
    assert snap.run.cleanup_state.value == "DONE"


def test_faults_are_ignored_for_runs_that_are_not_lab_runs(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[{"kind": "table", "table": "orders"}])
    run_id = submit(svc, project.id, art.id, fault={"kill_environment_before_validation": True})  # no lab_scenario
    drain(svc)
    assert snapshot(svc, run_id).state == "SUCCEEDED"


# ---------------------------------------------------------------- cleanup ------------------------
def test_cleanup_failure_leaves_needs_cleanup_then_settles_after_retry(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[{"kind": "table", "table": "orders"}])
    run_id = submit(svc, project.id, art.id, fault={"fail_cleanup_once": True}, lab_scenario="t")
    drain(svc)
    snap = snapshot(svc, run_id)
    assert snap.state == "NEEDS_CLEANUP" and snap.run.settled_state.value == "SUCCEEDED", snap.dump()
    assert (
        snap.run.cleanup_state.value == "FAILED"
        and snap.tasks[0].state.value == "FAILED"
        and snap.tasks[0].attempts == 1
    )
    assert list(Path(svc.settings.scratch_dir).glob("e-*")), (
        "the environment is still there, which is why the run is NEEDS_CLEANUP"
    )

    time.sleep(1.2)  # cleanup retry delay is 1 s in tests
    assert reaper.retry_pending_cleanups(svc) == [run_id]
    snap = snapshot(svc, run_id)
    assert snap.state == "SUCCEEDED" and snap.run.cleanup_state.value == "DONE" and snap.tasks[0].attempts == 2
    assert not list(Path(svc.settings.scratch_dir).glob("e-*"))
    assert "Recovery verified" in svc.storage.read_bytes(snap.evidence["report_json"].storage_key).decode()


def test_cleanup_refuses_to_delete_paths_it_does_not_own(svc, fixtures, tmp_path):  # type: ignore[no-untyped-def]
    from afterstate.engine.environment import LocalClusterEnvironment, UnsafeCleanup

    scratch = Path(svc.settings.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    decoy = scratch / "e-decoy000000"
    decoy.mkdir()
    (decoy / "precious.txt").write_text("do not delete")
    with pytest.raises(UnsafeCleanup):
        LocalClusterEnvironment.destroy_path(decoy, scratch)  # no ownership marker
    with pytest.raises(UnsafeCleanup):
        LocalClusterEnvironment.destroy_path(tmp_path, scratch)  # outside the scratch root
    assert (decoy / "precious.txt").exists()
    import shutil

    shutil.rmtree(decoy)


# ---------------------------------------------------------------- cancellation -------------------
def test_cancelling_a_running_restore_stops_it_and_cleans_up(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[])
    run_id = submit(svc, project.id, art.id, fault={"stall_restore_seconds": 120}, lab_scenario="t")
    worker = Worker(svc)
    claimed = worker.claim_next()
    assert claimed == run_id
    t = threading.Thread(target=worker.execute, args=(run_id,), daemon=True)
    t.start()
    wait_for(lambda: snapshot(svc, run_id).stages.get("restore") == "RUNNING", 60, message="restore to start")
    with svc.sf() as s, s.begin():
        s.execute(update(RecoveryRun).where(RecoveryRun.id == run_id).values(cancel_requested=True))
    t.join(timeout=40)
    assert not t.is_alive()
    snap = snapshot(svc, run_id)
    assert snap.state == "CANCELLED" and snap.run.cleanup_state.value == "DONE", snap.dump()
    assert snap.run.failure_class == "CANCELLED" and not list(Path(svc.settings.scratch_dir).glob("e-*"))


# ---------------------------------------------------------------- worker interruption ------------
@pytest.mark.slow
def test_worker_killed_mid_restore_is_recovered_from_durable_state(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[{"kind": "table", "table": "orders"}], max_attempts=2)
    run_id = submit(
        svc,
        project.id,
        art.id,
        fault={"stall_restore_seconds": 300, "kill_worker_during_restore": True},
        lab_scenario="t",
        retry_backoff=1,
    )
    worker = Worker(svc)
    assert worker.process_one()  # spawns a real child worker, then SIGKILLs it during RESTORING

    snap = snapshot(svc, run_id)
    assert snap.state == "RESTORING", "the run is stranded in an active state: only the lease can reveal the crash"
    env_fragment = "e-" + run_id.hex[:12]
    assert processes_mentioning(env_fragment), (
        "the orphaned disposable PostgreSQL must still be running (that is the point)"
    )

    time.sleep(7)  # lease is 6 s
    assert reaper.recover_interrupted_runs(svc) == [run_id]
    snap = snapshot(svc, run_id)
    assert snap.state == "FAILED" and snap.run.failure_class == "WORKER_INTERRUPTED", snap.dump()
    assert snap.run.cleanup_state.value == "DONE" and snap.tasks[0].state.value == "DONE"
    assert not processes_mentioning(env_fragment), "orphaned postgres must be terminated"
    assert not list(Path(svc.settings.scratch_dir).glob("e-*"))
    assert snap.stages["restore"] == "FAILED" and snap.stages["structural_validation"] == "SKIPPED"

    # the retryable failure queued a fresh attempt, with the fault cleared; it now succeeds
    with svc.sf() as s:
        child = s.scalars(select(RecoveryRun).where(RecoveryRun.parent_run_id == run_id)).one()
        child_id = child.id
        assert child.attempt == 2 and child.fault is None and child.trigger.value == "retry"
    time.sleep(1.2)
    assert drain(svc) == 1
    assert snapshot(svc, child_id).state == "SUCCEEDED"


# ---------------------------------------------------------------- submission rules ---------------
def test_only_one_run_per_project_may_be_queued_or_active(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, art, project = _scenario(svc, fixtures, "good", rules=[{"kind": "table", "table": "orders"}])
    first = submit(svc, project.id, art.id)
    with pytest.raises(ActiveRunExists):
        submit(svc, project.id, art.id)
    drain(svc)
    assert snapshot(svc, first).state == "SUCCEEDED"
    assert submit(svc, project.id, art.id)  # slot is free again


def test_workspace_concurrency_limit_is_enforced_at_claim_time(svc, fixtures):  # type: ignore[no-untyped-def]
    from afterstate.db.models import Workspace

    ws, user = make_workspace(svc)
    with svc.sf() as s, s.begin():
        s.execute(update(Workspace).where(Workspace.id == ws.id).values(max_concurrent_runs=1))
    art = make_artifact(svc, ws, fixtures.path("good"))
    p1 = make_project(svc, ws, art, name="one", rules=[])
    p2 = make_project(svc, ws, art, name="two", rules=[])
    r1, r2 = submit(svc, p1.id, art.id), submit(svc, p2.id, art.id)
    w = Worker(svc)
    first = w.claim_next()
    assert first in (r1, r2)
    assert w.claim_next() is None, "the second run must wait: the workspace limit is 1"
    with svc.sf() as s, s.begin():
        s.execute(update(RecoveryRun).where(RecoveryRun.id == first).values(state="CANCELLED", lease_owner=None))
    assert w.claim_next() is not None
    with svc.sf() as s, s.begin():  # leave nothing active for fixture teardown
        s.execute(
            update(RecoveryRun).where(RecoveryRun.state == "PREPARING").values(state="CANCELLED", lease_owner=None)
        )


def test_stale_owner_cannot_write_after_losing_its_lease(svc, fixtures):  # type: ignore[no-untyped-def]
    from afterstate.engine.store import EventRecord, LeaseLost

    ws, art, project = _scenario(svc, fixtures, "good", rules=[])
    run_id = submit(svc, project.id, art.id)
    w = Worker(svc)
    assert w.claim_next() == run_id
    svc.store.add_events(run_id, w.worker_id, [EventRecord("INFO", "hello from the legitimate owner")])
    with svc.sf() as s, s.begin():
        s.execute(update(RecoveryRun).where(RecoveryRun.id == run_id).values(lease_owner="someone-else"))
    with pytest.raises(LeaseLost):
        svc.store.add_events(run_id, w.worker_id, [EventRecord("INFO", "zombie write")])
    assert select  # keep import used
    with svc.sf() as s, s.begin():
        s.execute(update(RecoveryRun).where(RecoveryRun.id == run_id).values(state="CANCELLED", lease_owner=None))
