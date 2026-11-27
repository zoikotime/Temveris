"""
Admissibility rules for intraday leave intervals — pure, no I/O, no ORM.
=======================================================================

STEP 7 of the owner-authorised Phase 2 foundation.

Integrity is a property of the evidence, not of its persistence, so these rules
are testable without a database and cannot be bypassed by a caller that happens
to hold a session.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
* **No derivation.** Nothing in this module turns a whole-day or half-day leave
  request into intraday boundaries. ``half_day_period`` is a label
  ("first_half" / "second_half") that the platform never maps to clock times
  anywhere; inventing that mapping would require deciding which hours of a
  working day a half-day consumes, which is a policy — in some jurisdictions a
  legal — question. Intervals here are the ones somebody stated.
* **No worked-time arithmetic.** This module says when leave was taken, never
  how much work that displaces. §16.4 duration arithmetic is imported from the
  ledger rather than reimplemented, so the two layers cannot drift apart on what
  "seconds between two instants" means.
* **No inference from activity, idle time, clock gaps or break records.**
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional, Tuple

# The ONE duration implementation. Imported, not copied: two functions computing
# elapsed seconds is precisely the divergence this programme exists to remove.
# An import-graph test asserts this module takes nothing else from the ledger —
# no models, no service, no work-state vocabulary.
from app.work_ledger.integrity import duration_seconds


class LeaveIntervalFault(str, enum.Enum):
    """Why an offered interval set is inadmissible."""

    NON_POSITIVE_DURATION = "NON_POSITIVE_DURATION"
    """end <= start. Leave that ends before or when it began did not happen."""

    OVERLAPPING_INTERVALS = "OVERLAPPING_INTERVALS"
    """Two intervals cover the same instant. A Worker cannot be on leave twice
    over; one of them is wrong and neither may be trusted."""

    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    """A boundary carries no timezone. §16.4 prohibits duration arithmetic on
    naive datetimes, and the existing leave columns being naive is exactly how a
    payroll run once failed — see app/payroll/service.py."""

    DUPLICATE_SEQUENCE = "DUPLICATE_SEQUENCE"
    """Two intervals claim the same position. Ordering would then depend on
    iteration order, which INV-8 does not permit."""

    NON_POSITIVE_SEQUENCE = "NON_POSITIVE_SEQUENCE"
    """Sequence numbers start at 1 and increase. Zero or negative is a caller
    error, not an ordering."""


@dataclass(frozen=True)
class CandidateLeaveInterval:
    """One intraday leave interval offered for recording, before it is stored.

    A plain value object, not an ORM row: an interval that has not been accepted
    yet should not be a database row waiting to be rolled back.

    Both instants MUST be timezone-aware. There is no naive convenience form —
    accepting one and assuming UTC is how a leave boundary silently moves by
    hours when the tenant is not in UTC.
    """

    seq: int
    start_at_utc: datetime
    end_at_utc: datetime


@dataclass(frozen=True)
class LeaveIntervalViolation:
    fault: LeaveIntervalFault
    detail: str
    seq: Optional[int] = None
    other_seq: Optional[int] = None


@dataclass(frozen=True)
class LeaveIntervalValidation:
    """Outcome of checking a candidate set. ``admissible`` is the gate."""

    admissible: bool
    violations: Tuple[LeaveIntervalViolation, ...]

    @property
    def fault_summary(self) -> str:
        if self.admissible:
            return "admissible"
        return "; ".join(
            f"{v.fault.value}: {v.detail}" for v in self.violations
        )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def order_leave_intervals(
    intervals: Iterable[CandidateLeaveInterval],
) -> List[CandidateLeaveInterval]:
    """Deterministic ordering by ``(start_at_utc, seq)``.

    Total and pure: the same input always yields the same order. ``seq`` breaks
    the tie so two intervals sharing a start instant cannot order differently
    between runs (INV-8).
    """
    return sorted(intervals, key=lambda i: (i.start_at_utc, i.seq))


def validate_leave_intervals(
    intervals: Iterable[CandidateLeaveInterval],
) -> LeaveIntervalValidation:
    """Check a candidate set. Reports EVERY violation, not just the first.

    An operator fixing a bad import wants the whole list; returning one fault at
    a time turns a single correction into several round trips.

    An EMPTY set is admissible. That is not an oversight: recording an empty set
    is how intraday leave is withdrawn without deleting the history that it once
    existed.
    """
    candidates = list(intervals)
    violations: List[LeaveIntervalViolation] = []

    # ── Per-interval checks ─────────────────────────────────────────────────
    seen_seq = {}
    for interval in candidates:
        if interval.seq < 1:
            violations.append(LeaveIntervalViolation(
                fault=LeaveIntervalFault.NON_POSITIVE_SEQUENCE,
                detail=f"seq must be >= 1, got {interval.seq}",
                seq=interval.seq,
            ))
        if interval.seq in seen_seq:
            violations.append(LeaveIntervalViolation(
                fault=LeaveIntervalFault.DUPLICATE_SEQUENCE,
                detail=f"seq {interval.seq} used more than once",
                seq=interval.seq, other_seq=interval.seq,
            ))
        seen_seq[interval.seq] = True

        naive = [
            name for name, value in (
                ("start_at_utc", interval.start_at_utc),
                ("end_at_utc", interval.end_at_utc),
            )
            if not _is_aware(value)
        ]
        if naive:
            violations.append(LeaveIntervalViolation(
                fault=LeaveIntervalFault.NAIVE_TIMESTAMP,
                detail=f"{', '.join(naive)} carries no timezone",
                seq=interval.seq,
            ))
            # Duration and overlap cannot be judged on a naive instant, and
            # assuming a zone to keep checking is the error being reported.
            continue

        if duration_seconds(interval.start_at_utc, interval.end_at_utc) <= 0:
            violations.append(LeaveIntervalViolation(
                fault=LeaveIntervalFault.NON_POSITIVE_DURATION,
                detail=(
                    f"end {interval.end_at_utc.isoformat()} is not after "
                    f"start {interval.start_at_utc.isoformat()}"
                ),
                seq=interval.seq,
            ))

    # ── Pairwise overlap, over the intervals that are individually sound ────
    sound = [
        i for i in candidates
        if _is_aware(i.start_at_utc) and _is_aware(i.end_at_utc)
        and duration_seconds(i.start_at_utc, i.end_at_utc) > 0
    ]
    ordered = order_leave_intervals(sound)
    for earlier, later in zip(ordered, ordered[1:]):
        # Half-open [start, end): one interval ending exactly when the next
        # begins is contiguous, not overlapping. Treating that as an overlap
        # would make two adjacent leave blocks in one day unrecordable.
        if later.start_at_utc < earlier.end_at_utc:
            violations.append(LeaveIntervalViolation(
                fault=LeaveIntervalFault.OVERLAPPING_INTERVALS,
                detail=(
                    f"interval {later.seq} starts "
                    f"{later.start_at_utc.isoformat()} before interval "
                    f"{earlier.seq} ends {earlier.end_at_utc.isoformat()}"
                ),
                seq=later.seq, other_seq=earlier.seq,
            ))

    return LeaveIntervalValidation(
        admissible=not violations, violations=tuple(violations),
    )


def attributed_work_date(start_at_utc: datetime, zone) -> date:
    """The local calendar date an interval is attributed to.

    START_DATE attribution, matching the owner ruling already in force for the
    work-day ledger (§12.3). An interval running 22:00 to 06:00 belongs to the
    date it STARTED on, in the organization's zone — the same answer the ledger
    gives for a night shift, so leave and work cannot disagree about which day
    they are talking about.

    This reuses an existing ruling rather than making a new one. No cross-midnight
    decision is invented here.
    """
    if not _is_aware(start_at_utc):
        raise ValueError(
            "attributed_work_date requires a timezone-aware instant; "
            "a naive start has no local date"
        )
    return start_at_utc.astimezone(zone).date()


__all__ = [
    "LeaveIntervalFault",
    "CandidateLeaveInterval",
    "LeaveIntervalViolation",
    "LeaveIntervalValidation",
    "order_leave_intervals",
    "validate_leave_intervals",
    "attributed_work_date",
    "duration_seconds",
]
