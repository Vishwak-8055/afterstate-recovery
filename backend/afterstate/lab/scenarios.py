"""Recovery Lab scenario catalog.

Each scenario is a real archive, a real project configuration and (for four of them) a fault that is
only honoured for lab runs. Expectations are machine-checked against what actually happened."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from afterstate.lab.fixtures import shop_rules


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    summary: str
    variant: str
    initial_conditions: list[str]
    expected: str  # human statement of what must happen
    rules: Callable[[dict[str, Any]], list[dict[str, Any]]] = shop_rules
    project: dict[str, Any] = field(default_factory=dict)
    fault: dict[str, Any] | None = None
    fault_description: str | None = None
    expected_state: str = "SUCCEEDED"
    expected_failure_class: str | None = None
    multi_run: bool = False


def _basic(meta: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in shop_rules(meta) if r["kind"] in ("table", "schema", "row_count")]


SCENARIOS: list[Scenario] = [
    Scenario(
        "successful_restore",
        "Successful restore",
        "A healthy backup is restored and passes every structural, data, application and objective check.",
        "good",
        [
            "Real pg_dump -Fc archive of a synthetic shop (2,000 customers, 6,000 orders, ledger).",
            "17 project rules including a complete customer dataset comparison and a recovery-point probe.",
            "RTO 120 s, RPO 48 h, reference application smoke suite.",
        ],
        "SUCCEEDED, every required check PASS, RTO and RPO MET, environment removed.",
        project={"rto_seconds": 120, "rpo_seconds": 48 * 3600, "app_suite": "reference-shop"},
    ),
    Scenario(
        "corrupted_backup",
        "Corrupted backup",
        "Bytes inside the archive are damaged. The deep scan must reject it before any database is created.",
        "corrupt",
        [
            "The same archive as the healthy case, with 60 bytes inside the data region overwritten.",
            "The recorded checksum is taken from the damaged file, so only the integrity scan can catch this.",
        ],
        "FAILED with ARTIFACT_CORRUPT; no environment is ever created; every rule is SKIPPED.",
        expected_state="FAILED",
        expected_failure_class="ARTIFACT_CORRUPT",
    ),
    Scenario(
        "unsupported_format",
        "Unsupported backup format",
        "A plain-SQL dump is refused: it is an executable script for psql, not an archive.",
        "plain",
        ["Real pg_dump -Fp output (about 1 MB of SQL text)."],
        "FAILED with FORMAT_UNSUPPORTED; nothing in the file is executed.",
        expected_state="FAILED",
        expected_failure_class="FORMAT_UNSUPPORTED",
    ),
    Scenario(
        "missing_schema",
        "Missing required schema",
        "The restore works, but the backup was taken from a database without the billing schema.",
        "no_billing",
        [
            "A real archive from a database where the billing schema was dropped before the dump.",
            "The project requires the billing schema and its ledger table.",
        ],
        "FAILED with VALIDATION_FAILED. Restore succeeds, structural checks fail, dependent queries are INCONCLUSIVE.",
        expected_state="FAILED",
        expected_failure_class="VALIDATION_FAILED",
    ),
    Scenario(
        "failed_invariant",
        "Failed business invariant",
        "The data restores and the schema is intact, but order totals and ledger balance no longer reconcile.",
        "broken_invariant",
        [
            "A real archive in which some order totals were altered and ledger entries deleted before the dump.",
            "Customers are untouched, so the customer dataset comparison still passes.",
        ],
        "FAILED with VALIDATION_FAILED. Invariant checks FAIL while independent checks still PASS.",
        expected_state="FAILED",
        expected_failure_class="VALIDATION_FAILED",
    ),
    Scenario(
        "restore_timeout",
        "Restore timeout",
        "An index build that is slow by construction exceeds the project's 4 second restore limit.",
        "slow",
        [
            "A real archive with an expression index whose function sleeps 10 ms per row only for the restore role (4,000 rows, about 40 s).",
            "Restore timeout: 4 seconds.",
        ],
        "FAILED with RESTORE_TIMEOUT; pg_restore is terminated; the environment is removed.",
        rules=lambda meta: [],
        project={"restore_timeout_seconds": 4},
        expected_state="FAILED",
        expected_failure_class="RESTORE_TIMEOUT",
    ),
    Scenario(
        "interrupted_worker",
        "Interrupted worker",
        "The worker process is killed with SIGKILL in the middle of the restore; the platform must recover from durable state.",
        "good",
        [
            "The healthy archive and a small rule set.",
            "A real child worker process executes the run and is killed during RESTORING.",
            "Its disposable PostgreSQL is orphaned and keeps running.",
            "Retry policy: 2 attempts, 2 s backoff.",
        ],
        "Attempt 1: FAILED with WORKER_INTERRUPTED after lease expiry, orphan terminated, cleanup DONE. Attempt 2: SUCCEEDED.",
        rules=_basic,
        project={"max_attempts": 2},
        fault={"stall_restore_seconds": 300, "kill_worker_during_restore": True},
        fault_description="Restore is held open, then the executing worker process receives SIGKILL.",
        expected_state="SUCCEEDED",
        multi_run=True,
    ),
    Scenario(
        "unavailable_database",
        "Unavailable database",
        "The restored PostgreSQL is killed just before validation starts.",
        "good",
        ["The healthy archive and the full rule set."],
        "FAILED with ENVIRONMENT_UNAVAILABLE; no check is falsely reported as passed; remaining rules are SKIPPED.",
        fault={"kill_environment_before_validation": True},
        fault_description="SIGKILL is sent to the restored database process before the validation stage.",
        expected_state="FAILED",
        expected_failure_class="ENVIRONMENT_UNAVAILABLE",
    ),
    Scenario(
        "validation_query_failure",
        "Validation query failure",
        "A configured assertion references a column that does not exist, so its result cannot be known.",
        "good",
        ["The healthy archive.", "One assertion queries a missing column; two table checks are valid."],
        "NEEDS_ATTENTION with VALIDATION_INCONCLUSIVE: an unevaluable required check is never a success.",
        rules=lambda meta: [
            {"kind": "table", "table": "orders"},
            {"kind": "table", "table": "customers"},
            {
                "kind": "sql_assertion",
                "name": "Assertion on a column that does not exist",
                "sql": "SELECT count(missing_column) > 0 FROM orders",
            },
        ],
        expected_state="NEEDS_ATTENTION",
        expected_failure_class="VALIDATION_INCONCLUSIVE",
    ),
    Scenario(
        "cleanup_failure",
        "Cleanup failure",
        "The first attempt to remove the disposable environment fails; the run must not look finished while resources remain.",
        "good",
        ["The healthy archive and a small rule set.", "Cleanup retry delay: 30 s (worker setting)."],
        "NEEDS_CLEANUP while the environment still exists, then SUCCEEDED after a verified retry.",
        rules=_basic,
        fault={"fail_cleanup_once": True},
        fault_description="The first cleanup attempt raises an error.",
        expected_state="SUCCEEDED",
    ),
]
BY_KEY = {s.key: s for s in SCENARIOS}
