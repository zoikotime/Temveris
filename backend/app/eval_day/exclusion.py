"""
MODEL-EXCLUSION — the authoritative approved-leave overlap for a Worker-day.
============================================================================

    approved_leave_overlap(db, organization_id=..., user_id=..., work_date=...)

Answers exactly one question: **how much approved leave was recorded for this
Worker on this work date?** In seconds, so the single rounding site stays in the
evaluator.

WHAT MAKES IT AUTHORITATIVE
---------------------------
Two authorities, each consulted where it is canonical, and neither duplicated:

* ``leave_intervals.intervals_for_day`` decides WHICH intervals exist. It
  already returns current-version intervals only (a corrected interval never
  appears beside its correction), filters on ``organization_id`` as well as
  ``user_id``, and matches on ``work_date``. Withdrawal needs no special case:
  a withdrawal records a new set version holding no intervals, so a withdrawn
  day simply has none.
* ``LeaveRequest.status`` decides WHETHER the leave was granted. Step 7 is
  explicit that "``leave_requests`` remains authoritative for whether leave was
  granted… for its approval state", and this reads that column rather than
  inventing a second grant.

NO NEW TIMEZONE RULE, AND NO CLIPPING
-------------------------------------
``LeaveInterval.work_date`` is the LOCAL date the interval is attributed to
under the same START_DATE rule the work-day ledger uses (§12.3) — "an interval
crossing local midnight belongs to the date it began on, so leave and work never
disagree about which day is under discussion". So the day's leave is the
intervals bearing that ``work_date``, and no UTC window is computed, no
timezone is read, and no interval is clipped at a boundary. Inventing a window
here would be a second attribution rule competing with the ruled one.

WHY NOT DERIVED FROM ANYTHING ELSE
----------------------------------
Not from ``LeaveRequest.from_date``/``to_date`` alone, not from ``is_half_day``
or ``half_day_period`` (the platform has never mapped those to clock times, and
deciding which hours a half-day consumes is a policy question Steps 3-4 remain
blocked on), and not from clock gaps, screenshots, activity, breaks, schedule,
worker assertions or inferred absence. Only stated intervals count.

THE ONE PLACE IT FAILS CLOSED
-----------------------------
Owner ruling 11 draws a distinction this module implements literally:

* **No leave exists** -> overlap is 0. A determination, not a fallback.
* **Recognised non-approved leave** (draft, submitted, under_review, rejected)
  -> contributes 0. Also a determination: a pending request is a recorded
  intention, not granted leave.
* **Leave exists but its authority cannot be established** -> REFUSED. The
  request row is missing, or carries a status this increment has no ruling for.
  Never silently zero.

``locked_for_payroll`` was once in the third group, pending a ruling. It is now
GRANTED leave, and the repository settles it rather than the name suggesting it:

* ``leave.router.TRANSITIONS`` gives it exactly one predecessor — ``approved``
  — and no successor, and the review endpoint enforces that map. So a request
  can only reach it THROUGH approval, and cannot leave.
* ``payroll.service`` already selects ``["approved", "locked_for_payroll"]``.
* ``reports.router`` names the pair ``PAYROLL_LEAVE_STATUSES``.

Treating it as non-approved would have meant leave that counts for pay not
counting for ECO — the same day granted in one subsystem and withheld in
another.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leave_intervals import (
    LeaveIntervalSet, day_version_history, intervals_for_day,
)

from .leave_snapshot import LeaveContribution, leave_interval_fingerprint

#: Statuses that constitute GRANTED leave for ECO exclusion.
#:
#: Both members are owner-ruled. ``locked_for_payroll`` is reachable only from
#: ``approved`` and is terminal, so it is approved leave that has additionally
#: been locked for payroll — see the module docstring for the three pieces of
#: repository evidence. Widening this set further is an owner decision, not a
#: condition tweak — the same reason ``QUALIFYING_STATES`` is a set of one.
APPROVED_LEAVE_STATUSES = frozenset({
    "approved",
    "locked_for_payroll",
})

#: Statuses recognised as NOT granted leave. Recorded intervals under these
#: contribute nothing, which is a determination rather than a fallback: the
#: leave was never granted, so there is nothing to exclude.
NON_APPROVED_LEAVE_STATUSES = frozenset({
    "draft", "submitted", "under_review", "rejected",
})

#: Recognised, but with no ruling on whether they constitute granted leave.
#:
#: Empty: every member of the platform's ``LeaveStatus`` vocabulary is now
#: ruled. Kept rather than deleted because it records that fact, and because a
#: status added to that vocabulary later would belong here until ruled. It is
#: documentary only — an unruled or unrecognised status is refused by the
#: membership test in :func:`approved_leave_overlap`, not by this set.
UNRULED_LEAVE_STATUSES: frozenset = frozenset()

REASON_LEAVE_AUTHORITY_UNRESOLVED = "LEAVE_AUTHORITY_UNRESOLVED"


class LeaveOverlapOutcome(str, enum.Enum):
    RESOLVED = "RESOLVED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class LeaveOverlap:
    """How much approved leave overlapped a Worker-day, or why that is unknown.

    ``overlap_seconds`` is ``None`` unless ``RESOLVED`` — the same guarantee
    every other layer here makes, so an unresolved authority cannot be read as
    "no leave".
    """

    outcome: LeaveOverlapOutcome
    overlap_seconds: Optional[int] = None

    #: Intervals that were counted, and the requests they were granted under.
    #: Provenance for the explanation right (INV-10), using existing fields.
    interval_count: int = 0
    leave_request_ids: Tuple[int, ...] = ()

    #: EVERY request the day's resolution considered, including those that
    #: contributed nothing because their status is not ECO-approved (ruling C).
    #: This is what the immutable snapshot is built from, and what keeps "leave
    #: existed but did not qualify" distinguishable from "no leave existed".
    contributions: Tuple[LeaveContribution, ...] = ()

    reason: Optional[str] = None
    detail: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return self.outcome is LeaveOverlapOutcome.RESOLVED

    def __post_init__(self) -> None:
        if self.outcome is LeaveOverlapOutcome.RESOLVED:
            if self.overlap_seconds is None:
                raise ValueError("a RESOLVED overlap must carry its seconds")
            if self.overlap_seconds < 0:
                raise ValueError(
                    f"leave overlap cannot be negative "
                    f"(got {self.overlap_seconds!r})"
                )
            if self.reason is not None:
                raise ValueError("a RESOLVED overlap carries no reason")
        else:
            if self.reason is None:
                raise ValueError("a REFUSED overlap must state its reason")
            if self.overlap_seconds is not None or self.contributions:
                raise ValueError(
                    f"{self.reason} established no overlap, so it must not "
                    f"carry seconds or contributions"
                )


def approved_leave_overlap(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
) -> LeaveOverlap:
    """Approved-leave seconds recorded for this Worker-day.

    Reads only canonical persisted leave. Returns, never raises, and never
    substitutes zero for an authority it could not establish.
    """
    intervals = intervals_for_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date,
    )

    # ── Withdrawn requests (Owner Ruling C, A2-narrow) ──────────────────────
    # A request whose boundaries were withdrawn has no CURRENT interval on the
    # date, so ``intervals_for_day`` does not return it — correctly, since it
    # contributes nothing. But zero seconds from "leave was withdrawn" and zero
    # from "no leave existed" are different facts, and the snapshot has to keep
    # them apart. Resolved through the leave package's own reader, which reads
    # the work_date already stored on the superseded rows: no date is
    # re-derived here, and ``leave_requests.from_date`` is never consulted.
    withdrawn = _withdrawn_requests(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date,
    )

    if not intervals and not withdrawn:
        # Nothing was ever attributed to this day. Zero is the truthful answer,
        # not a fallback.
        return LeaveOverlap(
            outcome=LeaveOverlapOutcome.RESOLVED, overlap_seconds=0,
        )

    if not intervals:
        # Withdrawals only: no seconds to resolve, but the requests that were
        # considered must still be recorded. Statuses are read for exactly the
        # same reason and by exactly the same rule as below.
        return _withdrawn_only_overlap(
            db, work_date=work_date, withdrawn=withdrawn,
        )

    # The grant each interval belongs to. Queried explicitly rather than through
    # the relationship, so the tenant filter is applied here as well as by the
    # composite foreign key — a boundary resting on one mechanism has one way
    # to fail.
    set_ids = sorted({int(i.set_id) for i in intervals})
    sets = {
        int(row.id): row
        for row in db.execute(
            select(LeaveIntervalSet).where(
                LeaveIntervalSet.id.in_(set_ids),
                LeaveIntervalSet.organization_id == organization_id,
                LeaveIntervalSet.user_id == user_id,
            )
        ).scalars().all()
    }

    missing_sets = [i for i in intervals if int(i.set_id) not in sets]
    if missing_sets:
        return LeaveOverlap(
            outcome=LeaveOverlapOutcome.REFUSED,
            reason=REASON_LEAVE_AUTHORITY_UNRESOLVED,
            detail=(
                f"{len(missing_sets)} leave interval(s) on "
                f"{work_date.isoformat()} belong to no interval set readable "
                f"for this Worker and organization"
            ),
        )

    request_ids = sorted({
        int(sets[int(i.set_id)].leave_request_id) for i in intervals
    })
    # Withdrawn requests are considered by this day's resolution too, so their
    # status is read by the same rule and refused by the same test. Nothing
    # about status POLICY changes: they contribute zero under every status, and
    # the membership test decides only whether a status is one the platform has
    # ruled on at all.
    withdrawn_ids = sorted({int(h.leave_request_id) for h in withdrawn})
    statuses = _request_statuses(db, sorted(set(request_ids) | set(withdrawn_ids)))

    refusal = _refuse_unknown_statuses(
        statuses, request_ids + withdrawn_ids, work_date=work_date)
    if refusal is not None:
        return refusal

    approved = {
        rid for rid in request_ids
        if statuses[rid] in APPROVED_LEAVE_STATUSES
    }
    counted = [
        i for i in intervals
        if int(sets[int(i.set_id)].leave_request_id) in approved
    ]

    # Seconds, summed as integers. The conversion to minutes happens once, at
    # the evaluator's single rounding site — never here.
    total_seconds = sum(int(i.duration_seconds) for i in counted)

    # ── One contribution per request CONSIDERED, contributing or not ─────────
    # Ruling C: the scope is the requests owning intervals on this date, which
    # is exactly what the authoritative day resolution looked at. Unrelated
    # requests are not recorded. A non-approved request appears with zero
    # seconds rather than being dropped, so the snapshot can say "leave existed
    # and did not qualify".
    contributions = []
    for rid in request_ids:
        own = [
            i for i in intervals
            if int(sets[int(i.set_id)].leave_request_id) == rid
        ]
        # Every interval of one request on one date belongs to that request's
        # single current set, so the version is unambiguous.
        set_version = int(sets[int(own[0].set_id)].set_version)
        contributions.append(LeaveContribution(
            leave_request_id=rid,
            set_version=set_version,
            status=statuses[rid],
            interval_fingerprint=leave_interval_fingerprint(own),
            interval_count=len(own),
            contributed_seconds=(
                sum(int(i.duration_seconds) for i in own)
                if rid in approved else 0
            ),
        ))

    # And one zero contribution for each withdrawn request (ruling C).
    contributions.extend(
        _withdrawn_contributions(withdrawn, statuses=statuses))

    return LeaveOverlap(
        outcome=LeaveOverlapOutcome.RESOLVED,
        overlap_seconds=total_seconds,
        interval_count=len(counted),
        leave_request_ids=tuple(sorted(approved)),
        contributions=tuple(
            sorted(contributions, key=lambda c: c.leave_request_id)),
    )


#: The fingerprint of an empty interval list. Ruling C fixes this as the
#: withdrawn contribution's fingerprint: the request's current version holds no
#: intervals, and that is exactly what an empty list fingerprints to. Computed
#: through the same function every other contribution uses, so the two can
#: never diverge, and non-empty by construction — the CHECK constraint on
#: ``interval_fingerprint`` is satisfied.
EMPTY_INTERVAL_FINGERPRINT = leave_interval_fingerprint(())


def _withdrawn_requests(
    db: Session, *, organization_id: int, user_id: int, work_date: date
) -> Tuple:
    """Requests withdrawn after having claimed this date — ruling C, A2-narrow.

    NARROW, deliberately. A request qualifies only when its CURRENT interval
    set exists and holds no intervals at all. A request whose intervals were
    re-dated still has boundaries, just elsewhere, and is NOT a withdrawal —
    representing it as one would put a fact in the snapshot that never
    happened. ``DayVersionHistory.is_withdrawn`` is that test, and it lives in
    the leave package with the rest of the leave authority.
    """
    return tuple(
        history for history in day_version_history(
            db, organization_id=organization_id, user_id=user_id,
            work_date=work_date,
        )
        if history.is_withdrawn
    )


def _refuse_unknown_statuses(
    statuses: dict, request_ids, *, work_date: date
) -> Optional[LeaveOverlap]:
    """REFUSED when any considered request carries an unruled status.

    Unchanged in rule and in wording from the single test it replaced; factored
    out only so the withdrawn-only path applies exactly the same one rather
    than a second copy of it.
    """
    unknown = sorted({
        rid for rid in request_ids
        if statuses.get(rid) is None
        or statuses[rid] not in (APPROVED_LEAVE_STATUSES
                                 | NON_APPROVED_LEAVE_STATUSES)
    })
    if not unknown:
        return None

    offending = sorted({str(statuses.get(rid)) for rid in unknown})
    return LeaveOverlap(
        outcome=LeaveOverlapOutcome.REFUSED,
        reason=REASON_LEAVE_AUTHORITY_UNRESOLVED,
        detail=(
            f"leave request(s) {unknown} on {work_date.isoformat()} carry "
            f"status {offending} for which no ruling establishes whether "
            f"the leave was granted"
        ),
    )


def _withdrawn_contributions(withdrawn, *, statuses: dict):
    """One zero contribution per withdrawn request, in ruling C's exact shape.

    ``set_version`` is the SELECTED version — the current, empty one — not the
    version whose intervals used to claim the date. That is what the ruling
    names, and it is the version the evaluation actually read.
    """
    return [
        LeaveContribution(
            leave_request_id=int(history.leave_request_id),
            set_version=int(history.current_set_version),
            status=statuses[int(history.leave_request_id)],
            interval_fingerprint=EMPTY_INTERVAL_FINGERPRINT,
            interval_count=0,
            contributed_seconds=0,
        )
        for history in withdrawn
    ]


def _withdrawn_only_overlap(
    db: Session, *, work_date: date, withdrawn
) -> LeaveOverlap:
    """A day whose only leave was withdrawn.

    Zero seconds, exactly as before this ruling — the arithmetic is untouched.
    What changes is that the snapshot now carries the requests that were
    considered, so the day is no longer indistinguishable from one where no
    leave ever existed.
    """
    request_ids = sorted({int(h.leave_request_id) for h in withdrawn})
    statuses = _request_statuses(db, request_ids)

    refusal = _refuse_unknown_statuses(
        statuses, request_ids, work_date=work_date)
    if refusal is not None:
        return refusal

    return LeaveOverlap(
        outcome=LeaveOverlapOutcome.RESOLVED,
        overlap_seconds=0,
        interval_count=0,
        leave_request_ids=(),
        contributions=tuple(sorted(
            _withdrawn_contributions(withdrawn, statuses=statuses),
            key=lambda c: c.leave_request_id,
        )),
    )


def _request_statuses(db: Session, request_ids) -> dict:
    """The canonical approval state of each grant, keyed by request id.

    Reads ``leave_requests`` — the authority Step 7 defers to — with a deferred
    import so this package takes no module-level dependency on the legacy
    models.
    """
    if not request_ids:
        return {}

    from database.models import LeaveRequest

    rows = db.execute(
        select(LeaveRequest.id, LeaveRequest.status).where(
            LeaveRequest.id.in_(list(request_ids))
        )
    ).all()
    return {int(rid): status for rid, status in rows}


__all__ = [
    "APPROVED_LEAVE_STATUSES",
    "NON_APPROVED_LEAVE_STATUSES",
    "UNRULED_LEAVE_STATUSES",
    "REASON_LEAVE_AUTHORITY_UNRESOLVED",
    "LeaveOverlap",
    "LeaveOverlapOutcome",
    "approved_leave_overlap",
]
