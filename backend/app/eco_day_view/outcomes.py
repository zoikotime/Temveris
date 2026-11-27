"""
The typed result of one ECO day read.
=====================================

A VALUE, not an exception, and a value that carries EVIDENCE rather than COPY.

WHY THERE IS NO ``DeferralReason`` ENUM HERE
--------------------------------------------
Deliberately absent. §24.6 and §24.7 are already distinguishable from canonical
persisted evidence the ledger has held since D18-F2:

    missing clock-out (§24.6)   ledger_state = MISSING_CLOCK_OUT
    unverified exit   (§24.7)   ledger_state = SEALED, has_unverified_exit

Both are existing authoritative vocabulary. Minting a third name for the same
two facts would create a second place the §24.6/§24.7 boundary is defined, and
the whole point of owner ruling B3's Option A was that there must be exactly
one. So the contract reports the evidence and leaves naming to the surface —
which is also where §31 says the naming authority lives.

WHY THERE IS NO RENDERED TEXT HERE
-----------------------------------
This type carries no label, no sentence and no copy key. §28.1's Status
vocabulary is locked to §31, and §31 registers no deferred string, so there is
nothing authoritative to put in such a field. A read contract that carried a
rendered status would have to invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class EcoDayView:
    """One Worker-day's ECO state, as persisted, with the evidence that explains it.

    Every field is read from an immutable row. Nothing here is recomputed from
    current configuration, so the same (org, worker, date) read tomorrow under a
    changed policy, schedule or workweek boundary returns the same answer.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    organization_id: int
    user_id: int
    work_date: date

    # ── The evaluation, as recorded ─────────────────────────────────────────
    #: ``EcoDayState`` value. The internal state name, NOT a display label —
    #: mapping it to §28.1's locked Status vocabulary is the renderer's job.
    evaluation_state: str
    evaluation_generation: int
    shortfall_computed: bool
    contributes_to_accumulator_minutes: Optional[int]

    # ── The ledger version this evaluation was made against ─────────────────
    #: Not the CURRENT version — the one named on the record. A later
    #: correction produces a new record with its own version; it does not
    #: rewrite what this one was evaluated from.
    ledger_version: int
    #: ``LedgerState`` value, or None when the record's ledger version is no
    #: longer present. The §24.6 / §24.7 distinction lives here.
    ledger_state: Optional[str]
    #: §24.7: the day's sealed evidence contains an exit nobody verified.
    #: Answered by EVAL-DAY's own §19 step-6 predicate, never restated.
    has_unverified_exit: bool
    #: §24.15, free text as the ledger recorded it. Present only on a fault.
    integrity_fault_reason: Optional[str]

    # ── Lineage (§23) ───────────────────────────────────────────────────────
    supersedes_record_id: Optional[int]
    superseded_by_record_id: Optional[int]

    # ── Frozen configuration identity (owner Rulings B and E) ───────────────
    # Recorded ON the day record and read from it, never re-resolved: these
    # say WHICH policy and configuration produced the figures below, which is
    # what makes a historical day checkable years later. A day evaluated
    # before a policy change keeps naming the version it was evaluated under.
    #
    # Defaulted because they are optional ON THE RECORD — a deferred or
    # unevaluated day resolves no configuration and truthfully has none.
    policy_version: Optional[str] = None
    config_version: Optional[str] = None
    #: The immutable leave input this evaluation's exclusion was computed from.
    #: A day with no contributing leave references an explicit EMPTY snapshot,
    #: so "no leave" and "leave nobody captured" stay different facts.
    leave_input_snapshot_key: Optional[str] = None

    #: WHICH condition deferred the day (D-1), or None for the departure/ledger
    #: deferral this state has always carried. Carried here because the
    #: renderer selects between two non-interchangeable bodies on it — it is
    #: rendering input, not an implementation detail.
    evaluation_deferral_reason: Optional[str] = None

    #: The classification input the evaluation used (C-1). Provenance: it
    #: explains WHICH assertions produced this answer, and it is what makes a
    #: later correction a different evaluation generation rather than a
    #: rewrite.
    classification_snapshot_key: Optional[str] = None

    # ── The measurements, exactly as recorded ───────────────────────────────
    # All NULL together on a deferred or unevaluated day: ``DayEvaluation``
    # refuses to persist a measurement for a state that has none, so absence
    # here is a recorded fact rather than a lookup that failed.
    #
    # THERE IS NO SECONDS FIELD, DELIBERATELY. §35 mandates
    # ``qualifying_seconds`` ("pre-rounding, for reproducibility") and §28.1
    # requires the rounding basis to be shown in raw seconds — but
    # ``EcoDayRecord`` does not persist it, and owner Ruling B forbids both
    # inventing it and reconstructing it from interval durations at read time.
    # Closing that §35 gap is a separate EVAL-DAY increment.
    required_minutes: Optional[int] = None
    qualifying_minutes: Optional[int] = None
    gross_shortfall_minutes: Optional[int] = None
    exclusion_minutes: Optional[int] = None
    attributable_minutes: Optional[int] = None
    schedule_variance_minutes: Optional[int] = None

    @property
    def is_deferred(self) -> bool:
        """§24.7 / §24.6: the day is neither counted nor excused."""
        from app.eval_day import EcoDayState

        return self.evaluation_state == EcoDayState.DEFERRED_PENDING_LEDGER.value

    @property
    def is_missing_clock_out(self) -> bool:
        """§24.6: there is no exit at all."""
        from app.work_ledger import LedgerState

        return self.ledger_state == LedgerState.MISSING_CLOCK_OUT.value

    @property
    def is_unverified_exit(self) -> bool:
        """§24.7: an exit EXISTS and nobody verified it.

        Mutually exclusive with :attr:`is_missing_clock_out` by construction —
        one requires the sealed state, the other forbids it — which is what
        keeps §19.1 ("a deferred day is not a missing clock-out and must not be
        reported as one") true at the read surface as well as at the ledger.
        """
        from app.work_ledger import LedgerState

        return (self.ledger_state == LedgerState.SEALED.value
                and self.has_unverified_exit)

    @property
    def has_integrity_fault(self) -> bool:
        """§24.15: the day holds inadmissible evidence and was not evaluated."""
        from app.work_ledger import LedgerState

        return self.ledger_state == LedgerState.LEDGER_INTEGRITY_FAULT.value


__all__ = ["EcoDayView"]
