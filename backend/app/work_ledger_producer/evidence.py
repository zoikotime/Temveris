"""
Source evidence: D-15 boundaries in, canonical intervals out.
=============================================================

WHAT IS AUTHORITATIVE
---------------------
A ``ClockSession``'s two boundary INSTANTS, each one gated on a D-15
``clock_session_boundary_event``. A timestamp with no recorded origin is
insufficient and the origin is never inferred — not from the endpoint, not from
the session's shape, not from ``total_seconds``, and never from the value of
the timestamp itself. The audit that produced D-15 showed why: the abandoned
sweep closes at ``max(last_heartbeat, clock_in)``, so a synthesized closure can
land exactly on the clock-in instant and is indistinguishable BY VALUE from an
instant Worker clock-out.

WHAT IS NEVER READ
------------------
``ClockSession.total_seconds``, ``break_seconds``, ``offline_seconds``,
``is_active``; ``BreakSession.break_seconds``/``break_type``/``status``;
``OfflineInterval.claimed_seconds``/``applied_seconds``/``capped``/
``cap_reason``; ``TimeAdjustment`` in any form; ``ShiftAssignment`` as an
attendance fact. Cached totals are a derived convenience for dashboards, not
evidence, and D-19 keeps adjustments out of the evidence model except through
the ``ADMIN_OVERRIDE`` boundary event D-15 already records.

INTERVAL PROVENANCE
-------------------
Per-interval provenance answers "how did the boundary that ENDED this interval
come to exist?" — which is the reading ``provenance.has_verified_departure_
evidence`` requires when it says an interval "was bounded by a real departure".
So:

* the WRK segment that ends at the session's clock-out carries that boundary's
  own verified provenance (``WORKER`` or ``ADMIN_OVERRIDE``);
* every segment that ends at a producer-authored carve-out — a complete break,
  or an observed connectivity gap — carries ``PRODUCER_DERIVED`` (D-18A);
* every ``OFF`` interval carries ``PRODUCER_DERIVED`` (D-18B).

NOTHING IS REPAIRED
-------------------
No clamping, no union, no truncation, no reordering into validity, no silently
skipped row. Every malformed or ambiguous source record refuses the whole day.

STORED-INSTANT AWARENESS
------------------------
Stored instants are coerced to aware UTC on read, following the established
convention in ``app/work_ledger/service._utc`` and ``app/_tz.to_local``:
pysqlite drops ``tzinfo`` while PostgreSQL preserves it, and comparing
representations rather than instants would make eligibility differ between
backends. Awareness is then enforced on the intervals the producer EMITS, by
``validate_intervals``, which is the guard that actually protects the ledger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple, Union

from sqlalchemy.orm import Session

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    ClockSessionBoundaryEvent,
)
from app.work_ledger.integrity import CandidateInterval
from app.work_ledger.states import (
    Provenance,
    SYNTHESIZED_CLOSURE_PROVENANCE,
    VERIFIED_DEPARTURE_PROVENANCE,
    WorkState,
    parse_provenance,
)
from database import models

from .context import DayContext
from .outcomes import RefusalReason


def _utc(value) -> Optional[datetime]:
    """Coerce a stored instant to aware UTC, or None if it is not a datetime."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Window:
    """A half-open carve-out of a working session."""

    start: datetime
    end: datetime
    kind: str            # "break" | "offline"
    source_ref: str


@dataclass(frozen=True)
class SessionEvidence:
    """One verified, closed session with both boundaries provenance-backed."""

    session_id: int
    clock_in_at: datetime
    clock_out_at: datetime
    open_provenance: Provenance
    close_provenance: Provenance
    windows: Tuple[Window, ...]
    #: id of the authoritative D-15 close event, for D18-F2's
    #: ``departure_source_ref``. Identifies WHICH event authorised the close,
    #: not merely that one existed.
    close_event_id: Optional[int] = None

    @property
    def source_ref(self) -> str:
        return f"clock_session:{self.session_id}"

    @property
    def departure_source_ref(self) -> str:
        """Opaque, auditable reference to the authoritative close event."""
        return (f"clock_session:{self.session_id}"
                f"/boundary_event:{self.close_event_id}")


# ═════════════════════════════════════════════════════════════════════════════
# D-15 boundary events
# ═════════════════════════════════════════════════════════════════════════════

def _authoritative_boundary(
    events: Sequence[ClockSessionBoundaryEvent],
    *,
    boundary: str,
    session_id: int,
    user_id: int,
    column_instant: Optional[datetime],
) -> Union[Tuple[Provenance, int], RefusalReason]:
    """The provenance of one boundary, or the reason it cannot be trusted.

    The highest ``sequence`` for a ``(session, boundary)`` is authoritative: a
    later boundary write appends, so the latest event describes the value the
    session row currently holds. Ordering must be total — a repeated sequence
    means the history cannot be read deterministically and the day is refused
    rather than a winner being picked.
    """
    mine = [e for e in events if e.boundary == boundary]
    if not mine:
        return RefusalReason.BOUNDARY_PROVENANCE_MISSING

    sequences = [int(e.sequence) for e in mine]
    if len(set(sequences)) != len(sequences):
        return RefusalReason.BOUNDARY_PROVENANCE_AMBIGUOUS

    latest = max(mine, key=lambda e: int(e.sequence))

    if int(latest.session_id) != int(session_id):
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH
    if int(latest.user_id) != int(user_id):
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH

    recorded = _utc(latest.boundary_at)
    if recorded is None or column_instant is None or recorded != column_instant:
        # The event must describe the instant the session row actually holds.
        # A divergence means the row was written by a path that did not record
        # its origin, which is precisely what D-15 exists to make impossible.
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH

    try:
        return parse_provenance(latest.provenance), int(latest.id)
    except ValueError:
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH


def _carve_out_windows(
    db: Session,
    *,
    session: models.ClockSession,
    user_id: int,
    clock_in_at: datetime,
    clock_out_at: datetime,
) -> Union[Tuple[Window, ...], RefusalReason]:
    """Complete breaks and observed offline gaps, validated, never repaired."""
    windows: List[Window] = []

    breaks = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.session_id == session.id)
        .order_by(models.BreakSession.break_start, models.BreakSession.id)
        .all()
    )
    for row in breaks:
        if int(row.user_id) != int(user_id):
            return RefusalReason.CROSS_WORKER_MISMATCH
        start, end = _utc(row.break_start), _utc(row.break_end)
        if start is None:
            return RefusalReason.BREAK_MALFORMED
        if end is None:
            # Closure cannot be established. Refused, not inferred: the day
            # would otherwise credit work through an unbounded break.
            return RefusalReason.BREAK_OPEN
        if end <= start:
            return RefusalReason.BREAK_MALFORMED
        if start < clock_in_at or end > clock_out_at:
            # No clamping. A break outside the session it belongs to is a
            # malformed source record, not something to trim to fit.
            return RefusalReason.BREAK_MALFORMED
        windows.append(Window(start, end, "break", f"break_session:{row.id}"))

    offlines = (
        db.query(models.OfflineInterval)
        .filter(models.OfflineInterval.session_id == session.id)
        .order_by(models.OfflineInterval.offline_start,
                  models.OfflineInterval.id)
        .all()
    )
    for row in offlines:
        if int(row.user_id) != int(user_id):
            return RefusalReason.CROSS_WORKER_MISMATCH
        # The APPLIED window, read unaltered. claimed_seconds, applied_seconds,
        # capped and cap_reason are deliberately not consulted.
        start, end = _utc(row.offline_start), _utc(row.offline_end)
        if start is None or end is None:
            return RefusalReason.OFFLINE_MALFORMED
        if end <= start:
            return RefusalReason.OFFLINE_MALFORMED
        if start < clock_in_at or end > clock_out_at:
            return RefusalReason.OFFLINE_MALFORMED
        windows.append(
            Window(start, end, "offline", f"offline_interval:{row.id}"))

    windows.sort(key=lambda w: (w.start, w.end, w.source_ref))
    for earlier, later in zip(windows, windows[1:]):
        if later.start < earlier.end:
            # Overlapping carve-outs are not unioned. Which one is right is a
            # source-data question the producer must not answer.
            return (RefusalReason.OFFLINE_MALFORMED
                    if "offline" in (earlier.kind, later.kind)
                    else RefusalReason.BREAK_MALFORMED)

    return tuple(windows)


def resolve_session_evidence(
    db: Session,
    *,
    session: models.ClockSession,
    organization_id: int,
    user_id: int,
    allow_unverified_closure: bool = False,
) -> Union[SessionEvidence, RefusalReason]:
    """Validate one session end to end, or refuse with a reason.

    ``allow_unverified_closure`` — DEFAULT FALSE, and the default is the whole
    point. Every existing caller keeps the behaviour it had: a synthesized
    close is refused with ``SESSION_SYNTHESIZED_CLOSURE`` and never becomes
    sealable evidence.

    It is set True only by the producer, and only after
    ``departure_resolution`` has already returned ``UNVERIFIED_EXIT`` for the
    day — i.e. after the semantic decision was made elsewhere. The flag does
    not decide anything; it says "I already know this exit is unverified, give
    me the evidence so I can seal and defer it" (§24.7, §19 step 5).

    What it does NOT do is make the closure verified. The returned
    ``SessionEvidence`` carries its real ``close_provenance``, so every caller
    testing ``VERIFIED_DEPARTURE_PROVENANCE`` still sees a synthesized close
    for what it is.
    """
    if session.organization_id is None or \
            int(session.organization_id) != int(organization_id):
        return RefusalReason.CROSS_ORG_MISMATCH
    if int(session.user_id) != int(user_id):
        return RefusalReason.CROSS_WORKER_MISMATCH

    clock_in_at = _utc(session.clock_in_time)
    if clock_in_at is None:
        return RefusalReason.NAIVE_TIMESTAMP
    clock_out_at = _utc(session.clock_out_time)
    if clock_out_at is None:
        # Open or ongoing. Never sealed as complete; no departure is invented.
        return RefusalReason.SESSION_OPEN
    if clock_out_at <= clock_in_at:
        return RefusalReason.NON_POSITIVE_INTERVAL

    events = (
        db.query(ClockSessionBoundaryEvent)
        .filter(ClockSessionBoundaryEvent.session_id == session.id)
        .all()
    )

    open_resolved = _authoritative_boundary(
        events, boundary=BOUNDARY_CLOCK_IN, session_id=session.id,
        user_id=user_id, column_instant=clock_in_at)
    if isinstance(open_resolved, RefusalReason):
        return open_resolved
    open_prov, _open_event_id = open_resolved

    close_resolved = _authoritative_boundary(
        events, boundary=BOUNDARY_CLOCK_OUT, session_id=session.id,
        user_id=user_id, column_instant=clock_out_at)
    if isinstance(close_resolved, RefusalReason):
        return close_resolved
    close_prov, close_event_id = close_resolved

    # D-17. A synthesized closure is provenance-bearing evidence but not a
    # verified departure. It is refused here by default and relabelled never —
    # what such a day BECOMES is `departure_resolution`'s to say, and it now
    # says §24.7: sealed, with this very provenance, and deferred. When that
    # verdict has already been reached, the caller opts in and the evidence is
    # returned carrying its true close provenance rather than a borrowed one.
    if close_prov in SYNTHESIZED_CLOSURE_PROVENANCE \
            and not allow_unverified_closure:
        return RefusalReason.SESSION_SYNTHESIZED_CLOSURE
    # An unrecognised provenance is still a defect either way: opting in admits
    # the three NAMED synthesized values, never an arbitrary one.
    permitted_close = (
        VERIFIED_DEPARTURE_PROVENANCE | SYNTHESIZED_CLOSURE_PROVENANCE
        if allow_unverified_closure else VERIFIED_DEPARTURE_PROVENANCE
    )
    if close_prov not in permitted_close:
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH
    if open_prov not in VERIFIED_DEPARTURE_PROVENANCE:
        # D-15 makes this structurally impossible on the open boundary; checked
        # anyway so a future schema slip cannot pass silently.
        return RefusalReason.BOUNDARY_PROVENANCE_MISMATCH

    windows = _carve_out_windows(
        db, session=session, user_id=user_id,
        clock_in_at=clock_in_at, clock_out_at=clock_out_at)
    if isinstance(windows, RefusalReason):
        return windows

    return SessionEvidence(
        session_id=int(session.id),
        clock_in_at=clock_in_at,
        clock_out_at=clock_out_at,
        open_provenance=open_prov,
        close_provenance=close_prov,
        windows=windows,
        close_event_id=close_event_id,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Canonical intervals
# ═════════════════════════════════════════════════════════════════════════════

def build_intervals(
    evidence: Sequence[SessionEvidence],
) -> List[CandidateInterval]:
    """Canonical, non-overlapping intervals for a day's verified sessions.

    WRK is the session window minus its carve-outs. A break window is left as a
    GAP — D-18 forbids the producer from classifying ``"break"``/``"lunch"``
    into ``BRK_M``/``BRK_R``, and gaps are how a non-qualifying period is
    represented. An offline window becomes an ``OFF`` interval, which is why it
    is carved out too: emitting OFF over an unsplit WRK would overlap, and the
    ledger refuses overlap rather than merging it.

    A carve-out flush against a boundary yields a zero-length segment. That is
    the ABSENCE of a work interval, not a discarded one, so none is emitted —
    consistent with the ledger's rule that an empty set is admissible.
    """
    out: List[CandidateInterval] = []

    for ev in evidence:
        cursor = ev.clock_in_at
        for window in ev.windows:
            if window.start > cursor:
                out.append(CandidateInterval(
                    seq=0,
                    state=WorkState.WRK.value,
                    start_at_utc=cursor,
                    end_at_utc=window.start,
                    # Ends at a producer-authored carve-out, not a departure.
                    provenance=Provenance.PRODUCER_DERIVED.value,
                    source_ref=f"{ev.source_ref}/{window.source_ref}",
                ))
            if window.kind == "offline":
                out.append(CandidateInterval(
                    seq=0,
                    state=WorkState.OFF.value,
                    start_at_utc=window.start,
                    end_at_utc=window.end,
                    provenance=Provenance.PRODUCER_DERIVED.value,
                    source_ref=window.source_ref,
                ))
            cursor = max(cursor, window.end)

        if ev.clock_out_at > cursor:
            out.append(CandidateInterval(
                seq=0,
                state=WorkState.WRK.value,
                start_at_utc=cursor,
                end_at_utc=ev.clock_out_at,
                # Ends at the session's own verified departure, so it carries
                # that boundary's provenance verbatim.
                provenance=ev.close_provenance.value,
                source_ref=ev.source_ref,
            ))

    out.sort(key=lambda i: (i.start_at_utc, i.end_at_utc, i.state))
    return [
        CandidateInterval(
            seq=n,
            state=i.state,
            start_at_utc=i.start_at_utc,
            end_at_utc=i.end_at_utc,
            provenance=i.provenance,
            source_ref=i.source_ref,
        )
        for n, i in enumerate(out, start=1)
    ]


# ═════════════════════════════════════════════════════════════════════════════
# D18-F3-S — the SOURCE-evidence fingerprint
# ═════════════════════════════════════════════════════════════════════════════

def source_evidence_fingerprint(evidence: Sequence[SessionEvidence]) -> str:
    """A deterministic digest of the SOURCE records a version was built from.

    Owner ruling D18-F3-S separates a SOURCE change, which may supersede a
    sealed day, from a CONTEXT change, which must refuse. This digest is the
    source side of that comparison, and it covers exactly the dimensions the
    ruling names:

    * eligible session identity and set
    * D-15 verified open and close boundary instants
    * D-15 verified open and close provenance
    * complete break windows
    * applied offline windows

    It DELIBERATELY EXCLUDES resolved context — timezone, UTC offset,
    working-day result, working-day source, workweek key, the current time,
    resolved requirement, leave, and any schedule value beyond the frozen
    working-day fact. Those are compared separately against the sealed row.

    WHY NOT THE EXISTING INTERVAL FINGERPRINT
    -----------------------------------------
    ``work_ledger.service._evidence_fingerprint`` digests CANONICAL INTERVALS,
    which are derived. It is simultaneously blind to some source-identity
    changes that yield identical intervals, and sensitive to context changes,
    because the resolved day window decides which sessions become intervals at
    all. It therefore cannot make the distinction this ruling requires, which
    is why a separate digest is persisted.

    Determinism: sessions are ordered by id, windows by their already-canonical
    sort, instants are normalised to aware UTC and rendered ISO-8601, and the
    JSON is emitted with fixed separators and no key sorting ambiguity (the
    structure is positional). The same source evidence always yields the same
    digest, on either dialect.
    """
    payload = [
        [
            ev.session_id,
            _iso(ev.clock_in_at),
            ev.open_provenance.value,
            _iso(ev.clock_out_at),
            ev.close_provenance.value,
            [
                [w.kind, w.source_ref, _iso(w.start), _iso(w.end)]
                for w in sorted(ev.windows,
                                key=lambda x: (x.start, x.end, x.source_ref))
            ],
        ]
        for ev in sorted(evidence, key=lambda e: e.session_id)
    ]
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    """An instant as aware-UTC ISO-8601, so the digest is dialect-stable."""
    return value.astimezone(timezone.utc).isoformat()


def final_departure(
    evidence: Sequence[SessionEvidence],
) -> Optional[SessionEvidence]:
    """The day's FINAL departure (D18-F2), or None if there is none.

    The latest ``clock_out_at`` among the eligible sessions. Ties break on
    session id so the choice is deterministic.

    It selects; it does not judge. Ordinarily every session reaching here
    closed with VERIFIED provenance, so the result is a verified departure. On
    the §24.7 path the caller opted into unverified closures, so the result may
    carry synthesized provenance — and carries it truthfully, as
    ``close_provenance``, for the ledger to record. Nothing here relabels a
    closure or promotes one to verified.
    """
    if not evidence:
        return None
    return max(evidence, key=lambda e: (e.clock_out_at, e.session_id))


__all__ = [
    "Window",
    "SessionEvidence",
    "resolve_session_evidence",
    "build_intervals",
    "source_evidence_fingerprint",
    "final_departure",
]
