"""Explicit run state machine.

    QUEUED -> PREPARING -> RESTORING -> VALIDATING -> FINALIZING -> SUCCEEDED
                                                                 -> FAILED
                                                                 -> NEEDS_ATTENTION
                                                                 -> CANCELLED
    any in-flight state -> NEEDS_CLEANUP -> (settled state)

Every failure path, including cancellation, passes through FINALIZING so that evidence is written
and temporary resources are cleaned up before the verdict becomes visible. NEEDS_CLEANUP is only
entered when cleanup could not be confirmed; the run remembers the state it will settle into.
"""

from __future__ import annotations

from afterstate.domain.enums import RunState as S

ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.QUEUED: frozenset({S.PREPARING, S.CANCELLED}),
    S.PREPARING: frozenset({S.RESTORING, S.FINALIZING, S.NEEDS_CLEANUP}),
    S.RESTORING: frozenset({S.VALIDATING, S.FINALIZING, S.NEEDS_CLEANUP}),
    S.VALIDATING: frozenset({S.FINALIZING, S.NEEDS_CLEANUP}),
    S.FINALIZING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.NEEDS_ATTENTION, S.NEEDS_CLEANUP}),
    S.NEEDS_CLEANUP: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.NEEDS_ATTENTION}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.NEEDS_ATTENTION: frozenset(),
}

#: States in which a worker owns the run and holds a lease.
ACTIVE_STATES: frozenset[S] = frozenset({S.PREPARING, S.RESTORING, S.VALIDATING, S.FINALIZING})
#: States that occupy the project's single execution slot.
SLOT_STATES: frozenset[S] = frozenset({S.QUEUED}) | ACTIVE_STATES
#: Settled verdicts. NEEDS_CLEANUP is deliberately not terminal.
TERMINAL_STATES: frozenset[S] = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.NEEDS_ATTENTION})
#: States a run may legitimately settle into after cleanup.
SETTLED_STATES = TERMINAL_STATES


class InvalidTransition(Exception):  # noqa: N818 - domain name reads better without the suffix
    def __init__(self, source: S, target: S) -> None:
        super().__init__(f"illegal run transition {source} -> {target}")
        self.source = source
        self.target = target


def can_transition(source: S, target: S) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


def assert_transition(source: S, target: S) -> None:
    if not can_transition(source, target):
        raise InvalidTransition(source, target)


def is_terminal(state: S) -> bool:
    return state in TERMINAL_STATES
