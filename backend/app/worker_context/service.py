"""
Recording and resolving worker context over time.
=================================================

WORKER CONTEXT HISTORY foundation.

NOT WIRED
---------
No route, scheduler or existing service calls this. Creating, inviting and
updating employees all continue to work exactly as they do today, and every
existing consumer of ``User.employment_type`` is untouched.

TWO PATHS WITH OPPOSITE DISPOSITIONS
------------------------------------
READ (:func:`worker_context_as_of`) never raises. A missing timeline, a date
below the floor, another organization's Worker, a database error — every one of
them returns UNKNOWN, and UNKNOWN means the day is not evaluated. A resolver
that throws while being consulted fails an evaluation for the wrong reason.

WRITE (:func:`record_timeline`, :func:`set_worker_context`) refuses loudly, and
is the ONLY place ``User.employment_type`` may be assigned. That invariant is
what keeps one fact from becoming two sources; an AST guard test enforces it
across the repository.

THE HISTORICAL FLOOR (H-1, approved and locked)
-----------------------------------------------
Before a Worker's earliest captured period the answer is UNKNOWN. Never today's
value, never the latest value, never ``full_time``. Nothing is reconstructed
from WEC, schedules, payroll, audit text or current state — for many Workers the
prior value was destroyed by an unaudited overwrite and is genuinely gone.

The asymmetry that makes the floor safe, in GV-27's own numbers: reading UNKNOWN
when the truth was Part-Time declines to evaluate a day. Reading Full-Time when
the truth was Part-Time computes a shortfall against 525 minutes the Worker
never owed instead of 240.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import (
    InvalidWorkerContext,
    StaleWorkerContextVersion,
    WorkerContextOrganizationRequired,
)
from .models import WorkerContextTimeline
from .vocabulary import (
    VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
    WorkerContextProvenance,
    is_known_vocabulary,
    parse_provenance,
)

_log = logging.getLogger("zoiko.worker_context")


class ContextOutcome(str, enum.Enum):
    """Why a resolution did or did not produce a value."""

    RESOLVED = "RESOLVED"

    UNKNOWN_NO_HISTORY = "UNKNOWN_NO_HISTORY"
    """No timeline was ever captured for this Worker."""

    UNKNOWN_BELOW_FLOOR = "UNKNOWN_BELOW_FLOOR"
    """A timeline exists, but the date precedes its earliest period."""


#: The two outcomes that carry no value. Kept as a set so a caller checks
#: membership rather than enumerating, and a fourth outcome cannot be missed.
UNKNOWN_OUTCOMES = frozenset({
    ContextOutcome.UNKNOWN_NO_HISTORY,
    ContextOutcome.UNKNOWN_BELOW_FLOOR,
})


@dataclass(frozen=True)
class WorkerContextResolution:
    """The answer to "what was this Worker on that date?".

    A non-RESOLVED outcome structurally cannot carry a value — the same
    guarantee Step 5's ``RequirementResolution`` makes about ``required_minutes``.
    It is what stops an UNKNOWN being read as a classification by a caller that
    forgot to check the outcome.
    """

    outcome: ContextOutcome
    organization_id: int
    user_id: int
    as_of: date
    worker_context: Optional[str] = None
    context_vocabulary: Optional[str] = None
    provenance: Optional[str] = None
    effective_from: Optional[date] = None
    timeline_version: Optional[int] = None

    def __post_init__(self) -> None:
        if self.outcome is not ContextOutcome.RESOLVED:
            for field in ("worker_context", "context_vocabulary", "provenance",
                          "effective_from", "timeline_version"):
                if getattr(self, field) is not None:
                    raise InvalidWorkerContext(
                        f"outcome {self.outcome.value} cannot carry {field}; an "
                        f"unresolved worker context has no value"
                    )
        elif self.worker_context is None:
            raise InvalidWorkerContext("RESOLVED requires a worker_context")

    @property
    def is_resolved(self) -> bool:
        return self.outcome is ContextOutcome.RESOLVED

    @property
    def is_unknown(self) -> bool:
        return self.outcome in UNKNOWN_OUTCOMES

    @property
    def is_deliberate(self) -> bool:
        """True only when a person chose this classification.

        A ``CREATION_DEFAULT`` period is resolved and usable as current state,
        but it is not evidence that anybody decided the Worker was full-time.
        """
        return (
            self.is_resolved
            and self.provenance in {
                WorkerContextProvenance.CAPTURED.value,
                WorkerContextProvenance.CORRECTION.value,
            }
        )


@dataclass(frozen=True)
class ContextPeriod:
    """One period offered for recording, before it is stored."""

    effective_from: date
    worker_context: str
    context_vocabulary: str = VOCABULARY_PRODUCT_EMPLOYMENT_TYPE
    provenance: str = WorkerContextProvenance.CAPTURED.value


@dataclass(frozen=True)
class RecordedTimeline:
    """What a write produced."""

    organization_id: int
    user_id: int
    timeline_version: int
    period_count: int
    created: bool
    """False when an identical timeline already stood — a replay, not a
    correction, so no version was appended."""


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


# ── Read path — never raises ────────────────────────────────────────────────
def current_timeline_version(
    db: Session, *, organization_id: int, user_id: int
) -> Optional[int]:
    """The highest recorded timeline version, or ``None`` if never captured."""
    return db.execute(
        select(func.max(WorkerContextTimeline.timeline_version)).where(
            WorkerContextTimeline.organization_id == organization_id,
            WorkerContextTimeline.user_id == user_id,
        )
    ).scalar()


def read_timeline(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    timeline_version: Optional[int] = None,
) -> Tuple[WorkerContextTimeline, ...]:
    """One version's periods, or the current version's when unspecified.

    Deterministically ordered by ``(effective_from, id)``.
    """
    if timeline_version is None:
        timeline_version = current_timeline_version(
            db, organization_id=organization_id, user_id=user_id
        )
        if timeline_version is None:
            return ()
    return tuple(db.execute(
        select(WorkerContextTimeline)
        .where(
            WorkerContextTimeline.organization_id == organization_id,
            WorkerContextTimeline.user_id == user_id,
            WorkerContextTimeline.timeline_version == timeline_version,
        )
        .order_by(WorkerContextTimeline.effective_from, WorkerContextTimeline.id)
    ).scalars().all())


def worker_context_as_of(
    db: Session,
    *,
    organization_id: Optional[int],
    user_id: Optional[int],
    as_of: date | datetime,
    context_vocabulary: str = VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
) -> WorkerContextResolution:
    """The Worker's context on ``as_of``. UNKNOWN unless proven.

    Selection is the latest period whose ``effective_from <= as_of`` within the
    latest timeline version — NEVER the latest period outright. That is what
    makes a future-dated change invisible to today and to every historical date
    before it takes effect.

    Never raises. A database error is logged and returns UNKNOWN.
    """
    on = _as_date(as_of)
    org = organization_id if organization_id is not None else -1
    uid = user_id if user_id is not None else -1

    if organization_id is None or user_id is None:
        return WorkerContextResolution(
            outcome=ContextOutcome.UNKNOWN_NO_HISTORY,
            organization_id=org, user_id=uid, as_of=on,
        )

    try:
        version = current_timeline_version(
            db, organization_id=organization_id, user_id=user_id
        )
        if version is None:
            return WorkerContextResolution(
                outcome=ContextOutcome.UNKNOWN_NO_HISTORY,
                organization_id=organization_id, user_id=user_id, as_of=on,
            )

        row = db.execute(
            select(WorkerContextTimeline)
            .where(
                WorkerContextTimeline.organization_id == organization_id,
                WorkerContextTimeline.user_id == user_id,
                WorkerContextTimeline.timeline_version == version,
                WorkerContextTimeline.context_vocabulary == context_vocabulary,
                WorkerContextTimeline.effective_from <= on,
            )
            # Latest applicable period. ``id`` breaks a tie that the unique
            # constraint already prevents, so the answer is total even if a
            # constraint were somehow absent.
            .order_by(
                WorkerContextTimeline.effective_from.desc(),
                WorkerContextTimeline.id.desc(),
            )
            .limit(1)
        ).scalars().first()
    except Exception:  # pragma: no cover - defensive
        _log.warning(
            "worker_context: could not resolve org=%s user=%s as_of=%s; "
            "treating as UNKNOWN", organization_id, user_id, on, exc_info=True,
        )
        return WorkerContextResolution(
            outcome=ContextOutcome.UNKNOWN_NO_HISTORY,
            organization_id=organization_id, user_id=user_id, as_of=on,
        )

    if row is None:
        # A timeline exists, but this date precedes its earliest period.
        return WorkerContextResolution(
            outcome=ContextOutcome.UNKNOWN_BELOW_FLOOR,
            organization_id=organization_id, user_id=user_id, as_of=on,
        )

    return WorkerContextResolution(
        outcome=ContextOutcome.RESOLVED,
        organization_id=organization_id,
        user_id=user_id,
        as_of=on,
        worker_context=row.worker_context,
        context_vocabulary=row.context_vocabulary,
        provenance=row.provenance,
        effective_from=row.effective_from,
        timeline_version=row.timeline_version,
    )


# ── Write path — the ONLY place User.employment_type may be assigned ────────
def _validate(periods: Sequence[ContextPeriod]) -> List[ContextPeriod]:
    """Reject a timeline that is not storable as evidence. Reports all faults."""
    if not periods:
        raise InvalidWorkerContext(
            "a timeline must contain at least one period; there is no "
            "'withdraw the classification' operation"
        )

    faults: List[str] = []
    seen: dict = {}
    for p in periods:
        key = (p.context_vocabulary, p.effective_from)
        if key in seen:
            faults.append(
                f"two periods claim {p.effective_from.isoformat()} in "
                f"{p.context_vocabulary}"
            )
        seen[key] = True

        if not isinstance(p.worker_context, str) or not p.worker_context.strip():
            faults.append(f"empty worker_context at {p.effective_from}")
        if not is_known_vocabulary(p.context_vocabulary):
            faults.append(
                f"{p.context_vocabulary!r} is not a declared vocabulary; "
                f"declare it rather than creating one by writing to it"
            )
        try:
            parse_provenance(p.provenance)
        except ValueError as exc:
            faults.append(str(exc))
        if not isinstance(p.effective_from, date):
            faults.append(f"effective_from must be a date, got {p.effective_from!r}")

    if faults:
        raise InvalidWorkerContext(
            "worker-context timeline is not storable: " + "; ".join(faults)
        )
    return sorted(periods, key=lambda p: (p.context_vocabulary, p.effective_from))


def _fingerprint(periods) -> Tuple:
    """Order-independent identity of a timeline, for replay detection."""
    return tuple(sorted(
        (
            p.context_vocabulary,
            (p.effective_from.isoformat() if isinstance(p.effective_from, date)
             else str(p.effective_from)),
            p.worker_context,
            str(getattr(p, "provenance", "")),
        )
        for p in periods
    ))


def record_timeline(
    db: Session,
    *,
    user,
    periods: Sequence[ContextPeriod],
    expected_version: Optional[int] = None,
    recorded_by_user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> RecordedTimeline:
    """Append a complete timeline version, and synchronise the projection.

    ``user`` is the ORM row, not an id, because this function must read its
    organization and write its ``employment_type`` in the same transaction.

    ``expected_version`` is the version the caller read; ``None`` means "no
    timeline yet". A mismatch is rejected rather than merged.

    Replaying an identical timeline is a no-op returning the existing version.

    THE PROJECTION RULE: after the write, ``User.employment_type`` is set to
    whatever the PRODUCT vocabulary resolves to TODAY — not to the value just
    supplied. A future-dated change therefore does not move today's projection,
    which is what keeps ``worker_context_as_of(today) == User.employment_type``
    true in every case.

    Raises on every refusal, leaving the transaction for the caller to roll
    back. Nothing is written until every check has passed.
    """
    organization_id = getattr(user, "organization_id", None)
    if organization_id is None:
        raise WorkerContextOrganizationRequired(
            f"user {getattr(user, 'id', None)} has no organization_id; worker "
            f"context will not inherit a NULL tenant"
        )
    user_id = getattr(user, "id", None)
    if user_id is None:
        raise InvalidWorkerContext("user has no id")

    ordered = _validate(periods)
    at = now or datetime.now(timezone.utc)

    existing = current_timeline_version(
        db, organization_id=organization_id, user_id=user_id
    )
    if expected_version != existing:
        raise StaleWorkerContextVersion(
            f"worker {user_id} is at timeline version {existing!r}, but the "
            f"write was computed against {expected_version!r}. Re-read and "
            f"re-run; timelines are never merged."
        )

    if existing is not None:
        stored = read_timeline(
            db, organization_id=organization_id, user_id=user_id,
            timeline_version=existing,
        )
        if _fingerprint(stored) == _fingerprint(ordered):
            return RecordedTimeline(
                organization_id=organization_id, user_id=user_id,
                timeline_version=existing, period_count=len(stored),
                created=False,
            )

    new_version = 1 if existing is None else existing + 1
    for p in ordered:
        db.add(WorkerContextTimeline(
            organization_id=organization_id,
            user_id=user_id,
            timeline_version=new_version,
            effective_from=p.effective_from,
            worker_context=p.worker_context,
            context_vocabulary=p.context_vocabulary,
            provenance=parse_provenance(p.provenance).value,
            recorded_at_utc=at,
            recorded_by_user_id=recorded_by_user_id,
            supersedes_version=existing,
        ))
    db.flush()

    # ── Projection synchronisation, same transaction ────────────────────────
    # The ONLY assignment to User.employment_type in the repository that this
    # package permits. If this raises, the whole transaction rolls back and the
    # projection is left untouched — never advanced without its history.
    today = _as_date(at.astimezone(timezone.utc))
    resolved = worker_context_as_of(
        db, organization_id=organization_id, user_id=user_id, as_of=today,
        context_vocabulary=VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
    )
    if resolved.is_resolved:
        user.employment_type = resolved.worker_context
    db.flush()

    return RecordedTimeline(
        organization_id=organization_id, user_id=user_id,
        timeline_version=new_version, period_count=len(ordered), created=True,
    )


def set_worker_context(
    db: Session,
    *,
    user,
    worker_context: str,
    effective_from: date | datetime,
    expected_version: Optional[int] = None,
    provenance: str = WorkerContextProvenance.CAPTURED.value,
    context_vocabulary: str = VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
    recorded_by_user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> RecordedTimeline:
    """Set the Worker's context from a date, preserving the rest of the timeline.

    Reads the current timeline, replaces or adds the period at
    ``effective_from``, and appends the result as a new version. Every other
    period is carried through unchanged, so a later change never erases an
    earlier one.
    """
    organization_id = getattr(user, "organization_id", None)
    if organization_id is None:
        raise WorkerContextOrganizationRequired(
            f"user {getattr(user, 'id', None)} has no organization_id"
        )

    eff = _as_date(effective_from)
    current = read_timeline(
        db, organization_id=organization_id, user_id=user.id,
        timeline_version=expected_version,
    )

    merged = [
        ContextPeriod(
            effective_from=row.effective_from,
            worker_context=row.worker_context,
            context_vocabulary=row.context_vocabulary,
            provenance=row.provenance,
        )
        for row in current
        if not (row.effective_from == eff
                and row.context_vocabulary == context_vocabulary)
    ]
    merged.append(ContextPeriod(
        effective_from=eff,
        worker_context=worker_context,
        context_vocabulary=context_vocabulary,
        provenance=provenance,
    ))

    return record_timeline(
        db, user=user, periods=merged, expected_version=expected_version,
        recorded_by_user_id=recorded_by_user_id, now=now,
    )


__all__ = [
    "ContextOutcome",
    "UNKNOWN_OUTCOMES",
    "WorkerContextResolution",
    "ContextPeriod",
    "RecordedTimeline",
    "worker_context_as_of",
    "current_timeline_version",
    "read_timeline",
    "record_timeline",
    "set_worker_context",
]
