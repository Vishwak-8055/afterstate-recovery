from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from afterstate.lab.fixtures import shop_rules
from tests.factories import make_artifact, make_project, make_workspace
from tests.integration.helpers import drain, snapshot, submit

pytestmark = pytest.mark.integration


def test_good_backup_is_restored_verified_and_reported(svc, fixtures):  # type: ignore[no-untyped-def]
    ws, _ = make_workspace(svc)
    art = make_artifact(svc, ws, fixtures.path("good"), backup_created_at=datetime.now(UTC) - timedelta(hours=1))
    project = make_project(
        svc,
        ws,
        art,
        rules=shop_rules(fixtures.meta),
        rto_seconds=300,
        rpo_seconds=48 * 3600,
        app_suite="reference-shop",
    )
    run_id = submit(svc, project.id, art.id)

    assert drain(svc) == 1
    snap = snapshot(svc, run_id)
    assert snap.state == "SUCCEEDED", snap.dump()
    assert snap.run.failure_class is None
    assert all(r.outcome.value == "PASS" for r in snap.results if r.required), snap.dump()

    # every stage executed for real, in order
    assert snap.stages == {
        "artifact_verification": "SUCCEEDED",
        "environment_provisioning": "SUCCEEDED",
        "restore": "SUCCEEDED",
        "structural_validation": "SUCCEEDED",
        "data_validation": "SUCCEEDED",
        "application_validation": "SUCCEEDED",
        "cleanup": "SUCCEEDED",
        "report": "SUCCEEDED",
    }
    # the dataset comparison is a complete comparison; counts are partial
    ds = next(r for r in snap.results if r.key == "data.dataset_checksum")
    assert ds.coverage == "complete" and ds.actual["rows"] == fixtures.meta["customers_rows"]
    assert next(r for r in snap.results if r.key == "data.row_count").coverage == "partial"
    # application stage really started the reference app and hit it over HTTP
    http = [r for r in snap.results if r.key == "application.http_check"]
    assert len(http) == 4 and all(r.outcome.value == "PASS" and r.actual["status"] == 200 for r in http)

    # measured metrics, not invented ones
    m = snap.run.metrics
    assert m["restore_seconds"] > 0 and m["recovery_seconds"] >= m["restore_seconds"]
    assert m["restored_database_bytes"] > 1_000_000 and m["restore_throughput_mb_s"] > 0
    assert m["restore_items_done"] == m["restore_items_total"] > 20  # progress came from real pg_restore events
    assert m["checks"]["FAIL"] == 0 and m["checks"]["PASS"] >= 25

    # objectives
    assert snap.run.objectives["RTO"]["status"] == "MET" and snap.run.objectives["RPO"]["status"] == "MET"
    assert snap.run.objectives["RPO"]["basis"].startswith("recovery-point probe")

    # cleanup: environment gone, task recorded and DONE
    assert snap.run.cleanup_state.value == "DONE"
    assert [t.state.value for t in snap.tasks] == ["DONE"]
    assert not list(Path(svc.settings.scratch_dir).glob("e-*"))

    # evidence is stored with checksums that match the bytes on disk
    assert {"execution_log", "report_json", "report_html"} <= set(snap.evidence)
    for ev in snap.evidence.values():
        assert hashlib.sha256(svc.storage.read_bytes(ev.storage_key)).hexdigest() == ev.sha256
    report = svc.storage.read_bytes(snap.evidence["report_json"].storage_key).decode()
    assert snap.run.id.hex[:8] in report.replace("-", "") and "Recovery verified" in report
    html = svc.storage.read_bytes(snap.evidence["report_html"].storage_key).decode()
    assert "<!doctype html>" in html and art.sha256 in html
    log = svc.storage.read_bytes(snap.evidence["execution_log"].storage_key).decode()
    assert "pg_restore: creating" in log, "the execution log must contain the real tool output"
    assert "password" not in log.lower() and "PGPASSWORD" not in log
