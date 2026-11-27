"""
Response annotation — resolved schedule facts for a list of employees.
======================================================================
One call resolves every employee's effective schedule and classifies their
arrival, returning the per-user fields ``/employees/team`` attaches to its
response: ``arrival_status``, ``late_minutes``, ``expected_seconds``,
``shift_start_time``, ``shift_end_time``, ``grace_minutes``, ``is_working_day``
and ``schedule_source``.

This is the ONLY place a server-side arrival status is produced for a response.
The frontend renders ``arrival_status`` directly rather than deriving it, so
"was this arrival on time" has exactly one implementation
(``classify.classify_arrival``) fed by exactly one schedule
(``resolver.resolve_work_schedule``).

Timezone
--------
Classification happens in the ORGANIZATION's configured local time
(``OrgSettings.default_timezone`` via ``app._tz``), not a fixed offset. That is
the whole point: "is 09:20 late" and "is it past the absence cutoff yet" are
wall-clock questions, and the wall clock belongs to the tenant. Users are
grouped by organization so a mixed-tenant caller classifies each group in its
own frame.

History
-------
This file replaces ``schedule_policy/shadow.py``. That module existed to run the
resolver *alongside* a set of hard-coded legacy branches and log where the two
disagreed, gated on ``schedule.shadow_mode_enabled`` / ``schedule.engine_enabled``.
The legacy branches are gone, so there is nothing to shadow: what remains is the
annotation itself.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import _tz

_log = logging.getLogger("zoiko.schedule.annotate")


def arrival_for(sched, clock_in_utc: Optional[datetime],
                now_local: datetime) -> Dict[str, Any]:
    """Classify one arrival in the schedule's own local frame.

    ``now_local`` must already be the organization's wall clock — the caller
    resolves it once per organization rather than per employee.
    """
    try:
        from app.schedule_policy.classify import classify_arrival
        ci = None
        if clock_in_utc is not None:
            aware = (clock_in_utc.replace(tzinfo=timezone.utc)
                     if clock_in_utc.tzinfo is None else clock_in_utc)
            ci = aware.astimezone(now_local.tzinfo)
            # A clock-in from a previous local day is not today's arrival.
            if ci.date() != now_local.date():
                ci = None
        r = classify_arrival(ci, sched, now_local)
        return {"arrival_status": r.status.value, "late_minutes": r.late_minutes}
    except Exception:
        _log.warning("arrival classification failed", exc_info=True)
        return {"arrival_status": None, "late_minutes": None}


def annotate(db: Session, users, on_date: Optional[date] = None,
             clock_in_by_user: Optional[Dict[int, datetime]] = None,
             ) -> Dict[int, Dict[str, Any]]:
    """Resolve and classify every user. Returns ``{user_id: fields}``.

    ``on_date`` defaults to each organization's own local date, so a caller
    sweeping several tenants does not have to pick one calendar day for all of
    them. Pass it explicitly only when annotating a specific historical day.

    ``clock_in_by_user`` maps user id → today's clock-in instant (UTC) so the
    caller passes what it already loaded rather than this module re-querying.

    Returns ``{}`` on catastrophic failure — this must never break its caller.
    """
    clock_in_by_user = clock_in_by_user or {}
    out: Dict[int, Dict[str, Any]] = {}

    by_org: Dict[Any, list] = {}
    for u in users:
        by_org.setdefault(getattr(u, "organization_id", None), []).append(u)

    for org_id, group in by_org.items():
        try:
            now_local = _tz.local_now(db, org_id)
            day = on_date or now_local.date()
            from app.schedule_policy.resolver import resolve_work_schedules
            scheds = resolve_work_schedules(db, group, day)
        except Exception:
            _log.warning("schedule annotate failed org=%s; omitting %d user(s)",
                         org_id, len(group), exc_info=True)
            continue

        for u in group:
            sched = scheds.get(getattr(u, "id", None))
            if sched is None:
                continue
            out[u.id] = {
                "expected_seconds": sched.expected_work_seconds,
                "shift_name": sched.shift_name,
                "shift_start_time": (sched.start_local.strftime("%H:%M")
                                     if sched.start_local else None),
                "shift_end_time": (sched.end_local.strftime("%H:%M")
                                   if sched.end_local else None),
                "grace_minutes": sched.grace_minutes,
                "is_working_day": sched.is_working_day,
                "absent_after_hour": sched.absent_after_hour,
                "timezone": sched.timezone_name,
                "schedule_source": sched.sources.get("start_local"),
                **arrival_for(sched, clock_in_by_user.get(u.id), now_local),
            }
    return out
