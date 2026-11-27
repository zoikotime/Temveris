"""
Shared datetime helpers for emitting timestamps to JSON.

Why this module exists:
  After the timezone-aware migration (commit 9573a5f) every datetime in the
  DB and in memory is tz-aware UTC. The pre-existing serialization pattern
  `dt.isoformat() + "Z"` then produced strings like
      "2026-05-05T08:29:12.216088+00:00Z"
  which is NOT valid ISO 8601 — `new Date()` in V8 returns Invalid Date
  and every date-derived UI value (timer, "X mins ago", Active %) ends up
  as NaN / "Invalid Date" on the frontend.

  iso_z() is the single canonical way to render a Python datetime as the
  trailing-Z form the frontend already expects. Naive datetimes are
  treated as UTC (matches how the codebase used datetime.utcnow() before
  the migration).
"""

from datetime import datetime, timezone
from typing import Optional


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    """Return an ISO 8601 string ending in 'Z' (no '+00:00' offset).

    - tz-aware datetimes are converted to UTC, then the '+00:00' suffix
      that .isoformat() produces is replaced with 'Z'.
    - naive datetimes are assumed to already be UTC (they were, under the
      pre-migration utcnow() regime) and just get a 'Z' appended.
    - None passes through as None so callers can keep their `if dt else None`
      guards if they want, or rely on this default.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive — assume UTC (legacy rows/values from before the migration).
        return dt.isoformat() + "Z"
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
