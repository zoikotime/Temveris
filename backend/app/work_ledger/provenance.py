"""
Ledger-level provenance integration.
====================================

STEP 2 of the owner-authorised Phase 2 foundation.

Step 1 gave every interval a provenance column and a vocabulary. Step 2 makes
that vocabulary *usable at the ledger level*: given a sealed Worker-day, what
evidentiary weight does its evidence carry?

WHAT THIS MODULE DOES
---------------------
Reads provenance off the intervals of a ledger version and classifies it. That
is all. It answers questions like "does this day carry any verified departure
evidence?" — a provenance question, answerable by looking at labels.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* No worked-time calculation. No durations are summed anywhere in this module.
* No day-state determination. Whether an absence of verified departure evidence
  makes a day ``MISSING_CLOCK_OUT`` is §24.6 logic belonging to the classifier
  (Step 3), which is gated on the Legal jurisdictional matrix. This module
  supplies the input to that decision; it does not make it.
* No inference of provenance from mutable current state. Provenance is read
  from the immutable column written at seal time. It is never recomputed from,
  say, whether a session currently looks abandoned — a day's evidentiary status
  must not change because an unrelated row changed later.

WHY "NEVER INFER" MATTERS
-------------------------
§7.2 calls inferring an end of day "the single worst thing this control could
do". Inferring the *provenance* of an end of day is the same error one level
up: it would let a synthesized closure acquire verified status retroactively
because the evidence it was derived from was later corrected.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from .models import WorkDayLedger, WorkStateInterval
from .states import (
    Provenance,
    ProvenanceClass,
    classify_provenance,
    is_valid_provenance,
)


@dataclass(frozen=True)
class ProvenanceSummary:
    """What kinds of evidence a ledger version is built from.

    Counts, not durations. This is a statement about evidentiary weight, not
    about time worked.
    """

    interval_count: int
    by_value: Mapping[str, int]
    by_class: Mapping[str, int]

    #: D18-F2: the day's frozen verified departure, when the ledger row
    #: carries one. NOT an interval and NOT counted in ``interval_count``,
    #: ``by_value`` or ``by_class`` — those remain strictly interval-level.
    #: Populated only by :func:`summarise_ledger_provenance`, which has the
    #: ledger row; :func:`summarise_provenance` sees intervals alone and
    #: leaves it ``None``.
    day_departure_provenance: Optional[str] = None

    @property
    def has_verified_departure_evidence(self) -> bool:
        """True when this day carries verified departure evidence.

        Either an interval was bounded by a real departure, OR the ledger row
        records the day's verified departure directly (D18-F2). The second
        limb exists because the WRK segment that would terminate at the
        clock-out can legitimately be zero-length and absent — a break or an
        observed offline gap running flush to the departure, or a break
        spanning the whole session — and the audit proved three such shapes
        reporting False despite a genuine verified clock-out.

        A day built entirely from synthesized closures and derived boundaries,
        with no recorded departure, still has no observed departure. Under
        §24.6 that is the input to a ``MISSING_CLOCK_OUT`` determination — but
        the determination itself is the classifier's (Step 3), not this
        module's.
        """
        if self.by_class.get(ProvenanceClass.VERIFIED.value, 0) > 0:
            return True
        return self.day_departure_provenance is not None

    @property
    def has_synthesized_closure(self) -> bool:
        """True when any boundary came from a closure the Worker never performed."""
        return self.by_class.get(ProvenanceClass.SYNTHESIZED.value, 0) > 0

    @property
    def is_wholly_derived(self) -> bool:
        """True when every boundary was computed rather than observed."""
        return (
            self.interval_count > 0
            and self.by_class.get(ProvenanceClass.DERIVED.value, 0) == self.interval_count
        )


def summarise_provenance(
    intervals: Sequence[WorkStateInterval] | Iterable[WorkStateInterval],
) -> ProvenanceSummary:
    """Classify the provenance of a set of persisted intervals.

    Raises ``ValueError`` on an unapproved provenance value rather than
    bucketing it as "other". A row carrying an unapproved value means the
    closed vocabulary was breached somewhere upstream, and quietly counting it
    would hide exactly the defect Step 2 exists to prevent.
    """
    by_value: Dict[str, int] = {}
    by_class: Dict[str, int] = {}
    count = 0

    for iv in intervals:
        count += 1
        value = iv.provenance
        cls = classify_provenance(value)  # raises on anything unapproved
        by_value[value] = by_value.get(value, 0) + 1
        by_class[cls.value] = by_class.get(cls.value, 0) + 1

    return ProvenanceSummary(
        interval_count=count,
        by_value=dict(by_value),
        by_class=dict(by_class),
    )


def summarise_ledger_provenance(
    db: Session, ledger: WorkDayLedger
) -> ProvenanceSummary:
    """Provenance summary for one ledger version, in deterministic order.

    Tenant scoping is inherited from the ledger row: intervals are selected by
    ``ledger_id``, and a ledger row is only ever obtained through an
    organization-scoped lookup.
    """
    intervals = (
        db.query(WorkStateInterval)
        .filter(WorkStateInterval.ledger_id == ledger.id)
        .order_by(WorkStateInterval.start_at_utc, WorkStateInterval.seq)
        .all()
    )
    summary = summarise_provenance(intervals)
    # D18-F2: fold in the day-level verified departure. The interval counts
    # above are untouched — this adds a day-level fact, not an interval.
    departure = getattr(ledger, "departure_provenance", None)
    if departure is None:
        return summary
    return replace(summary, day_departure_provenance=departure)


def verified_departure_intervals(
    intervals: Sequence[WorkStateInterval] | Iterable[WorkStateInterval],
) -> List[WorkStateInterval]:
    """Only those intervals bounded by a real departure.

    Useful to an auditor answering "which part of this day did the Worker
    actually attest to?" — the question §9.1's explanation right ultimately has
    to answer.
    """
    return [
        iv for iv in intervals
        if classify_provenance(iv.provenance) is ProvenanceClass.VERIFIED
    ]


def assert_provenance_vocabulary_closed(
    intervals: Sequence[WorkStateInterval] | Iterable[WorkStateInterval],
) -> None:
    """Raise if any interval carries a provenance outside the approved nine.

    A cheap integrity probe for a replay harness or an operational check: it
    detects a vocabulary breach that entered by a path which bypassed
    ``validate_intervals`` — a Core-level bulk insert, for instance.
    """
    for iv in intervals:
        if not is_valid_provenance(iv.provenance):
            approved = ", ".join(sorted(p.value for p in Provenance))
            raise ValueError(
                f"interval id={getattr(iv, 'id', '?')} carries unapproved "
                f"provenance {iv.provenance!r}. Approved: {approved}"
            )


__all__ = [
    "ProvenanceSummary",
    "summarise_provenance",
    "summarise_ledger_provenance",
    "verified_departure_intervals",
    "assert_provenance_vocabulary_closed",
]
