"""Durable, timezone-aware recurring recovery tests. Runs inside the worker; needs no browser."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select

from afterstate.db.base import utcnow
from afterstate.db.models import Notification, RecoveryProject, RecoveryRun, Schedule, ScheduleFiring, Workspace
from afterstate.domain.enums import Trigger
from afterstate.domain.state_machine import SLOT_STATES
from afterstate.engine.pipeline import Services
from afterstate.runs import ActiveRunExists, enqueue_run, resolve_artifact

log = logging.getLogger("afterstate.scheduler")
MIN_INTERVAL = timedelta(minutes=5)


class InvalidSchedule(ValueError):
    pass


def validate_schedule(cron_expr: str, tz: str) -> None:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidSchedule(f"unknown time zone '{tz}'") from exc
    if len(cron_expr.split()) != 5 or not croniter.is_valid(cron_expr):
        raise InvalidSchedule("cron expression must have 5 fields (minute hour day-of-month month day-of-week)")
    it = croniter(cron_expr, datetime.now(zone))
    first, second = it.get_next(datetime), it.get_next(datetime)
    if second - first < MIN_INTERVAL:
        raise InvalidSchedule("schedules may not run more often than every 5 minutes")


def next_fire_after(cron_expr: str, tz: str, after: datetime) -> datetime:
    zone = ZoneInfo(tz)
    nxt: datetime = croniter(cron_expr, after.astimezone(zone)).get_next(datetime)
    return nxt.astimezone(UTC)


def _slots_up_to(cron_expr: str, tz: str, first: datetime, now: datetime, cap: int = 500) -> list[datetime]:
    zone = ZoneInfo(tz)
    slots = [first]
    it = croniter(cron_expr, first.astimezone(zone))
    while len(slots) < cap:
        nxt = it.get_next(datetime).astimezone(UTC)
        if nxt > now:
            break
        slots.append(nxt)
    return slots


def tick(svc: Services, now: datetime | None = None) -> int:
    """Fire every due schedule once. Returns the number of runs enqueued."""
    now = now or utcnow()
    fired = 0
    grace = timedelta(seconds=max(60.0, svc.settings.scheduler_tick_seconds * 3))
    with svc.sf() as s, s.begin():
        due = list(
            s.scalars(
                select(Schedule)
                .where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                .order_by(Schedule.next_run_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        )
        for sch in due:
            assert sch.next_run_at is not None
            slots = _slots_up_to(sch.cron_expr, sch.timezone, sch.next_run_at, now)
            latest = slots[-1]
            missed = [x for x in slots if now - x > grace and x != latest]
            if now - latest > grace:
                missed.append(latest)
            on_time = latest if now - latest <= grace else None

            for slot in missed:
                if sch.missed_run_policy == "run_once" and on_time is None and slot == latest:
                    continue  # the latest missed slot is handled as a catch-up run below
                s.add(
                    ScheduleFiring(
                        schedule_id=sch.id,
                        scheduled_for=slot,
                        outcome="skipped_missed",
                        detail="The scheduler was not running at this time.",
                    )
                )
            if missed:
                s.add(
                    Notification(
                        workspace_id=sch.workspace_id,
                        kind="schedule_missed",
                        channel="in_app",
                        title=f"Schedule '{sch.name}' missed {len(missed)} run(s)",
                        body=f"Missed run policy: {sch.missed_run_policy}.",
                        payload={"schedule_id": str(sch.id), "missed": len(missed)},
                        status="delivered",
                        delivered_at=now,
                    )
                )

            slot_to_run = on_time or (latest if (missed and sch.missed_run_policy == "run_once") else None)
            if slot_to_run is not None:
                fired += _fire(svc, s, sch, slot_to_run, catch_up=on_time is None)
            sch.last_fired_at = now
            sch.next_run_at = next_fire_after(sch.cron_expr, sch.timezone, max(now, latest))
    return fired


def _fire(svc: Services, s, sch: Schedule, slot: datetime, *, catch_up: bool) -> int:  # type: ignore[no-untyped-def]
    project = s.get(RecoveryProject, sch.project_id)
    if project is None or project.archived_at is not None:
        s.add(
            ScheduleFiring(
                schedule_id=sch.id,
                scheduled_for=slot,
                outcome="error",
                detail="The project no longer exists or is archived.",
            )
        )
        return 0
    busy = s.scalar(
        select(RecoveryRun.id).where(RecoveryRun.project_id == project.id, RecoveryRun.state.in_(SLOT_STATES)).limit(1)
    )
    if busy:
        s.add(
            ScheduleFiring(
                schedule_id=sch.id,
                scheduled_for=slot,
                outcome="skipped_overlap",
                run_id=busy,
                detail="A run for this project was already queued or in progress.",
            )
        )
        return 0
    artifact = resolve_artifact(s, project)
    if artifact is None:
        s.add(
            ScheduleFiring(
                schedule_id=sch.id,
                scheduled_for=slot,
                outcome="error",
                detail="No backup artifact is available for this project.",
            )
        )
        ws = s.get(Workspace, sch.workspace_id)
        if ws:
            s.add(
                Notification(
                    workspace_id=ws.id,
                    kind="schedule_error",
                    channel="in_app",
                    title=f"Schedule '{sch.name}' could not run",
                    body="No backup artifact is available for the project.",
                    payload={"schedule_id": str(sch.id)},
                    status="delivered",
                    delivered_at=utcnow(),
                )
            )
        return 0
    try:
        run = enqueue_run(
            s,
            svc.settings,
            project=project,
            artifact=artifact,
            trigger=Trigger.SCHEDULE,
            schedule=sch,
            created_by=sch.created_by,
            max_attempts=1 + sch.retry_max_attempts,
            retry_backoff=sch.retry_backoff_seconds,
        )
    except ActiveRunExists:
        s.add(
            ScheduleFiring(
                schedule_id=sch.id,
                scheduled_for=slot,
                outcome="skipped_overlap",
                detail="A run for this project was queued concurrently.",
            )
        )
        return 0
    s.add(
        ScheduleFiring(
            schedule_id=sch.id,
            scheduled_for=slot,
            outcome="enqueued",
            run_id=run.id,
            detail="Catch-up run for a missed slot." if catch_up else "",
        )
    )
    return 1
