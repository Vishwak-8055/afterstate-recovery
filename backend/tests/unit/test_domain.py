from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from afterstate.domain.checks import CheckResult, ObjectiveResult, decide_verdict
from afterstate.domain.enums import FailureClass, ObjectiveStatus, Outcome, RunState
from afterstate.domain.failures import classify_tool_failure
from afterstate.domain.objectives import evaluate_rpo, evaluate_rto
from afterstate.domain.state_machine import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    SLOT_STATES,
    TERMINAL_STATES,
    InvalidTransition,
    assert_transition,
    can_transition,
)

S = RunState


# ------------------------------------------------------------------------ state machine ---------
def test_happy_path_and_every_documented_edge():
    path = [S.QUEUED, S.PREPARING, S.RESTORING, S.VALIDATING, S.FINALIZING, S.SUCCEEDED]
    for a, b in itertools.pairwise(path):
        assert can_transition(a, b)
    assert ALLOWED_TRANSITIONS[S.FINALIZING] == {S.SUCCEEDED, S.FAILED, S.CANCELLED, S.NEEDS_ATTENTION, S.NEEDS_CLEANUP}
    assert ALLOWED_TRANSITIONS[S.QUEUED] == {S.PREPARING, S.CANCELLED}


def test_terminal_states_are_final_and_needs_cleanup_is_not_terminal():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
    assert S.NEEDS_CLEANUP not in TERMINAL_STATES
    assert ALLOWED_TRANSITIONS[S.NEEDS_CLEANUP] == TERMINAL_STATES  # it can only settle into a verdict


def test_every_state_is_reachable_from_queued_and_failure_paths_pass_through_finalizing():
    seen, frontier = {S.QUEUED}, [S.QUEUED]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(S)
    # a failed or cancelled run can only be reached via FINALIZING (so cleanup/evidence happen) or via NEEDS_CLEANUP
    for state in (S.FAILED, S.NEEDS_ATTENTION, S.SUCCEEDED):
        sources = {src for src, targets in ALLOWED_TRANSITIONS.items() if state in targets}
        assert sources <= {S.FINALIZING, S.NEEDS_CLEANUP}
    assert {src for src, t in ALLOWED_TRANSITIONS.items() if S.CANCELLED in t} == {
        S.QUEUED,
        S.FINALIZING,
        S.NEEDS_CLEANUP,
    }


@pytest.mark.parametrize(
    "a,b",
    [
        (S.SUCCEEDED, S.FAILED),
        (S.QUEUED, S.RESTORING),
        (S.FAILED, S.QUEUED),
        (S.RESTORING, S.SUCCEEDED),
        (S.VALIDATING, S.RESTORING),
    ],
)
def test_illegal_transitions_raise(a, b):
    with pytest.raises(InvalidTransition):
        assert_transition(a, b)


def test_slot_and_active_sets_are_consistent_with_the_database_index():
    assert ACTIVE_STATES | {S.QUEUED} == SLOT_STATES and S.NEEDS_CLEANUP not in SLOT_STATES
    from afterstate.db.models import RecoveryRun

    index = next(i for i in RecoveryRun.__table__.indexes if i.name == "uq_runs_one_slot_per_project")
    predicate = str(index.dialect_options["postgresql"]["where"])
    assert {s.value for s in SLOT_STATES} == {
        tok.strip("', ") for tok in predicate.replace("state IN (", "").rstrip(")").split(",")
    }


# ---------------------------------------------------------------------------- verdicts ----------
def chk(outcome: Outcome, required: bool = True, name: str = "c") -> CheckResult:
    return CheckResult("k", name, "data", outcome, required, "msg")


def verdict(checks, *, failure=None, cancelled=False, objectives=()):
    return decide_verdict(
        cancelled=cancelled,
        execution_failure=failure,
        execution_failure_message="boom" if failure else None,
        checks=list(checks),
        objectives=list(objectives),
    )


def test_success_requires_all_required_checks_to_pass():
    assert verdict([chk(Outcome.PASS)] * 3).state is S.SUCCEEDED


@pytest.mark.parametrize(
    "outcome,state,klass",
    [
        (Outcome.FAIL, S.FAILED, FailureClass.VALIDATION_FAILED),
        (Outcome.SKIPPED, S.NEEDS_ATTENTION, FailureClass.VALIDATION_INCONCLUSIVE),
        (Outcome.INCONCLUSIVE, S.NEEDS_ATTENTION, FailureClass.VALIDATION_INCONCLUSIVE),
    ],
)
def test_a_required_check_that_is_not_a_pass_is_never_success(outcome, state, klass):
    v = verdict([chk(Outcome.PASS), chk(outcome)])
    assert v.state is state and v.failure_class is klass


def test_optional_checks_only_produce_warnings():
    v = verdict([chk(Outcome.PASS), chk(Outcome.FAIL, required=False, name="nice to have")])
    assert v.state is S.SUCCEEDED and v.warnings and "nice to have" in v.warnings[0]


def test_failure_outranks_inconclusive_and_execution_failure_outranks_checks():
    assert verdict([chk(Outcome.FAIL), chk(Outcome.INCONCLUSIVE)]).state is S.FAILED
    v = verdict([chk(Outcome.PASS)], failure=FailureClass.RESTORE_TIMEOUT)
    assert v.state is S.FAILED and v.failure_class is FailureClass.RESTORE_TIMEOUT
    assert verdict([chk(Outcome.FAIL)], cancelled=True).state is S.CANCELLED


def obj(status):
    return ObjectiveResult("RTO", status, 10, 5.0, "d")


def test_objectives_gate_success_only_when_missed_or_unmeasurable():
    ok = [chk(Outcome.PASS)]
    assert verdict(ok, objectives=[obj(ObjectiveStatus.MET), obj(ObjectiveStatus.NOT_CONFIGURED)]).state is S.SUCCEEDED
    assert verdict(ok, objectives=[obj(ObjectiveStatus.MISSED)]).failure_class is FailureClass.OBJECTIVE_MISSED
    assert verdict(ok, objectives=[obj(ObjectiveStatus.UNKNOWN)]).failure_class is FailureClass.OBJECTIVE_UNKNOWN


def test_property_success_implies_everything_required_passed_and_nothing_missed():
    """Exhaustive over every combination of up to three required checks and both objectives."""
    outcomes, statuses = list(Outcome), list(ObjectiveStatus)
    for n in range(0, 4):
        for combo in itertools.product(outcomes, repeat=n):
            for o1, o2 in itertools.product(statuses, repeat=2):
                v = verdict([chk(x) for x in combo], objectives=[obj(o1), obj(o2)])
                if v.state is S.SUCCEEDED:
                    assert all(x is Outcome.PASS for x in combo)
                    assert not {o1, o2} & {ObjectiveStatus.MISSED, ObjectiveStatus.UNKNOWN}


# --------------------------------------------------------------------------- objectives ---------
def test_rto():
    assert evaluate_rto(None, 5).status is ObjectiveStatus.NOT_CONFIGURED
    assert evaluate_rto(60, 59.9).status is ObjectiveStatus.MET and evaluate_rto(60, 60.0).status is ObjectiveStatus.MET
    assert evaluate_rto(60, 60.1).status is ObjectiveStatus.MISSED
    assert evaluate_rto(60, None).status is ObjectiveStatus.UNKNOWN


def test_rpo_is_never_fabricated():
    t0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    met = evaluate_rpo(
        3600, test_started_at=t0, latest_recoverable_at=t0 - timedelta(minutes=30), backup_created_at=None
    )
    assert met.status is ObjectiveStatus.MET and met.measured_seconds == 1800 and "probe" in (met.basis or "")
    assert (
        evaluate_rpo(
            3600, test_started_at=t0, latest_recoverable_at=t0 - timedelta(hours=2), backup_created_at=None
        ).status
        is ObjectiveStatus.MISSED
    )
    # a backup older than the target PROVES the miss (data written since is lost) ...
    assert (
        evaluate_rpo(
            3600, test_started_at=t0, latest_recoverable_at=None, backup_created_at=t0 - timedelta(hours=5)
        ).status
        is ObjectiveStatus.MISSED
    )
    # ... but a young backup cannot prove compliance without evidence about the data
    young = evaluate_rpo(
        3600, test_started_at=t0, latest_recoverable_at=None, backup_created_at=t0 - timedelta(minutes=5)
    )
    assert young.status is ObjectiveStatus.UNKNOWN
    assert (
        evaluate_rpo(3600, test_started_at=t0, latest_recoverable_at=None, backup_created_at=None).status
        is ObjectiveStatus.UNKNOWN
    )
    assert (
        evaluate_rpo(None, test_started_at=t0, latest_recoverable_at=None, backup_created_at=None).status
        is ObjectiveStatus.NOT_CONFIGURED
    )


# ----------------------------------------------------------------------- classification ---------
@pytest.mark.parametrize(
    "kwargs,klass",
    [
        ({"returncode": None, "stderr": "", "timed_out": True}, FailureClass.RESTORE_TIMEOUT),
        ({"returncode": -9, "stderr": "", "limit_exceeded": True}, FailureClass.RESOURCE_LIMIT),
        ({"returncode": 139, "stderr": ""}, FailureClass.ARTIFACT_CORRUPT),
        ({"returncode": -11, "stderr": ""}, FailureClass.ARTIFACT_CORRUPT),
        (
            {"returncode": 1, "stderr": "pg_restore: error: could not read from input file: end of file"},
            FailureClass.ARTIFACT_CORRUPT,
        ),
        (
            {"returncode": 1, "stderr": "pg_restore: error: could not uncompress data: incorrect data check"},
            FailureClass.ARTIFACT_CORRUPT,
        ),
        (
            {"returncode": 1, "stderr": "pg_restore: error: unsupported version (1.16) in file header"},
            FailureClass.VERSION_INCOMPATIBLE,
        ),
        (
            {
                "returncode": 1,
                "stderr": 'connection to server on socket "/x/.s.PGSQL.5432" failed: No such file or directory',
            },
            FailureClass.ENVIRONMENT_UNAVAILABLE,
        ),
        (
            {"returncode": 1, "stderr": 'ERROR: permission denied to create extension "dblink"'},
            FailureClass.RESTORE_FAILED,
        ),
        ({"returncode": 1, "stderr": 'ERROR: relation "x" already exists'}, FailureClass.RESTORE_FAILED),
        ({"returncode": 1, "stderr": "", "cancelled": True}, FailureClass.CANCELLED),
    ],
)
def test_failure_classification(kwargs, klass):
    assert classify_tool_failure(**kwargs)[0] is klass


def test_privilege_failures_are_explained():
    assert "unprivileged" in classify_tool_failure(returncode=1, stderr="permission denied to create extension")[1]
