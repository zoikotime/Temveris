"""
Canonical workweek membership.
==============================

EVAL-WEEK, under owner ruling 3 and the "EXPECTED DAY SET" constraint.

HOW THE SEVEN CALENDAR DATES ARE DERIVED
----------------------------------------
The persisted ``workweek_key`` **is** the local workweek START DATE, ISO
formatted — ``"2026-08-31"`` for a Monday-start organization, ``"2026-08-30"``
for a Sunday-start one in the same physical week. That format is fixed by
owner ruling W4-3 and produced by exactly one function,
``work_ledger.workweek.canonical_workweek_key``.

So the week's dates are the start date plus zero through six:

    date.fromisoformat(workweek_key) + timedelta(days=0..6)

Nothing is invented. No workweek definition is introduced here, no ISO calendar
week is used, and — critically — ``week_start_date()`` is NOT called: that reads
CURRENT configuration, which is right at seal time and wrong for re-deriving a
past week. The stored key is the historical anchor (W4-1, W4-5), so parsing it
is the only derivation that cannot drift when an organization changes its
week-start day.

Pure date arithmetic, so a DST transition inside the week cannot move a
boundary. GV-11's "169 hours" is an asserted property of such a week, not an
input to identity — a 25-hour day is still one calendar date.

WHY THE LEDGER DECIDES MEMBERSHIP
---------------------------------
Per the ruling's LEDGER VS ECODAY RECORD distinction: the work ledger
establishes canonical workday membership and the workweek key; the day record
establishes the evaluated state and contribution. So membership is the set of
CURRENT ledger rows carrying this key — ``superseded_by IS NULL`` — and never
the caller's opinion.

Reading current rows only is what makes a correction behave: ``supersede_day()``
writes a new version and marks the old one superseded, so exactly one row per
day is ever current and none is in two places at once.

The new version **inherits** the superseded row's ``workweek_key`` rather than
re-deriving one (owner Ruling 4). This module previously documented the
opposite — a correction adopting the current week-start definition — which
meant a day corrected after a boundary change silently left the week it had
been evaluated in and joined another, and the week it left lost a day it had
already counted. §12.4 forbids that: "Historical workweeks remain readable and
reproducible indefinitely, and change only by the addition of correction
events, never by mutation." A correction is an addition to the lineage, so it
carries the lineage's identity.

The seven derived dates are not used to manufacture membership — a date with no
ledger row is not conjured into one. They are used to check that every row's
``work_date`` really lies in the week its key claims, which is the only way a
mis-keyed row can be detected rather than silently folded into the wrong week.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.eval_day import EcoDayRecord
from app.work_ledger import WorkDayLedger

#: A workweek is seven calendar dates. Not a duration: a DST week is 169 or 167
#: hours long and still spans seven dates.
DAYS_IN_WORKWEEK = 7


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def canonical_dates(workweek_key: str) -> Optional[Tuple[date, ...]]:
    """The seven calendar dates a workweek key denotes, or ``None``.

    ``None`` means the key is not the canonical ``YYYY-MM-DD`` start date. It is
    refused rather than guessed: the key is the week's identity, so a key
    nobody can parse identifies no week.

    THE VALIDATION IS STRICT, AND HAS TO BE
    ---------------------------------------
    ``date.fromisoformat`` is lenient about ISO forms. On Python 3.11+ it
    accepts ISO WEEK strings, so the legacy ``"2026-W36"`` format — the very
    notation W4-3 replaced — parses happily to 2026-08-31, a MONDAY. For a
    Sunday-start organization that is silently the wrong week, which is exactly
    the drift the start-date format exists to remove. ``"20260831"`` is
    accepted by the parser too.

    So the shape is checked before parsing and the result is round-tripped: a
    key is canonical only if it reads back as itself.
    """
    if not isinstance(workweek_key, str):
        return None
    candidate = workweek_key.strip()
    # Exactly YYYY-MM-DD. Rejects ISO week and ordinal forms, basic-format
    # digits, and anything carrying a time component.
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return None
    try:
        start = date.fromisoformat(candidate)
    except (ValueError, TypeError):
        return None
    if start.isoformat() != candidate:
        return None
    return tuple(start + timedelta(days=offset)
                 for offset in range(DAYS_IN_WORKWEEK))


def current_ledger_days(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str
) -> List[WorkDayLedger]:
    """The current ledger rows carrying this key, ordered by date.

    ``superseded_by IS NULL`` selects the current version of each day —
    corrections supersede rather than mutate, so a superseded row is history and
    must not be evaluated as though it were still in force.

    Uses the ``(organization_id, workweek_key)`` index that Step 1 already
    declares for exactly this read.
    """
    return list(db.execute(
        select(WorkDayLedger)
        .where(
            WorkDayLedger.organization_id == organization_id,
            WorkDayLedger.user_id == user_id,
            WorkDayLedger.workweek_key == workweek_key,
            WorkDayLedger.superseded_by.is_(None),
        )
        .order_by(WorkDayLedger.work_date, WorkDayLedger.id)
    ).scalars().all())


def day_records_for(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    ledger_days: Sequence[WorkDayLedger],
) -> Dict[Tuple[date, int], List[EcoDayRecord]]:
    """Every ``EcoDayRecord`` matching one of these ledger days.

    Keyed by ``(work_date, ledger_version)``, which is the part of the snapshot
    identity a ledger row fixes. ``policy_version``, ``config_version`` and the
    leave input key are deliberately NOT part of the key: this reader must be
    able to SEE more than one candidate in order to refuse the ambiguity rather
    than pick one.

    ONLY AUTHORITATIVE RECORDS (Owner Ruling 2, L)
    ----------------------------------------------
    ``superseded_by IS NULL`` — the same guard ``current_ledger_days`` already
    applies to ledger rows. This is a filter to the single row the WRITER
    marked authoritative, not a selection among candidates: EVAL-WEEK resolves
    no leave, reads no ``LeaveRequest.status``, resolves no configuration and
    compares no snapshots. Everything it needs arrives already resolved on the
    row.

    Without it, an ordinary re-evaluation — leave captured after the fact, a
    configuration correction — would leave two rows for one ledger version and
    every corrected day would report ``DAY_RECORD_AMBIGUOUS``. With it, that
    outcome means what it should: the one-authoritative-row invariant is broken.

    Returns lists, so "none" and "more than one" are distinguishable.
    """
    if not ledger_days:
        return {}

    dates = {_as_date(day.work_date) for day in ledger_days}
    versions = {int(day.ledger_version) for day in ledger_days}

    rows = db.execute(
        select(EcoDayRecord).where(
            EcoDayRecord.organization_id == organization_id,
            EcoDayRecord.user_id == user_id,
            EcoDayRecord.work_date.in_(sorted(dates)),
            EcoDayRecord.ledger_version.in_(sorted(versions)),
            EcoDayRecord.superseded_by.is_(None),
        )
        .order_by(EcoDayRecord.work_date, EcoDayRecord.id)
    ).scalars().all()

    out: Dict[Tuple[date, int], List[EcoDayRecord]] = {}
    for row in rows:
        out.setdefault(
            (_as_date(row.work_date), int(row.ledger_version)), []
        ).append(row)
    return out


__all__ = [
    "DAYS_IN_WORKWEEK",
    "canonical_dates",
    "current_ledger_days",
    "day_records_for",
]
