"""
The current open workweek start.
================================

The one date ruling H turns on, and the only place in the ECO threshold code
that is allowed to ask what week it is now.

WHY THIS IS NOT IN THE READER
-----------------------------
The reader resolves a threshold *for a workweek that already exists* — its
as-of date is the persisted ``WorkDayLedger.workweek_key``, parsed. It must
never compute a week, because computing one reads CURRENT schedule
configuration and a sealed day's week identity is what was persisted, not what
would be derived today. An AST guard enforces that across the reader package.

Capture asks a genuinely different question: *which workweek is open right
now*, so that an effective date earlier than its start can be refused. That
question can only be answered from current configuration and today's date, so
it lives here, in a separate package, and the reader's guard stays exactly as
strict as it was.

NOTHING IS RE-DERIVED HERE
--------------------------
Both halves come from the repository's canonical helpers:

* ``schedule_policy.week_start_date`` — the organization's own first day of the
  week, the same function the sealed ledger uses to mint a ``workweek_key``. No
  weekday arithmetic, no ISO week numbering, no hard-coded Monday or Sunday.
* ``_tz.local_date`` — today in the organization's own timezone, "the today
  every dashboard bucket, absence cutoff and daily report must agree on". A UTC
  date is not substituted: for an organization far enough east or west, UTC's
  today is somebody else's yesterday, and near a week boundary that would move
  the whole refusal threshold by a day.

No country or timezone inference: ``local_date`` resolves the organization's
configured IANA zone.
"""

from __future__ import annotations

from datetime import date


def current_open_workweek_start(db, *, organization_id) -> date:
    """The start date of the workweek currently in progress for this organization.

    Reads live configuration deliberately — "which week is open now" is a
    question about the present, unlike every other date in ECO evaluation.

    Not deterministic across days, by nature: it moves when the week turns.
    Callers that need a fixed boundary must capture this value once and pass it
    down rather than calling twice.
    """
    # Deferred imports, the idiom this repository already uses to keep the
    # module graph acyclic across packages (see work_ledger.workweek).
    from app._tz import local_date
    from app.schedule_policy import week_start_date

    today = local_date(db, organization_id)
    return week_start_date(db, organization_id, today)


__all__ = ["current_open_workweek_start"]
