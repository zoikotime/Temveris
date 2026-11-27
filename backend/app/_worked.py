"""
The one definition of "how long did this session work".

Three modules each had their own answer, and they disagreed:

  * ``risk/router._session_worked_secs`` — always recomputed
    ``(clock_out or now) - clock_in - break_seconds - offline_seconds``.
  * ``reports/router._session_seconds`` — preferred the stored
    ``total_seconds``, and when it fell back to recomputing it forgot
    ``offline_seconds``, so a session with a connectivity gap counted MORE
    hours in the report than in the risk check.
  * ``payroll/service.get_worked_hours`` — summed raw ``total_seconds`` and
    silently skipped sessions that were still open.

The disagreement is not academic: the risk alert fires on one number, the
attendance report shows a second, and the payslip is computed from a third.

The rule, once:

    worked = (clock_out or now) - clock_in - break_seconds - offline_seconds

with the stored ``total_seconds`` preferred for a CLOSED session, because
clock-out writes exactly that expression and later admin corrections are
applied to the stored value rather than to the timestamps.

Manual time adjustments (``app/_time_adjustments.py``) are deliberately NOT
folded in here: they are credited per employee per DAY, not per session, so
they belong to whoever is aggregating days. This module answers about one
session.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def session_worked_seconds(session, now: Optional[datetime] = None) -> int:
    """Worked seconds for one ClockSession, open or closed.

    ``now`` anchors an in-progress session; defaults to the current instant.
    Never negative, and never raises on a half-written row.
    """
    start = _utc(getattr(session, "clock_in_time", None))
    if start is None:
        return 0

    end = _utc(getattr(session, "clock_out_time", None))
    if end is not None:
        stored = getattr(session, "total_seconds", None)
        if stored and stored > 0:
            return int(stored)
    else:
        end = _utc(now) or datetime.now(timezone.utc)

    try:
        elapsed = int((end - start).total_seconds())
    except TypeError:      # mixed naive/aware that slipped past _utc
        return 0
    breaks = int(getattr(session, "break_seconds", 0) or 0)
    offline = int(getattr(session, "offline_seconds", 0) or 0)
    return max(0, elapsed - breaks - offline)
