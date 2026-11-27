"""
Arrival / departure classification — PURE.
==========================================
No database, no models, no imports from the resolver. Given a resolved schedule
and a clock-in instant, decide what the arrival is called. Kept separate from
``resolver.py`` so the decision table is trivially testable and has exactly one
implementation.

``arrival_status`` is a NEW, ORTHOGONAL field. It does not replace the existing
presence status (``Working`` / ``On Break`` / ``Stopped`` / ``Yet to Start`` /
``Absent`` / ``On Leave``) computed in ``employees/router.py``. Those keep their
exact meaning; this answers a different question — *was the arrival on time?*

Nothing calls this yet (P5 is pure).

Boundaries
----------
Measured in minutes from the shift start, inclusive lower / exclusive upper:

    delta <= present_until          -> PRESENT     (present_until = max(grace, late_after))
    delta <  very_late_after        -> LATE
    otherwise                       -> VERY_LATE

With the approved defaults (start 09:00, grace 15, late_after 15, very_late 120)
this gives the required behaviour exactly:

    09:08 -> PRESENT     (8 <= 15)
    09:20 -> LATE        (20 > 15, 20 < 120)
    11:00 -> VERY_LATE   (delta 120, NOT < 120)

The upper bounds are exclusive precisely so 11:00 lands on VERY_LATE rather
than LATE. ``present_until`` is a max() so an org can widen the late threshold
beyond the grace window without the two settings fighting.

Absence
-------
Absence is measured the same way — from the shift start, not from a fixed hour
of the day. See ``absence_cutoff``. The invariant is stronger than "not before
the shift start": nobody is ever ABSENT while a clock-in at that same instant
would still have been called PRESENT, LATE or VERY_LATE. Absence is the rung
after the arrival ladder, so it cannot pre-empt any rung on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class ArrivalStatus(str, Enum):
    """str-valued so it serializes to JSON as-is."""
    NOT_APPLICABLE = "not_applicable"   # not a working day for this employee
    AWAITING       = "awaiting"         # no clock-in yet, still within grace
    LATE_PENDING   = "late_pending"     # no clock-in, past grace, before the cutoff
    PRESENT        = "present"
    LATE           = "late"
    VERY_LATE      = "very_late"
    ABSENT         = "absent"           # no clock-in, past the absent cutoff


class DepartureStatus(str, Enum):
    EARLY    = "early"
    COMPLETE = "complete"
    OVERTIME = "overtime"


@dataclass(frozen=True)
class ArrivalResult:
    status: ArrivalStatus
    # Minutes past shift start. None unless the arrival was actually late, so a
    # caller can render "Late by X" without re-deriving the threshold.
    late_minutes: Optional[int] = None


@dataclass(frozen=True)
class DepartureResult:
    status: DepartureStatus
    shortfall_seconds: int = 0   # expected - worked, when short
    overtime_seconds: int = 0    # worked - expected, when over


def _present_until(sched) -> int:
    """Minutes after shift start that still count as on time."""
    grace = max(0, int(sched.grace_minutes or 0))
    late_after = sched.late_after_minutes
    if late_after is None:
        return grace
    return max(grace, max(0, int(late_after)))


def _absence_floor_minutes(sched) -> int:
    """Minutes after shift start before a no-show may be called Absent.

    Used ONLY by the fallback in ``absence_cutoff`` — when some tier configured
    ``absent_after_minutes`` that number is the rule and this is not consulted.
    Two floors apply, whichever is later:

      * ``_present_until`` — a clock-in anywhere inside this window classifies
        PRESENT, so calling the same minute Absent would contradict the arrival
        table in this very module.
      * ``very_late_after_minutes`` — the last rung of the arrival ladder
        (PRESENT -> LATE -> VERY_LATE). Absent is the rung *after* it and can
        never arrive first, or LATE and VERY_LATE become unreachable states for
        that employee.

    Both are read defensively. ``absence_cutoff`` is public and ``sched`` is
    duck-typed (see ``classify_arrival``), so a caller may pass an object whose
    value is missing, None, negative or not a number — and the resolver's own
    ``very_late = _int_or(..., 120) or 120`` lets a NEGATIVE row through, which
    would otherwise pull the cutoff *earlier* than the shift start. Anything
    unusable degrades to 0, reproducing the previous behaviour rather than
    inventing a threshold nobody chose.
    """
    floor = 0
    try:
        floor = max(floor, _present_until(sched))
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        floor = max(floor, int(getattr(sched, "very_late_after_minutes", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        pass
    return max(0, floor)


def absence_cutoff(sched, start_dt: Optional[datetime],
                   now_local: datetime) -> datetime:
    """The instant a no-show becomes Absent, in the schedule's local frame.

    Two rules, and which one applies depends on whether the employee has a
    scheduled shift start at all:

      * **Shift start known** — ``start + absent_after_minutes``. This is the
        rule an organization configures, and it is the only one that works for
        an afternoon shift: an absolute hour cannot serve a 09:00 and a 16:30
        employee in the same workspace.
      * **No shift start** — ``absent_after_hour`` on the local day, unchanged.
        Most existing employees have no start time; for them "how long after
        the shift" has no meaning and the cutoff hour is still the only answer.

    When a start exists but no organization has configured the offset, the
    cutoff hour still decides — but floored at the end of the shift's own
    arrival window (``_absence_floor_minutes``), because the one thing that is
    never right is calling somebody absent before their shift has had a chance
    to begin. That floor is what a 16:30 employee was missing when a global
    noon cutoff marked eleven of them absent at 12:02.

    The floor is a WINDOW, not the bare start instant. It used to be
    ``max(start_dt, hour_cutoff)``, which stopped the cutoff landing before the
    shift and then landed it exactly ON the shift: a 13:00 employee was Absent
    at 13:00:00 with zero grace, and because ``classify_arrival`` tests ABSENT
    before the grace branch, their ``grace_minutes`` was never read at all.
    Every shift starting at or after the cutoff hour had the same collapse.

    The fallback can therefore only ever move LATER than it used to, never
    earlier — ``start + max(0, floor) >= start`` — so no organization can begin
    receiving an absence alert sooner than it does today.
    """
    hour_cutoff = now_local.replace(
        hour=int(sched.absent_after_hour), minute=0, second=0, microsecond=0,
    )
    if start_dt is None:
        return hour_cutoff
    minutes = getattr(sched, "absent_after_minutes", None)
    if minutes is not None:
        return start_dt + timedelta(minutes=max(0, int(minutes)))
    floored_start = start_dt + timedelta(minutes=_absence_floor_minutes(sched))
    return max(floored_start, hour_cutoff)


def classify_arrival(
    clock_in_local: Optional[datetime],
    sched,
    now_local: datetime,
) -> ArrivalResult:
    """Classify an arrival against a resolved schedule.

    ``clock_in_local`` and ``now_local`` must both be in the schedule's local
    timezone (see ``app._tz``); only wall-clock differences are used, so mixing
    frames would silently skew the result. ``clock_in_local`` is ``None`` when
    the employee has not clocked in.

    ``sched`` is duck-typed — anything exposing ``is_working_day``,
    ``start_local``, ``grace_minutes``, ``late_after_minutes``,
    ``very_late_after_minutes``, ``absent_after_hour`` and (optionally)
    ``absent_after_minutes``. ``EffectiveSchedule`` satisfies it.
    """
    if not sched.is_working_day:
        return ArrivalResult(ArrivalStatus.NOT_APPLICABLE)

    start = sched.start_local
    if start is None:
        # No scheduled start: nothing to be late against. An absent cutoff still
        # applies so a no-show on a working day is not silently "awaiting".
        if clock_in_local is not None:
            return ArrivalResult(ArrivalStatus.PRESENT)
        if now_local >= absence_cutoff(sched, None, now_local):
            return ArrivalResult(ArrivalStatus.ABSENT)
        return ArrivalResult(ArrivalStatus.AWAITING)

    # Shift start as an instant on the same local day as `now_local`.
    start_dt = now_local.replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0,
    )

    if clock_in_local is None:
        if now_local >= absence_cutoff(sched, start_dt, now_local):
            return ArrivalResult(ArrivalStatus.ABSENT)
        # Past the on-time window with still no clock-in. Distinguished from
        # AWAITING because the admin dashboard treats the two differently: this
        # one surfaces in Requires Attention, "still within grace" does not.
        # Collapsing them would have silently dropped that bucket in P9.
        pending_min = int((now_local - start_dt).total_seconds() // 60)
        if pending_min > _present_until(sched):
            return ArrivalResult(ArrivalStatus.LATE_PENDING, pending_min)
        return ArrivalResult(ArrivalStatus.AWAITING)

    delta_min = int((clock_in_local - start_dt).total_seconds() // 60)

    # Early or on time — including clocking in before the shift starts.
    if delta_min <= _present_until(sched):
        return ArrivalResult(ArrivalStatus.PRESENT)

    late_minutes = delta_min
    if delta_min < int(sched.very_late_after_minutes):
        return ArrivalResult(ArrivalStatus.LATE, late_minutes)
    return ArrivalResult(ArrivalStatus.VERY_LATE, late_minutes)


def classify_departure(worked_seconds: int, sched) -> DepartureResult:
    """Compare actual worked time against the expected daily hours.

    ``worked_seconds`` must already exclude breaks and offline time — pass the
    value from ``risk.router._session_worked_secs``, which is the canonical
    worked-time calculation. This function must never re-derive it.
    """
    worked = max(0, int(worked_seconds or 0))
    expected = max(0, int(sched.expected_work_seconds or 0))
    if worked < expected:
        return DepartureResult(DepartureStatus.EARLY, shortfall_seconds=expected - worked)
    if worked > expected:
        return DepartureResult(DepartureStatus.OVERTIME, overtime_seconds=worked - expected)
    return DepartureResult(DepartureStatus.COMPLETE)
