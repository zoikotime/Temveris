"""
Ledger integrity validation — PURE.
===================================

STEP 1 of the owner-authorised Phase 2 foundation. No database, no ORM, no
clock reads, no worked-time calculation. Given an ordered set of candidate
intervals, decide whether the set is admissible as sealed evidence.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
ZT-SPEC-ECO-002 §24.15:

    "An interval with end <= start, or overlapping intervals in the sealed
    set, indicates a ledger defect. This control MUST NOT repair it, skip it,
    or clamp it. It MUST refuse to evaluate the day
    (state: LEDGER_INTEGRITY_FAULT), raise a platform incident, and evaluate
    nothing until the ledger is corrected. Silently coping with corrupt
    evidence is how evidentiary systems lose their standing."

This is the exact inverse of what the existing platform does today, and the
inversion is deliberate. ``employees/router.py::_actual_break_seconds_for_sessions``
currently UNIONS break intervals (:2175, :2853) specifically to absorb
overlapping rows from client retries, and SKIPS intervals where ``end <= start``
(:2245, :2842). That behaviour is correct for its own purpose — it protects a
dashboard number from a retrying client — but it is the opposite of an
evidentiary contract. Nothing in this module may merge, clamp, skip or repair.

DETERMINISTIC ORDERING
----------------------
Intervals order by ``(start_at_utc, seq)``. The tiebreaker matters: two
intervals may legitimately share a start instant (a zero-length gap between a
break ending and work resuming), and INV-8 requires byte-identical output from
identical inputs, so the order must never depend on set iteration or insertion
order.

INTEGER SECONDS
---------------
Durations are computed as integer seconds from true UTC instants. No rounding
happens anywhere in this module — §16.2 places the single rounding site in
evaluation, not in the ledger.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from .states import Provenance, is_valid_provenance


class IntegrityFault(str, enum.Enum):
    """Why a candidate interval set is inadmissible as sealed evidence."""

    NON_POSITIVE_DURATION = "NON_POSITIVE_DURATION"
    """end <= start. An interval that ends before or when it began."""

    OVERLAPPING_INTERVALS = "OVERLAPPING_INTERVALS"
    """Two intervals cover the same instant. The Worker cannot have been in two
    states at once, so one of them is wrong and neither may be trusted."""

    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    """A boundary carries no timezone. §16.4 prohibits duration arithmetic on
    naive datetimes; a naive instant is not a true UTC instant."""

    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    """No interval may exist without provenance (Gate 6, correction C-02)."""

    INVALID_PROVENANCE = "INVALID_PROVENANCE"
    """Provenance is present but is not one of the eight owner-approved values.

    Step 1 checked only that provenance was truthy, so a typo (``"WORKR"``), a
    case variant (``"worker"``) or an invented value (``"IMPORTED"``) would
    have been accepted and persisted. A provenance column that accepts
    arbitrary strings is not a closed vocabulary, and §32 warns that enum
    values are a product surface that cannot be corrected later without a
    breaking change. Adding a ninth value is an owner ruling, not a code
    change."""


@dataclass(frozen=True)
class CandidateInterval:
    """One interval offered to the ledger, before sealing.

    Deliberately a plain value object rather than an ORM row: integrity is a
    property of the evidence, not of its persistence, and keeping this pure
    means the rules are testable without a database.
    """

    seq: int
    state: str
    start_at_utc: datetime
    end_at_utc: datetime
    provenance: Optional[str] = None
    source_ref: Optional[str] = None


@dataclass(frozen=True)
class IntegrityViolation:
    fault: IntegrityFault
    detail: str
    seq: Optional[int] = None
    other_seq: Optional[int] = None


@dataclass(frozen=True)
class IntegrityResult:
    """Outcome of validating a candidate set. ``admissible`` is the gate."""

    admissible: bool
    violations: Tuple[IntegrityViolation, ...]

    @property
    def fault_summary(self) -> str:
        if self.admissible:
            return ""
        return "; ".join(
            f"{v.fault.value}: {v.detail}" for v in self.violations
        )


def _is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() is not None


def duration_seconds(start_at_utc: datetime, end_at_utc: datetime) -> int:
    """Integer seconds between two true UTC instants.

    §16.4 REQUIRED form: ``instantEnd.epochSeconds - instantStart.epochSeconds``
    where both instants are true UTC instants. Raises on a naive input rather
    than coercing one, because silently assuming a timezone is how a DST bug
    becomes a wage dispute.
    """
    if not _is_aware(start_at_utc) or not _is_aware(end_at_utc):
        raise ValueError(
            "duration_seconds requires timezone-aware instants; "
            "naive datetimes are prohibited by §16.4"
        )
    delta = end_at_utc.astimezone(timezone.utc) - start_at_utc.astimezone(timezone.utc)
    return int(delta.total_seconds())


def order_intervals(
    intervals: Iterable[CandidateInterval],
) -> List[CandidateInterval]:
    """Deterministic ordering by (start_at_utc, seq).

    Pure and total: the same input always yields the same order, regardless of
    iteration or insertion order (INV-8).
    """
    return sorted(intervals, key=lambda i: (i.start_at_utc, i.seq))


def validate_intervals(
    intervals: Sequence[CandidateInterval],
    *,
    require_provenance: bool = True,
) -> IntegrityResult:
    """Decide whether a candidate set is admissible as sealed evidence.

    Returns every violation found rather than the first, so an operator sees
    the whole defect at once instead of fixing it one interval at a time.

    NOTHING IS REPAIRED. No interval is merged, clamped, skipped, reordered
    into validity, or silently dropped. The only outcomes are "admissible" and
    "refuse the day".

    An empty set is admissible: a day with no intervals is not corrupt, it is
    simply a day with no recorded work. Whether such a day is
    NOT_A_WORKING_DAY or MISSING_CLOCK_OUT is a separate determination and is
    not made here.
    """
    violations: List[IntegrityViolation] = []

    for iv in intervals:
        # Timezone awareness first: every later check does instant arithmetic.
        if not _is_aware(iv.start_at_utc) or not _is_aware(iv.end_at_utc):
            violations.append(IntegrityViolation(
                fault=IntegrityFault.NAIVE_TIMESTAMP,
                detail=f"interval seq={iv.seq} has a naive boundary",
                seq=iv.seq,
            ))
            continue

        if iv.end_at_utc <= iv.start_at_utc:
            violations.append(IntegrityViolation(
                fault=IntegrityFault.NON_POSITIVE_DURATION,
                detail=(
                    f"interval seq={iv.seq} ends at or before it starts "
                    f"({iv.start_at_utc.isoformat()} -> {iv.end_at_utc.isoformat()})"
                ),
                seq=iv.seq,
            ))

        if not iv.provenance:
            if require_provenance:
                violations.append(IntegrityViolation(
                    fault=IntegrityFault.MISSING_PROVENANCE,
                    detail=f"interval seq={iv.seq} carries no provenance",
                    seq=iv.seq,
                ))
        elif not is_valid_provenance(iv.provenance):
            # Checked whenever provenance is present, even when it is not
            # required: an unapproved value is wrong regardless of whether one
            # was demanded, and silently persisting it would open the closed
            # vocabulary that §32 says cannot be corrected later.
            approved = ", ".join(sorted(p.value for p in Provenance))
            violations.append(IntegrityViolation(
                fault=IntegrityFault.INVALID_PROVENANCE,
                detail=(
                    f"interval seq={iv.seq} carries provenance "
                    f"{iv.provenance!r}, which is not owner-approved. "
                    f"Approved: {approved}"
                ),
                seq=iv.seq,
            ))

    # Overlap detection runs only over intervals whose boundaries are usable.
    # A naive or non-positive interval has already failed; pairing it against
    # its neighbours would report a second, derivative fault for one defect.
    usable = [
        iv for iv in intervals
        if _is_aware(iv.start_at_utc)
        and _is_aware(iv.end_at_utc)
        and iv.end_at_utc > iv.start_at_utc
    ]
    ordered = order_intervals(usable)
    for prev, nxt in zip(ordered, ordered[1:]):
        # Half-open [start, end): touching is legal, overlapping is not.
        if nxt.start_at_utc < prev.end_at_utc:
            violations.append(IntegrityViolation(
                fault=IntegrityFault.OVERLAPPING_INTERVALS,
                detail=(
                    f"interval seq={nxt.seq} starts {nxt.start_at_utc.isoformat()} "
                    f"before interval seq={prev.seq} ends {prev.end_at_utc.isoformat()}"
                ),
                seq=prev.seq,
                other_seq=nxt.seq,
            ))

    return IntegrityResult(
        admissible=not violations,
        violations=tuple(violations),
    )


__all__ = [
    "IntegrityFault",
    "CandidateInterval",
    "IntegrityViolation",
    "IntegrityResult",
    "duration_seconds",
    "order_intervals",
    "validate_intervals",
]
