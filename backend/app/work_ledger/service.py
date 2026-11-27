"""
Canonical ledger service — sealing, supersession, idempotency, concurrency.
===========================================================================

STEP 1 of the owner-authorised Phase 2 foundation.

WHAT THIS MODULE DOES
---------------------
Writes and reads the canonical sealed interval ledger. Sealing a Worker-day,
superseding a sealed day with a correction, and refusing a day whose evidence
is corrupt.

WHAT IT DOES NOT DO — AND MUST NEVER DO
---------------------------------------
* It does not compute worked time. There is no fold here. The canonical fold
  (Step 9) is the only thing that will read these intervals to answer
  "how much did this Worker work?", and it will be the single implementation.
* It does not classify. Turning sessions, breaks and offline windows into
  labelled intervals is the deterministic classifier (Step 3), which is gated
  on the Legal jurisdictional matrix.
* It does not read `clock_sessions`, `break_sessions` or `offline_intervals`.
  Step 1 is the ledger primitive; wiring is Steps 9–10.
* It imports nothing from `app.active_time`, `models.Activity`,
  `models.ActivityWindow`, `models.BrowserActivity` or `models.AppSession`
  (INV-6, INV-7). This is asserted by an import-graph test.

IDEMPOTENCY (§22.1)
-------------------
Day sealing is keyed on ``(organization_id, user_id, work_date, ledger_version)``.
Re-sealing with the same version and identical evidence is a no-op returning the
existing row. Re-sealing with the same version and DIFFERENT evidence is an
error, not an overwrite — an evidentiary store that silently accepts a rewrite
is not evidence.

CONCURRENCY (§22.2)
-------------------
``ledger_version`` is the optimistic-concurrency token. Superseding requires
naming the version being superseded; if it is not the current version the write
is rejected with ``StaleLedgerVersion`` and the caller re-runs. Nothing is
merged.

CORRECTION BY SUPERSESSION (INV-3, §23)
---------------------------------------
Nothing is ever mutated. A correction writes a new row at the next version and
links both directions. Every prior version stays permanently readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import List, Optional, Sequence

from sqlalchemy import and_
from sqlalchemy.orm import Session

from .integrity import (
    CandidateInterval,
    IntegrityResult,
    duration_seconds,
    order_intervals,
    validate_intervals,
)
from .attribution import SHIFT_ATTRIBUTION_START_DATE
from .models import WorkDayLedger, WorkStateInterval
from .states import LedgerState, Provenance, WorkState
from .workweek import canonical_workweek_key

# §24.15's third obligation. Imported at module scope because
# `app.eco_ledger_incident` imports nothing from `app.work_ledger` — the
# dependency is one-directional by construction, so an incident can never
# become a precondition for sealing.
from app.eco_ledger_incident import raise_ledger_integrity_incident


class LedgerError(Exception):
    """Base for ledger write failures."""


class StaleLedgerVersion(LedgerError):
    """A write was attempted against a version that is no longer current.

    §22.2: "A write against a stale ledger version is rejected and the
    evaluation re-run — never merged."
    """


class LedgerEvidenceConflict(LedgerError):
    """A re-seal at the same version supplied different evidence.

    Not an overwrite. An append-only evidentiary store that silently accepts a
    rewrite of a sealed day has stopped being evidence.
    """


class LedgerStateContractViolation(LedgerError):
    """A ledger state and its frozen context contradict each other.

    Owner ruling D4-A: ``NOT_A_WORKING_DAY`` implies ``is_working_day=False``.
    A row asserting both that no work was expected and that the day was a
    working day is not a state the ledger will store — the two halves of the
    evidence would disagree, and every later reader would have to guess which
    one to believe.

    One-directional by ruling: ``is_working_day=False`` with ``SEALED`` is
    legal and is NOT refused, because a day may carry a working-day
    determination without having been classified into the zeroed path.
    """


class LedgerIntegrityFault(LedgerError):
    """The candidate evidence is inadmissible (§24.15).

    Carries the violations so an operator can see the whole defect. The day is
    recorded as LEDGER_INTEGRITY_FAULT and evaluated by nothing.
    """

    def __init__(self, result: IntegrityResult):
        super().__init__(result.fault_summary)
        self.result = result


@dataclass(frozen=True)
class DayKey:
    """Identity of a Worker-day. Tenant-scoped by construction."""

    organization_id: int
    user_id: int
    work_date: date


def _as_naive_date(work_date: date | datetime) -> datetime:
    """Normalise a work date to the midnight datetime the column stores.

    ``work_date`` is a calendar label, not an instant: it is the day the shift
    is attributed to under the tenant attribution rule (START_DATE). It carries
    no timezone of its own — the applicable zone is persisted separately on the
    ledger row.
    """
    if isinstance(work_date, datetime):
        return datetime(work_date.year, work_date.month, work_date.day)
    return datetime(work_date.year, work_date.month, work_date.day)


def get_current_ledger(db: Session, key: DayKey) -> Optional[WorkDayLedger]:
    """The current (non-superseded) ledger row for a Worker-day, or None.

    Strictly tenant-scoped: a row belonging to another organization can never
    be returned.
    """
    return (
        db.query(WorkDayLedger)
        .filter(
            and_(
                WorkDayLedger.organization_id == key.organization_id,
                WorkDayLedger.user_id == key.user_id,
                WorkDayLedger.work_date == _as_naive_date(key.work_date),
                WorkDayLedger.superseded_by.is_(None),
            )
        )
        .order_by(WorkDayLedger.ledger_version.desc())
        .first()
    )


def get_ledger_version(
    db: Session, key: DayKey, ledger_version: int
) -> Optional[WorkDayLedger]:
    """A specific historical version. Every prior version stays readable."""
    return (
        db.query(WorkDayLedger)
        .filter(
            and_(
                WorkDayLedger.organization_id == key.organization_id,
                WorkDayLedger.user_id == key.user_id,
                WorkDayLedger.work_date == _as_naive_date(key.work_date),
                WorkDayLedger.ledger_version == ledger_version,
            )
        )
        .first()
    )


def _utc(dt: datetime) -> datetime:
    """Coerce a stored or supplied instant to aware UTC before comparison.

    SQLite drops ``tzinfo`` on read while Postgres preserves it, so a value
    round-tripped through the database compares unequal to the aware value that
    was written even when they denote the same instant. The existing platform
    hit this exact hazard (see ``_utc_aware`` in ``employees/router.py``).
    Fingerprints compare INSTANTS, never tzinfo representations — otherwise an
    idempotent replay would be misread as a conflicting rewrite on SQLite and
    accepted on Postgres, which is a difference in evidentiary behaviour
    between backends.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _evidence_fingerprint(intervals: Sequence[CandidateInterval]) -> tuple:
    """A comparable, order-independent identity for a candidate evidence set.

    Used only to decide whether a re-seal at the same version is the same
    evidence (idempotent no-op) or different evidence (conflict). Deliberately
    a plain tuple rather than a hash: an exact comparison cannot collide, and
    the set is small.
    """
    return tuple(
        (i.seq, i.state, _utc(i.start_at_utc), _utc(i.end_at_utc),
         i.provenance, i.source_ref)
        for i in order_intervals(intervals)
    )


def _persisted_fingerprint(ledger: WorkDayLedger) -> tuple:
    return tuple(
        (i.seq, i.state, _utc(i.start_at_utc), _utc(i.end_at_utc),
         i.provenance, i.source_ref)
        for i in sorted(ledger.intervals, key=lambda x: (x.start_at_utc, x.seq))
    )


def seal_day(
    db: Session,
    key: DayKey,
    *,
    intervals: Sequence[CandidateInterval],
    timezone_iana: str,
    is_working_day: bool,
    working_day_source: str,
    scheduled_end_local: Optional[time] = None,
    scheduled_end_source: Optional[str] = None,
    utc_offset_seconds: int = 0,
    departure_at: Optional[datetime] = None,
    departure_provenance: Optional[str] = None,
    departure_source_ref: Optional[str] = None,
    source_fingerprint: Optional[str] = None,
    state: LedgerState = LedgerState.SEALED,
    created_by: str = "SYSTEM",
) -> WorkDayLedger:
    """Seal a Worker-day at version 1.

    ``workweek_key`` is NOT a parameter: it is derived here from the
    organization's configured week start and the day's START_DATE-attributed
    work_date (W4-4). A caller cannot supply an arbitrary week identity, which
    is what stops two callers disagreeing about which week a day belongs to.

    Idempotent on the natural key: re-sealing with identical evidence returns
    the existing row untouched. Re-sealing with different evidence raises
    ``LedgerEvidenceConflict`` — use :func:`supersede_day` to correct a sealed
    day.

    Raises ``LedgerIntegrityFault`` when the evidence is inadmissible. Nothing
    is repaired, clamped, merged or skipped (§24.15); the caller is expected to
    record the fault day and raise a platform incident.
    """
    existing = get_current_ledger(db, key)
    if existing is not None:
        # The frozen working-day result (D-18D) is part of the day's sealed
        # evidence, so it participates in "same evidence or different?". A
        # replay carrying a DIFFERENT working-day result is divergent evidence
        # about the same day; returning the existing row would tell the caller
        # its value was recorded when it was not. Fails closed and visibly.
        same_evidence = (
            _persisted_fingerprint(existing) == _evidence_fingerprint(intervals)
            and bool(existing.is_working_day) == bool(is_working_day)
            and existing.working_day_source == working_day_source
        )
        if same_evidence:
            return existing  # idempotent replay — no second row, no write
        raise LedgerEvidenceConflict(
            f"day already sealed at version {existing.ledger_version} with "
            "different evidence; use supersede_day to correct it"
        )

    return _write_version(
        db, key,
        intervals=intervals,
        ledger_version=1,
        workweek_key=canonical_workweek_key(
            db, organization_id=key.organization_id, work_date=key.work_date),
        timezone_iana=timezone_iana,
        utc_offset_seconds=utc_offset_seconds,
        is_working_day=is_working_day,
        working_day_source=working_day_source,
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
        departure_at=departure_at,
        departure_provenance=departure_provenance,
        departure_source_ref=departure_source_ref,
        source_fingerprint=source_fingerprint,
        state=state,
        created_by=created_by,
        supersedes=None,
    )


def supersede_day(
    db: Session,
    key: DayKey,
    *,
    expected_version: int,
    intervals: Sequence[CandidateInterval],
    timezone_iana: str,
    is_working_day: bool,
    working_day_source: str,
    scheduled_end_local: Optional[time] = None,
    scheduled_end_source: Optional[str] = None,
    utc_offset_seconds: int = 0,
    departure_at: Optional[datetime] = None,
    departure_provenance: Optional[str] = None,
    departure_source_ref: Optional[str] = None,
    source_fingerprint: Optional[str] = None,
    state: LedgerState = LedgerState.SEALED,
    created_by: str = "SYSTEM",
) -> WorkDayLedger:
    """Correct a sealed day by writing a new version that supersedes it.

    ``expected_version`` is the optimistic-concurrency token. If it is not the
    current version, ``StaleLedgerVersion`` is raised and nothing is written —
    the caller re-reads and re-runs (§22.2). The prior row is retained in full
    and linked in both directions; it is never mutated beyond having its
    ``superseded_by`` pointer set (INV-3, §23).
    """
    current = get_current_ledger(db, key)
    if current is None:
        raise StaleLedgerVersion(
            "no current ledger for this Worker-day; seal it before superseding"
        )
    if current.ledger_version != expected_version:
        raise StaleLedgerVersion(
            f"expected version {expected_version} but current is "
            f"{current.ledger_version}; re-run the evaluation"
        )

    new_row = _write_version(
        db, key,
        intervals=intervals,
        ledger_version=current.ledger_version + 1,
        # ── INHERITED, never re-derived (owner Ruling 4) ────────────────────
        # This previously called canonical_workweek_key(), which reads CURRENT
        # configuration. A day sealed under a Monday start and corrected after
        # the organization moved to Sunday would silently leave its historical
        # week and join another — its original week losing a day it had
        # already counted, and the new week gaining one it never evaluated.
        #
        # §12.4 forbids exactly that: "Historical workweeks remain readable and
        # reproducible indefinitely, and change only by the addition of
        # correction events, never by mutation." A correction is an addition to
        # the lineage, so it inherits the identity the lineage was opened with.
        #
        # The identity therefore travels down the supersession chain from the
        # row that was actually sealed under that boundary, and no later
        # configuration change can reach back through it.
        workweek_key=current.workweek_key,
        timezone_iana=timezone_iana,
        utc_offset_seconds=utc_offset_seconds,
        # Re-resolved by the caller, not copied: a correction adopts the
        # working-day result in force when it is written, and the superseded
        # row keeps the one it was sealed with.
        is_working_day=is_working_day,
        working_day_source=working_day_source,
        # Re-resolved by the caller alongside the working-day result,
        # never copied from the superseded row: a correction adopts the
        # schedule in force when it is written, and the superseded row
        # keeps the one it was sealed with (D-1.1).
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
        departure_at=departure_at,
        departure_provenance=departure_provenance,
        departure_source_ref=departure_source_ref,
        source_fingerprint=source_fingerprint,
        state=state,
        created_by=created_by,
        supersedes=current.id,
    )
    current.superseded_by = new_row.id
    db.flush()
    return new_row


def record_integrity_fault(
    db: Session,
    key: DayKey,
    *,
    reason: str,
    timezone_iana: str,
    is_working_day: bool,
    working_day_source: str,
    scheduled_end_local: Optional[time] = None,
    scheduled_end_source: Optional[str] = None,
    utc_offset_seconds: int = 0,
    departure_at: Optional[datetime] = None,
    departure_provenance: Optional[str] = None,
    departure_source_ref: Optional[str] = None,
    source_fingerprint: Optional[str] = None,
    created_by: str = "SYSTEM",
) -> WorkDayLedger:
    """Record a day as LEDGER_INTEGRITY_FAULT with no intervals.

    §24.15: the day is refused, not repaired. A row is still written so the
    week fold is complete and the refusal is explainable, and so an operator
    can see which day is blocked and why.

    THE WEEK IDENTITY IS INHERITED, NEVER RE-DERIVED (owner Ruling 4)
    ----------------------------------------------------------------
    A fault recorded against a day that already has a ledger row writes a NEW
    VERSION of that day, so it is a correction path and carries the lineage's
    identity — the same rule ``supersede_day`` follows.

    Re-deriving it was a live defect. A day sealed under a Monday start, whose
    organization then moved to a Sunday start, produced a fault version keyed
    to a DIFFERENT week: the sealed version stayed in the old week, the fault
    version appeared in the new one, and one Worker-day became a member of two
    workweeks — exactly what R-12.3.5 forbids ("A shift may never be
    attributed to two workweeks"), by the mutation §12.4 rules out.

    Worse, it hid itself. With the boundary unchanged both versions share a key,
    so ``eval_week`` sees two current rows on one date and refuses the week with
    ``LEDGER_MEMBERSHIP_INCONSISTENT``. Re-deriving split them across two weeks,
    where each looked singular and healthy — turning a detected fault into an
    undetected one.

    Only a FIRST version may establish an identity, which is the same thing
    ``seal_day`` does and is subject to the same strict boundary rules.
    """
    current = get_current_ledger(db, key)
    fault_version = _write_version(
        db, key,
        intervals=(),
        ledger_version=(current.ledger_version + 1) if current else 1,
        workweek_key=(
            current.workweek_key if current is not None
            else canonical_workweek_key(
                db, organization_id=key.organization_id,
                work_date=key.work_date)
        ),
        timezone_iana=timezone_iana,
        utc_offset_seconds=utc_offset_seconds,
        is_working_day=is_working_day,
        working_day_source=working_day_source,
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
        departure_at=departure_at,
        departure_provenance=departure_provenance,
        departure_source_ref=departure_source_ref,
        source_fingerprint=source_fingerprint,
        state=LedgerState.LEDGER_INTEGRITY_FAULT,
        created_by=created_by,
        supersedes=None,
        integrity_fault_reason=reason,
        validate=False,
    )

    # §24.15 lists three obligations, not two: refuse the day, record the
    # state, AND "raise a platform incident". The first two were built; this is
    # the third. It is raised HERE rather than in the producer because this is
    # the only place in the application that establishes
    # LEDGER_INTEGRITY_FAULT, so every caller inherits it and no future caller
    # can establish the state without the incident.
    #
    # It writes evidence and returns. It notifies nobody, schedules nothing and
    # changes no caller's outcome: the fault version is still what is returned,
    # so `record_integrity_fault`'s contract is exactly what it was.
    raise_ledger_integrity_incident(
        db, ledger=fault_version, raised_by=created_by)
    return fault_version


def _write_version(
    db: Session,
    key: DayKey,
    *,
    intervals: Sequence[CandidateInterval],
    ledger_version: int,
    workweek_key: str,
    timezone_iana: str,
    utc_offset_seconds: int,
    is_working_day: bool,
    working_day_source: str,
    scheduled_end_local: Optional[time] = None,
    scheduled_end_source: Optional[str] = None,
    departure_at: Optional[datetime] = None,
    departure_provenance: Optional[str] = None,
    departure_source_ref: Optional[str] = None,
    source_fingerprint: Optional[str] = None,
    state: LedgerState,
    created_by: str,
    supersedes: Optional[int],
    integrity_fault_reason: Optional[str] = None,
    validate: bool = True,
) -> WorkDayLedger:
    """Write one immutable ledger version. Internal."""
    # ── D4-A: the working-day state contract ────────────────────────────────
    # NOT_A_WORKING_DAY => is_working_day is False. Enforced here because this
    # is the ONLY WorkDayLedger construction in the application, so seal_day,
    # supersede_day and record_integrity_fault all inherit it — and so will any
    # future writer, which a per-caller check would not cover.
    #
    # The reverse implication is deliberately NOT enforced: is_working_day=False
    # with state=SEALED stays legal, because a day may carry a working-day
    # determination without having been classified into the zeroed path (D4-A).
    #
    # Service-layer only, per D14-A: no database CHECK and no migration, which
    # also avoids the SQLite ADD CONSTRAINT asymmetry that would make the
    # guarantee weaker on a migrated SQLite database than on PostgreSQL.
    if state is LedgerState.NOT_A_WORKING_DAY and bool(is_working_day):
        raise LedgerStateContractViolation(
            f"{key.work_date} cannot be sealed NOT_A_WORKING_DAY with "
            f"is_working_day=True: the state asserts no work was expected "
            f"while the frozen context asserts the opposite (D4-A). Nothing "
            f"was written."
        )

    if validate and intervals:
        result = validate_intervals(intervals, require_provenance=True)
        if not result.admissible:
            raise LedgerIntegrityFault(result)

    row = WorkDayLedger(
        organization_id=key.organization_id,
        user_id=key.user_id,
        work_date=_as_naive_date(key.work_date),
        workweek_key=workweek_key,
        # R-12.3.4 / §35: the rule that chose work_date, frozen on the row.
        # Stamped here rather than accepted as a parameter, for the same reason
        # workweek_key is derived rather than supplied (W4-4) — this is the ONLY
        # WorkDayLedger construction in the application, so seal_day,
        # supersede_day and record_integrity_fault all inherit one answer and no
        # caller can introduce a competing one.
        shift_attribution_rule=SHIFT_ATTRIBUTION_START_DATE,
        ledger_version=ledger_version,
        state=state.value,
        integrity_fault_reason=integrity_fault_reason,
        sealed_at=(
            datetime.now(timezone.utc)
            if state is not LedgerState.OPEN else None
        ),
        timezone_iana=timezone_iana,
        utc_offset_seconds=utc_offset_seconds,
        is_working_day=is_working_day,
        working_day_source=working_day_source,
        # D-1.1: frozen here because this is the ONLY WorkDayLedger
        # construction in the application, so seal_day, supersede_day
        # and record_integrity_fault all inherit one capture point and
        # no future writer can introduce a competing one.
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
        departure_at=departure_at,
        departure_provenance=departure_provenance,
        departure_source_ref=departure_source_ref,
        source_fingerprint=source_fingerprint,
        supersedes=supersedes,
        created_by=created_by,
    )
    db.add(row)
    db.flush()  # assign row.id before children reference it

    for iv in order_intervals(intervals):
        db.add(WorkStateInterval(
            ledger_id=row.id,
            organization_id=key.organization_id,
            seq=iv.seq,
            state=iv.state,
            start_at_utc=iv.start_at_utc,
            end_at_utc=iv.end_at_utc,
            duration_seconds=duration_seconds(iv.start_at_utc, iv.end_at_utc),
            provenance=iv.provenance,
            source_ref=iv.source_ref,
        ))
    db.flush()
    return row


def read_intervals(db: Session, ledger: WorkDayLedger) -> List[WorkStateInterval]:
    """Intervals of a ledger version in deterministic order (INV-8)."""
    return (
        db.query(WorkStateInterval)
        .filter(WorkStateInterval.ledger_id == ledger.id)
        .order_by(WorkStateInterval.start_at_utc, WorkStateInterval.seq)
        .all()
    )


__all__ = [
    "DayKey",
    "LedgerError",
    "StaleLedgerVersion",
    "LedgerEvidenceConflict",
    "LedgerIntegrityFault",
    "LedgerStateContractViolation",
    "get_current_ledger",
    "get_ledger_version",
    "seal_day",
    "supersede_day",
    "record_integrity_fault",
    "read_intervals",
]
