"""
The ECO day read contract.
==========================

    read_eco_day(db, caller=..., user_id=..., work_date=...)
        -> EcoDayView | ViewAuthorityDecision

One authoritative read of one Worker-day's recorded ECO state, together with
the canonical ledger evidence that explains it. Authorization first, evidence
second, and no copy at all.

AUTHORIZATION IS NOT DUPLICATED
-------------------------------
Every call goes through ``require_eco_view_authority`` (owner ruling B2) before
anything is read. A denial is RETURNED, not raised, and it is returned as the
``ViewAuthorityDecision`` itself — so a caller cannot mistake it for an empty
result, and the reason travels with it. There is no second authorization rule
here, no role check, and no way to reach the query without passing the gate:
the read happens after the guard in the same function, not in a sibling one a
future caller could reach directly.

HISTORICAL, NOT CURRENT
-----------------------
The ledger row is fetched by the record's OWN ``ledger_version``, never by
``get_current_ledger``. §12.4 requires historical workweeks to "remain readable
and reproducible indefinitely", and reading the current version would make a
correction retroactively re-describe an older record. A superseded record
therefore keeps reporting the evidence it was actually evaluated against.

Nothing is recomputed. No requirement is resolved, no schedule is consulted, no
classification is re-run, no clock is read. Every value comes off an immutable
row, so the same read under changed configuration returns the same answer.

THE §24.6 / §24.7 DISTINCTION IS BORROWED, NOT RESTATED
--------------------------------------------------------
``has_unverified_exit`` is answered by EVAL-DAY's own §19 step-6 predicate.
Importing it is deliberate: restating the rule here would create a second
implementation of the boundary owner ruling B3 (Option A) exists to keep
singular, and the two could drift. A test asserts the two agree.

WHAT THIS IS NOT
----------------
No HTTP surface, no route, no serializer. No rendered status label and no
explanatory sentence — §31 registers neither, so there is nothing authoritative
to render and this contract does not pretend otherwise. No notification, no
recipient, no reviewer, no audience. No write of any kind.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.eco_view_authority import (
    ViewAuthorityDecision,
    require_eco_view_authority,
)
from app.eval_day.models import EcoDayRecord
from app.work_ledger.models import WorkDayLedger, WorkStateInterval

from .outcomes import EcoDayView

#: The §19 step-6 predicate, owned by EVAL-DAY. Imported rather than restated —
#: see the module docstring. Private by name because EVAL-DAY owns the DECISION;
#: this module only reports what that decision would see.
from app.eval_day.evaluator import _has_unverified_exit  # noqa: E402


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _int_or_none(value) -> Optional[int]:
    """Preserve NULL as NULL. A deferred day's measurements are absent, and
    coercing them to 0 would turn "nothing was measured" into "zero"."""
    return None if value is None else int(value)


def _authoritative_record(
    db: Session, *, organization_id: int, user_id: int, work_date: date,
) -> Optional[EcoDayRecord]:
    """The record currently in force for this Worker-day.

    ``superseded_by IS NULL`` selects it, exactly as EVAL-DAY's own recording
    path does — one definition of "current", not two. A day with no record at
    all returns None; a day whose records are all superseded is a broken state
    and also returns None rather than silently promoting an old generation.
    """
    return db.execute(
        select(EcoDayRecord)
        .where(
            EcoDayRecord.organization_id == organization_id,
            EcoDayRecord.user_id == user_id,
            EcoDayRecord.work_date == work_date,
            EcoDayRecord.superseded_by.is_(None),
        )
        .order_by(EcoDayRecord.evaluation_generation.desc())
    ).scalars().first()


def _ledger_for(db: Session, record: EcoDayRecord) -> Optional[WorkDayLedger]:
    """The exact ledger version this record was evaluated against.

    Selected on the natural key ``(organization_id, user_id, work_date,
    ledger_version)``, which the ledger enforces as UNIQUE — so this resolves to
    at most one row and never depends on which version happens to be current.

    THE TWO TABLES SPELL ``work_date`` DIFFERENTLY. ``eco_day_record.work_date``
    is a DATE; ``work_day_ledger.work_date`` is the midnight DATETIME the ledger
    normalises every calendar label to. Comparing the record's date against the
    ledger column matches NOTHING — silently, returning no row and therefore no
    ledger state at all. The ledger's own ``_as_naive_date`` is used rather than
    a local conversion so there is one definition of that normalisation.
    """
    from app.work_ledger.service import _as_naive_date

    return db.execute(
        select(WorkDayLedger).where(
            WorkDayLedger.organization_id == record.organization_id,
            WorkDayLedger.user_id == record.user_id,
            WorkDayLedger.work_date == _as_naive_date(record.work_date),
            WorkDayLedger.ledger_version == record.ledger_version,
        )
    ).scalars().first()


def _intervals_for(db: Session, ledger: WorkDayLedger) -> List[WorkStateInterval]:
    return list(db.execute(
        select(WorkStateInterval)
        .where(WorkStateInterval.ledger_id == ledger.id)
        .order_by(WorkStateInterval.start_at_utc, WorkStateInterval.seq)
    ).scalars().all())


def read_eco_day(
    db: Session,
    *,
    caller,
    user_id: int,
    work_date,
) -> Union[EcoDayView, ViewAuthorityDecision, None]:
    """Read one Worker-day's ECO state, or return the denial that prevented it.

    Returns:
        * ``ViewAuthorityDecision`` — the caller may not read this record. The
          decision is returned rather than an empty view, so "denied" and "no
          such day" can never be confused for one another.
        * ``None`` — authorized, but no ECO record is in force for that day.
        * ``EcoDayView`` — authorized, and here is the recorded state.

    There is no ``organization_id`` parameter: the tenant comes from the caller,
    through the B2 primitive, and nowhere else.
    """
    decision = require_eco_view_authority(
        db, caller=caller, target_user_id=user_id)
    if not decision.is_authorized:
        return decision

    on = _as_date(work_date)
    record = _authoritative_record(
        db,
        organization_id=decision.organization_id,
        user_id=int(user_id),
        work_date=on,
    )
    if record is None:
        return None

    ledger = _ledger_for(db, record)
    intervals = _intervals_for(db, ledger) if ledger is not None else []

    return EcoDayView(
        organization_id=int(record.organization_id),
        user_id=int(record.user_id),
        work_date=_as_date(record.work_date),
        evaluation_state=record.state,
        evaluation_generation=int(record.evaluation_generation),
        shortfall_computed=bool(record.shortfall_computed),
        contributes_to_accumulator_minutes=(
            None if record.contributes_to_accumulator_minutes is None
            else int(record.contributes_to_accumulator_minutes)),
        ledger_version=int(record.ledger_version),
        ledger_state=(ledger.state if ledger is not None else None),
        # Fail closed: with no ledger row there is no evidence of an unverified
        # exit, and absence of evidence is never asserted as evidence.
        has_unverified_exit=(
            bool(_has_unverified_exit(ledger, intervals))
            if ledger is not None else False),
        integrity_fault_reason=(
            ledger.integrity_fault_reason if ledger is not None else None),
        supersedes_record_id=(
            None if record.supersedes is None else int(record.supersedes)),
        superseded_by_record_id=(
            None if record.superseded_by is None else int(record.superseded_by)),
        # ── Frozen identity and measurements, copied off the record ─────────
        # Owner Rulings B and E: read from the record, never re-resolved from
        # current configuration, and never recomputed. Every value below is a
        # column on the row selected above.
        policy_version=record.policy_version,
        config_version=record.config_version,
        leave_input_snapshot_key=record.leave_input_snapshot_key,
        evaluation_deferral_reason=record.evaluation_deferral_reason,
        classification_snapshot_key=record.classification_snapshot_key,
        required_minutes=_int_or_none(record.required_minutes),
        qualifying_minutes=_int_or_none(record.qualifying_minutes),
        gross_shortfall_minutes=_int_or_none(record.gross_shortfall_minutes),
        exclusion_minutes=_int_or_none(record.exclusion_minutes),
        attributable_minutes=_int_or_none(record.attributable_minutes),
        schedule_variance_minutes=_int_or_none(record.schedule_variance_minutes),
    )


__all__ = ["read_eco_day"]
