"""
Recording and reading intraday leave intervals.
===============================================

STEP 7 of the owner-authorised Phase 2 foundation.

NOT WIRED
---------
No route, scheduler or existing service calls this. Applying, approving,
editing and cancelling leave all continue to work exactly as they do today, on
``leave_requests`` alone, and a request with no recorded interval set behaves
precisely as it did before this module existed.

THE ONE WAY AN INTERVAL COMES TO EXIST
--------------------------------------
Somebody states it. There is no derivation path in this module: no rule that
turns ``is_half_day`` into clock times, no inference from a schedule, no
back-computation from attendance. That is a deliberate design constraint, not an
omission —

* the platform has never mapped ``half_day_period`` to instants anywhere, so
  there is no existing answer to adopt; and
* choosing which hours of a day a half-day consumes is a policy question, and in
  several jurisdictions a legal one, which Steps 3-4 are blocked on.

Because there is exactly one origin, no provenance vocabulary is needed here. If
a later step introduces derived boundaries, that step introduces provenance —
and raises the governance question at the point where it actually arises,
instead of this step pre-empting it with a value nobody has ruled on.

NO BACKFILL
-----------
Existing whole-day and half-day requests are left exactly as they are. A request
with no interval set means "no intraday representation was captured", which is
the truth. Manufacturing intervals for historical leave would be fabricated
evidence about days nobody recorded at this granularity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Sequence, Tuple

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .errors import (
    LeaveIntervalIntegrityFault,
    LeaveIntervalOwnershipMismatch,
    LeaveRequestNotTenanted,
    StaleLeaveIntervalVersion,
)
from .models import LeaveInterval, LeaveIntervalSet
from .validation import (
    CandidateLeaveInterval,
    attributed_work_date,
    duration_seconds,
    order_leave_intervals,
    validate_leave_intervals,
)


def _utc(value: datetime) -> datetime:
    """Normalise to a true UTC instant.

    SQLite discards ``tzinfo`` on write, so an instant read back is naive even
    though it was stored aware. Comparing a freshly-supplied aware instant
    against one that has round-tripped would otherwise report two identical
    times as different — which is how an idempotent re-record was once misread
    as a conflict in the work ledger. Naive values are treated as UTC because
    that is what the column stores; nothing else is guessed.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RecordedSet:
    """What a recording produced. Returned instead of an ORM row so callers do
    not hold a live object whose mutation the listeners would then refuse."""

    set_id: int
    leave_request_id: int
    set_version: int
    organization_id: int
    user_id: int
    interval_count: int
    created: bool
    """False when an identical set already stood at this version — the recording
    was a replay, not a correction, and no new version was appended."""


@dataclass(frozen=True)
class DayVersionHistory:
    """What one leave request's version history says about one work_date.

    Returned instead of ORM rows so ``eval_day`` cannot reach past this package
    into ``LeaveInterval`` and bypass the tenant and current-version filtering
    that lives here. The accessor guard in the Step 7 suite enforces that.

    ``current_interval_count`` counts the request's intervals on EVERY date at
    its current version, not only on the date asked about — that is exactly
    what separates a withdrawal from a re-dating:

        current_set_exists and current_interval_count == 0
            -> WITHDRAWN: the boundaries were captured and then withdrawn.

        current_interval_count > 0 and not current_on_this_date
            -> RE-DATED: the request still has boundaries, on other dates.

    Both had an interval attributed to the date once. They are different facts
    and only the first is a withdrawal (Owner Ruling C, A2-narrow).
    """

    leave_request_id: int
    #: The version in force now. ``None`` only if every version vanished, which
    #: the append-only constraints make impossible; carried so the caller can
    #: refuse rather than assume.
    current_set_version: Optional[int]
    current_set_exists: bool
    #: Intervals held by the current version, across all dates.
    current_interval_count: int
    #: Whether the CURRENT version still attributes an interval to this date.
    current_on_this_date: bool

    @property
    def is_withdrawn(self) -> bool:
        """A current version exists and holds no intervals at all."""
        return self.current_set_exists and self.current_interval_count == 0


def _fingerprint(intervals: Iterable) -> Tuple:
    """Order-independent identity of an interval set.

    Used only to recognise a replay. Built from the boundaries themselves, all
    normalised to UTC, so a retry that supplies the same intervals in a
    different order is still recognised as the same set (§22.1).
    """
    return tuple(sorted(
        (
            _utc(i.start_at_utc).isoformat(),
            _utc(i.end_at_utc).isoformat(),
        )
        for i in intervals
    ))


def current_version(db: Session, *, leave_request_id: int) -> Optional[int]:
    """The highest recorded set version, or ``None`` if none was ever recorded.

    ``None`` is meaningfully different from a version whose interval list is
    empty: the first says intraday leave was never captured for this request,
    the second says it was captured and then withdrawn.
    """
    return db.execute(
        select(func.max(LeaveIntervalSet.set_version)).where(
            LeaveIntervalSet.leave_request_id == leave_request_id
        )
    ).scalar()


def get_current_set(
    db: Session, *, leave_request_id: int
) -> Optional[LeaveIntervalSet]:
    """The current set row for a request, or ``None`` if never recorded."""
    version = current_version(db, leave_request_id=leave_request_id)
    if version is None:
        return None
    return get_set_at_version(
        db, leave_request_id=leave_request_id, set_version=version
    )


def get_set_at_version(
    db: Session, *, leave_request_id: int, set_version: int
) -> Optional[LeaveIntervalSet]:
    """One historical version, exactly as it was recorded."""
    return db.execute(
        select(LeaveIntervalSet).where(
            LeaveIntervalSet.leave_request_id == leave_request_id,
            LeaveIntervalSet.set_version == set_version,
        )
    ).scalars().first()


def read_intervals(
    db: Session, *, leave_request_id: int, set_version: Optional[int] = None
) -> Tuple[LeaveInterval, ...]:
    """Intervals of one version, or of the current version when unspecified.

    Deterministically ordered by ``(start_at_utc, seq)``. An empty tuple is
    returned both when the version holds no intervals and when no version
    exists — use :func:`current_version` to tell those apart.
    """
    if set_version is None:
        set_version = current_version(db, leave_request_id=leave_request_id)
        if set_version is None:
            return ()

    row = get_set_at_version(
        db, leave_request_id=leave_request_id, set_version=set_version
    )
    if row is None:
        return ()

    return tuple(db.execute(
        select(LeaveInterval)
        .where(LeaveInterval.set_id == row.id)
        .order_by(LeaveInterval.start_at_utc, LeaveInterval.seq)
    ).scalars().all())


def intervals_for_day(
    db: Session, *, organization_id: int, user_id: int, work_date: date
) -> Tuple[LeaveInterval, ...]:
    """Current-version intraday leave for one Worker-day, tenant-scoped.

    Superseded versions are excluded: a corrected interval must not appear
    alongside the correction. Filters on ``organization_id`` as well as
    ``user_id`` — a Worker id alone is not a tenant boundary.
    """
    current = (
        select(
            LeaveIntervalSet.leave_request_id.label("request_id"),
            func.max(LeaveIntervalSet.set_version).label("version"),
        )
        .where(
            LeaveIntervalSet.organization_id == organization_id,
            LeaveIntervalSet.user_id == user_id,
        )
        .group_by(LeaveIntervalSet.leave_request_id)
        .subquery()
    )
    return tuple(db.execute(
        select(LeaveInterval)
        .join(LeaveIntervalSet, LeaveInterval.set_id == LeaveIntervalSet.id)
        .join(
            current,
            (LeaveIntervalSet.leave_request_id == current.c.request_id)
            & (LeaveIntervalSet.set_version == current.c.version),
        )
        .where(
            LeaveInterval.organization_id == organization_id,
            LeaveInterval.work_date == work_date,
        )
        .order_by(LeaveInterval.start_at_utc, LeaveInterval.seq)
    ).scalars().all())


def day_version_history(
    db: Session, *, organization_id: int, user_id: int, work_date: date
) -> Tuple[DayVersionHistory, ...]:
    """Requests that EVER attributed an interval to this date, and their state now.

    Owner Ruling C (A2-narrow) needs one fact the current-version reader cannot
    supply: a request whose boundaries were withdrawn has no current interval on
    the date, so :func:`intervals_for_day` — correctly — does not return it, and
    the day cannot tell "leave was withdrawn" from "no leave existed".

    THE DATE IS READ, NEVER RE-DERIVED
    ----------------------------------
    ``LeaveInterval.work_date`` is a stored local date, computed by
    :func:`attributed_work_date` under the START_DATE rule at the moment the
    version was recorded, with the organization's zone frozen on the set
    (§12.2). This reader compares that stored value. It introduces no timezone
    rule, no calendar rule and no attribution rule, and it does not consult
    ``leave_requests`` at all.

    WHY ANY VERSION, AND WHY THAT IS NOT ENOUGH ON ITS OWN
    -----------------------------------------------------
    Scanning every version answers "was this date ever claimed by this
    request". That alone is TOO BROAD: a correction that moved leave from
    Monday to Tuesday also once claimed Monday. Each row therefore carries the
    request's CURRENT state so the caller can separate the two, and
    :attr:`DayVersionHistory.is_withdrawn` is the only classification this
    module offers. Deciding what to do with a re-dated request is the caller's,
    and Ruling C says: nothing.

    Tenant-scoped on both ``organization_id`` and ``user_id``, like every reader
    here — a Worker id alone is not a tenant boundary. Uses
    ``ix_leave_interval_org_date``, which already exists.
    """
    #: Requests with an interval attributed to this date at ANY version.
    ever = db.execute(
        select(LeaveIntervalSet.leave_request_id)
        .join(LeaveInterval, LeaveInterval.set_id == LeaveIntervalSet.id)
        .where(
            LeaveIntervalSet.organization_id == organization_id,
            LeaveIntervalSet.user_id == user_id,
            LeaveInterval.organization_id == organization_id,
            LeaveInterval.work_date == work_date,
        )
        .group_by(LeaveIntervalSet.leave_request_id)
        .order_by(LeaveIntervalSet.leave_request_id)
    ).scalars().all()

    out = []
    for request_id in ever:
        version = current_version(db, leave_request_id=int(request_id))
        current = (
            get_set_at_version(
                db, leave_request_id=int(request_id), set_version=version)
            if version is not None else None
        )
        if current is None:
            out.append(DayVersionHistory(
                leave_request_id=int(request_id),
                current_set_version=version,
                current_set_exists=False,
                current_interval_count=0,
                current_on_this_date=False,
            ))
            continue

        # Guard the tenant on the CURRENT version too: a request whose current
        # set belongs elsewhere is not this Worker-day's business.
        if (int(current.organization_id) != organization_id
                or int(current.user_id) != user_id):
            continue

        counts = db.execute(
            select(
                func.count(),
                func.sum(
                    case((LeaveInterval.work_date == work_date, 1), else_=0)
                ),
            )
            .select_from(LeaveInterval)
            .where(
                LeaveInterval.set_id == current.id,
                LeaveInterval.organization_id == organization_id,
            )
        ).one()
        total = int(counts[0] or 0)
        on_date = int(counts[1] or 0)

        out.append(DayVersionHistory(
            leave_request_id=int(request_id),
            current_set_version=int(current.set_version),
            current_set_exists=True,
            current_interval_count=total,
            current_on_this_date=on_date > 0,
        ))

    return tuple(out)


def record_leave_intervals(
    db: Session,
    *,
    leave_request,
    intervals: Sequence[CandidateLeaveInterval],
    timezone_iana: str,
    zone,
    expected_version: Optional[int] = None,
    recorded_by: str = "SYSTEM",
) -> RecordedSet:
    """Record an intraday interval set against an existing leave request.

    ``leave_request`` is the canonical grant — a ``LeaveRequest`` row, not an
    id — because this function must read its organization and Worker to bind
    against them. Passing an id would let a caller state an identity the grant
    does not agree with.

    ``expected_version`` is the optimistic-concurrency token: the version the
    caller believed was current. ``None`` means "this is the first recording"
    and is rejected if any version already exists. Two administrators correcting
    concurrently cannot interleave — the loser is told to re-run.

    Replaying an identical set at the current version is a no-op returning the
    existing version (§22.1), so a retried request does not append a spurious
    correction.

    Raises rather than returning a partial result. Every refusal leaves the
    database untouched.
    """
    organization_id = getattr(leave_request, "organization_id", None)
    if organization_id is None:
        raise LeaveRequestNotTenanted(
            f"leave request {getattr(leave_request, 'id', None)} has no "
            f"organization_id; intraday intervals will not inherit a NULL "
            f"tenant. Assign the request to an organization first."
        )

    user_id = getattr(leave_request, "user_id", None)
    if user_id is None:
        raise LeaveIntervalOwnershipMismatch(
            f"leave request {getattr(leave_request, 'id', None)} has no user_id"
        )

    leave_request_id = leave_request.id

    # ── 1. Are the offered intervals admissible at all? ─────────────────────
    result = validate_leave_intervals(intervals)
    if not result.admissible:
        raise LeaveIntervalIntegrityFault(
            f"intraday leave intervals for request {leave_request_id} are "
            f"inadmissible: {result.fault_summary}",
            violations=result.violations,
        )

    # ── 2. Concurrency: is the caller correcting what is actually current? ──
    existing = current_version(db, leave_request_id=leave_request_id)
    if expected_version != existing:
        raise StaleLeaveIntervalVersion(
            f"leave request {leave_request_id} is at version {existing!r}, "
            f"but the correction was computed against {expected_version!r}. "
            f"Re-read and re-run; interval sets are never merged."
        )

    # ── 3. Replay? Then this is not a correction. ───────────────────────────
    if existing is not None:
        stored = read_intervals(
            db, leave_request_id=leave_request_id, set_version=existing
        )
        if _fingerprint(stored) == _fingerprint(intervals):
            row = get_set_at_version(
                db, leave_request_id=leave_request_id, set_version=existing
            )
            return RecordedSet(
                set_id=row.id, leave_request_id=leave_request_id,
                set_version=existing, organization_id=row.organization_id,
                user_id=row.user_id, interval_count=len(stored), created=False,
            )

    # ── 4. Append the next version. Nothing existing is touched. ────────────
    previous = (
        get_set_at_version(
            db, leave_request_id=leave_request_id, set_version=existing
        )
        if existing is not None else None
    )
    new_version = 1 if existing is None else existing + 1

    interval_set = LeaveIntervalSet(
        organization_id=organization_id,
        user_id=user_id,
        leave_request_id=leave_request_id,
        set_version=new_version,
        supersedes=previous.id if previous is not None else None,
        timezone_iana=timezone_iana,
        recorded_at_utc=datetime.now(timezone.utc),
        recorded_by=recorded_by,
    )
    db.add(interval_set)
    db.flush()

    for candidate in order_leave_intervals(intervals):
        db.add(LeaveInterval(
            set_id=interval_set.id,
            organization_id=organization_id,
            seq=candidate.seq,
            # START_DATE attribution, per the ruling already in force for the
            # work-day ledger. Computed here, once, and stored — so a later
            # timezone change cannot re-attribute a recorded interval.
            work_date=attributed_work_date(candidate.start_at_utc, zone),
            start_at_utc=candidate.start_at_utc,
            end_at_utc=candidate.end_at_utc,
            duration_seconds=duration_seconds(
                candidate.start_at_utc, candidate.end_at_utc
            ),
        ))
    db.flush()

    return RecordedSet(
        set_id=interval_set.id,
        leave_request_id=leave_request_id,
        set_version=new_version,
        organization_id=organization_id,
        user_id=user_id,
        interval_count=len(list(intervals)),
        created=True,
    )


def withdraw_leave_intervals(
    db: Session,
    *,
    leave_request,
    timezone_iana: str,
    zone,
    expected_version: int,
    recorded_by: str = "SYSTEM",
) -> RecordedSet:
    """Withdraw a request's intraday representation, without erasing history.

    Records an EMPTY set version. The prior version stays exactly as it was, so
    "this leave used to have intraday boundaries and no longer does" is a
    readable fact rather than a gap. This is deliberately not a delete: the
    existing leave module hard-deletes cancelled requests, and that is precisely
    why the intraday record as it stood on a past date cannot be reconstructed
    there.

    The parent grant is untouched. Withdrawing intraday boundaries says nothing
    about whether the leave itself was cancelled.
    """
    return record_leave_intervals(
        db,
        leave_request=leave_request,
        intervals=(),
        timezone_iana=timezone_iana,
        zone=zone,
        expected_version=expected_version,
        recorded_by=recorded_by,
    )


__all__ = [
    "RecordedSet",
    "current_version",
    "get_current_set",
    "get_set_at_version",
    "read_intervals",
    "intervals_for_day",
    "DayVersionHistory",
    "day_version_history",
    "record_leave_intervals",
    "withdraw_leave_intervals",
]
