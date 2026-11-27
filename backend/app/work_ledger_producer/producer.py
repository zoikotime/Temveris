"""
The forward-only Work Ledger producer (§45).
============================================

One entry point, :func:`produce_and_seal_work_day`. It reads authoritative
source evidence, validates it, constructs canonical intervals itself, and seals
through the existing Work Ledger service.

CALLERS CANNOT SUPPLY EVIDENCE
------------------------------
There is no parameter for intervals, for provenance, for a timezone, or for the
cutover instant. A caller that could supply any of those could manufacture
evidence, and the whole point of the ledger is that it cannot be manufactured.
``work_date`` is a LOOKUP TARGET only: every source boundary is independently
verified to belong to that canonical local day.

THREE HISTORICAL GATES
----------------------
Sealing is refused unless all three hold:

1. a durable per-tenant ``producer_cutover_at`` exists (D-18C);
2. the organization timezone is explicitly configured and valid (D-18E);
3. the working-day result came from a real configuration tier, not the
   resolver's system default (D-18D).

The cutover comparison is on the day's organization-local START: a day whose
local midnight precedes the watermark is refused. Existence is checked before
the timezone so an unconfigured tenant reports ``CUTOVER_NOT_SET`` rather than
a timezone problem, but the BEFORE_CUTOVER comparison necessarily follows
timezone resolution, because the local day start is what it compares.

TRANSACTION
-----------
The producer never commits. It writes inside the caller's transaction so a
failed gate or a raised integrity fault leaves nothing partially sealed once
the caller rolls back, and so the seal is atomic with whatever else the caller
is doing.

NOT THE CLASSIFIER
------------------
No ``MISSING_CLOCK_OUT``, no qualification, no worked-time arithmetic, no ECO
outcome, no notification. A day with no eligible evidence is reported
``SOURCE_UNRESOLVED`` and no empty ledger is written — deciding what an
evidence-free day MEANS is the classifier's, gated on the legal matrix.

This boundary is UNCHANGED by owner ruling D2-A, and D13-A does not weaken it.
The producer now seals a non-working day with
``LedgerState.NOT_A_WORKING_DAY``, but it does not DECIDE that: the verdict
comes from ``app.working_day_classifier`` through the frozen ``DayContext``,
and the producer transports it. Freezing a verdict at seal time and deciding
one are different jobs; only the first is the producer's. There is no schedule
read, no weekday arithmetic and no inference from the presence or absence of
work anywhere in this module — an accidental clock-in on a rest day cannot
move the classification (D8-A, D17-A). An ``UNCLASSIFIED`` verdict is refused
by the context gate and never becomes a state.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.work_ledger.service import (
    DayKey,
    LedgerEvidenceConflict,
    LedgerIntegrityFault,
    get_current_ledger,
    record_integrity_fault,
    seal_day,
    supersede_day,
)
from app.departure_resolution import resolve_departure
from app.work_ledger.states import LedgerState, WorkState
from database import models

from .context import DayContext, resolve_day_context
from .cutover import get_producer_cutover
from .evidence import (
    SessionEvidence,
    build_intervals,
    final_departure,
    resolve_session_evidence,
    source_evidence_fingerprint,
)
from .outcomes import ProducerOutcome, ProducerResult, RefusalReason

#: ``created_by`` recorded on ledger versions this producer writes, so a sealed
#: row says which component authored it.
PRODUCER_ACTOR = "WORK_LEDGER_PRODUCER"


def _refused(
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
    reason: RefusalReason,
    detail: Optional[str] = None,
    context: Optional[DayContext] = None,
    cutover_at: Optional[datetime] = None,
) -> ProducerResult:
    return ProducerResult(
        outcome=ProducerOutcome.REFUSED,
        organization_id=organization_id,
        user_id=user_id,
        work_date=work_date,
        refusal=reason,
        detail=detail,
        timezone_iana=context.timezone_iana if context else None,
        utc_offset_seconds=context.utc_offset_seconds if context else None,
        is_working_day=context.is_working_day if context else None,
        working_day_source=context.working_day_source if context else None,
        producer_cutover_at=cutover_at,
    )


class _Decision(enum.Enum):
    """The D18-F3-S comparison outcome for an already-sealed day."""

    IDENTICAL = "IDENTICAL"                # case 1: nothing changed
    SOURCE_CHANGED = "SOURCE_CHANGED"      # case 2: supersede
    CONTEXT_DIVERGED = "CONTEXT_DIVERGED"  # cases 3 and 4: refuse
    #: The sealed version has no persisted source fingerprint, so source
    #: equality cannot be established either way. Fails closed (audit
    #: Finding 1): an impossible comparison is not a difference.
    FINGERPRINT_UNAVAILABLE = "FINGERPRINT_UNAVAILABLE"


def _frozen_context_matches(ledger, context: DayContext) -> bool:
    """Does the sealed version's FROZEN context equal what we just resolved?

    Compares only the frozen context fields. ``workweek_key`` is deliberately
    absent: it is re-derived by ``supersede_day`` and is already governed by
    the existing ledger conflict semantics.
    """
    return (
        ledger.timezone_iana == context.timezone_iana
        and int(ledger.utc_offset_seconds or 0) == int(context.utc_offset_seconds)
        and bool(ledger.is_working_day) == bool(context.is_working_day)
        and ledger.working_day_source == context.working_day_source
    )


def _supersession_decision(ledger, *, context: DayContext,
                           fingerprint: str) -> "_Decision":
    """Apply owner ruling D18-F3-S (Choice C).

    source evidence | frozen context | decision
    --------------- | -------------- | -----------------------
    identical       | identical      | IDENTICAL
    differs         | identical      | SOURCE_CHANGED
    identical       | differs        | CONTEXT_DIVERGED
    differs         | differs        | CONTEXT_DIVERGED
    UNCOMPARABLE    | identical      | FINGERPRINT_UNAVAILABLE
    UNCOMPARABLE    | differs        | CONTEXT_DIVERGED

    Context is checked FIRST, so a divergent context refuses regardless of the
    source. That is what stops today's configuration rewriting a historical
    day: the superseding version would otherwise carry a re-resolved timezone
    and re-attribute the day.

    A sealed version with NO persisted fingerprint is UNCOMPARABLE, not
    changed (audit Finding 1). Only a real comparison between two present
    fingerprints may yield IDENTICAL or SOURCE_CHANGED, so only a genuine
    difference can reach a supersession.
    """
    if not _frozen_context_matches(ledger, context):
        return _Decision.CONTEXT_DIVERGED
    stored = getattr(ledger, "source_fingerprint", None)
    if stored is None:
        # Absence is not evidence of difference. Fail closed rather than
        # supersede a version whose source evidence cannot be compared —
        # including one this producer did not author.
        return _Decision.FINGERPRINT_UNAVAILABLE
    if stored == fingerprint:
        return _Decision.IDENTICAL
    return _Decision.SOURCE_CHANGED


def _context_divergence_detail(ledger, context: DayContext) -> str:
    """Name exactly which frozen field diverged, for the operator."""
    diffs = []
    if ledger.timezone_iana != context.timezone_iana:
        diffs.append(f"timezone_iana {ledger.timezone_iana!r} -> "
                     f"{context.timezone_iana!r}")
    if int(ledger.utc_offset_seconds or 0) != int(context.utc_offset_seconds):
        diffs.append(f"utc_offset_seconds {ledger.utc_offset_seconds} -> "
                     f"{context.utc_offset_seconds}")
    if bool(ledger.is_working_day) != bool(context.is_working_day):
        diffs.append(f"is_working_day {bool(ledger.is_working_day)} -> "
                     f"{bool(context.is_working_day)}")
    if ledger.working_day_source != context.working_day_source:
        diffs.append(f"working_day_source {ledger.working_day_source!r} -> "
                     f"{context.working_day_source!r}")
    return ("sealed frozen context differs: " + "; ".join(diffs)
            if diffs else "sealed frozen context differs")


def _sealed_result(
    *,
    outcome: ProducerOutcome,
    organization_id: int,
    user_id: int,
    work_date: date,
    ledger,
    context: DayContext,
    cutover_at: Optional[datetime],
    intervals,
    superseded_from_version: Optional[int] = None,
) -> ProducerResult:
    """Build the result for a run that ended with a current sealed ledger."""
    counts: dict = {}
    for iv in intervals:
        counts[iv.state] = counts.get(iv.state, 0) + 1
    return ProducerResult(
        outcome=outcome,
        organization_id=organization_id, user_id=user_id,
        work_date=work_date,
        ledger_id=int(ledger.id),
        ledger_version=int(ledger.ledger_version),
        superseded_from_version=superseded_from_version,
        timezone_iana=context.timezone_iana,
        utc_offset_seconds=context.utc_offset_seconds,
        is_working_day=context.is_working_day,
        working_day_source=context.working_day_source,
        producer_cutover_at=cutover_at,
        interval_counts=counts,
        source_refs=tuple(iv.source_ref for iv in intervals if iv.source_ref),
    )


def produce_and_seal_work_day(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
) -> ProducerResult:
    """Produce and seal one Worker-day from verified source evidence.

    Returns a :class:`ProducerResult`; refusals are values, not exceptions.
    Does not commit.
    """
    if isinstance(work_date, datetime):
        work_date = work_date.date()

    worker = (
        db.query(models.User)
        .filter(models.User.id == user_id)
        .one_or_none()
    )
    if worker is None or worker.organization_id is None or \
            int(worker.organization_id) != int(organization_id):
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.CROSS_ORG_MISMATCH,
            detail="the Worker does not belong to this organization",
        )

    # ── Gate 1: a durable cutover must exist ────────────────────────────────
    cutover_at = get_producer_cutover(db, organization_id)
    if cutover_at is None:
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.CUTOVER_NOT_SET,
            detail="no durable producer cutover for this organization; a "
                   "tenant with no watermark is refused, never backfilled",
        )

    # ── Gate 2/3: timezone and working day, both strict ─────────────────────
    context = resolve_day_context(
        db, organization_id=organization_id, user=worker, work_date=work_date)
    if isinstance(context, RefusalReason):
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=context, cutover_at=cutover_at,
        )

    # ── Forward-only: the day's local START must be at or after the mark ────
    if context.day_start_utc < cutover_at:
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.BEFORE_CUTOVER,
            detail=(f"local day starts {context.day_start_utc.isoformat()}, "
                    f"cutover is {cutover_at.isoformat()}"),
            context=context, cutover_at=cutover_at,
        )

    # ── D18-F3 gate 1: the organization-local day must have ENDED ───────────
    # Sealing a day that can still accrue work produces partial evidence, and
    # because the producer's correction path is bounded, the rest of that day
    # could then never be sealed. `day_end_utc` is the exclusive next local
    # midnight from `_tz.local_day_bounds`, so this is DST-correct and is not
    # server date, UTC midnight or `date.today()`. Equality is permitted: the
    # day is over at that instant under the half-open [start, end) convention.
    # A future work_date lands here too.
    now_utc = datetime.now(timezone.utc)
    if now_utc < context.day_end_utc:
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.DAY_NOT_ENDED,
            detail=(f"local day ends {context.day_end_utc.isoformat()}, "
                    f"now is {now_utc.isoformat()}"),
            context=context, cutover_at=cutover_at,
        )

    # ── D18-F3 gate 2: no open session may still be attributed to the day ───
    # `clock_in_time < day_end_utc` catches a session opened on an earlier day
    # and still open, which would leave this day unresolved.
    still_open = (
        db.query(models.ClockSession.id)
        .filter(
            models.ClockSession.organization_id == organization_id,
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_out_time.is_(None),
            models.ClockSession.clock_in_time < context.day_end_utc,
        )
        .first()
    )
    # ── §24.6: ask the departure primitive, then TRANSPORT its verdict ──────
    # Reached only after gate 1, so the local day has already ended — which is
    # the authorized closure point Ruling 5 requires and the reason a still-open
    # session here means "never clocked out" rather than "still working".
    #
    # The producer does not decide that. It asks `departure_resolution`, whose
    # job this is (Ruling 4), and carries the answer into the ledger exactly as
    # it already carries the working-day verdict from the classifier. D2-A and
    # D13-A are untouched: freezing a verdict is the producer's job, forming one
    # is not.
    departure_verdict = resolve_departure(
        db, organization_id=organization_id, user_id=user_id,
        context=context, day_closed=True,
    )

    if departure_verdict.is_missing_clock_out:
        transported = _seal_missing_clock_out(
            db, organization_id=organization_id, user_id=user_id,
            work_date=work_date, context=context, cutover_at=cutover_at,
            verdict=departure_verdict,
        )
        # None means "a version already exists in another state" — the
        # established supersession path owns that case and is left untouched.
        if transported is not None:
            return transported

    # A session opened on an EARLIER day and still open is not this day's
    # missing clock-out — it belongs to the day it started on (§12.3). The
    # existing refusal stands, so a defect is never re-homed onto this date.
    if still_open is not None:
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.SESSION_STILL_OPEN,
            detail=f"clock_session:{still_open[0]} is still open",
            context=context, cutover_at=cutover_at,
        )

    # ── Source sessions, attributed by START_DATE ───────────────────────────
    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.organization_id == organization_id,
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_in_time >= context.day_start_utc,
            models.ClockSession.clock_in_time < context.day_end_utc,
        )
        .order_by(models.ClockSession.clock_in_time, models.ClockSession.id)
        .all()
    )
    if not sessions:
        return ProducerResult(
            outcome=ProducerOutcome.SOURCE_UNRESOLVED,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date,
            detail="no clock session opens on this organization-local day",
            timezone_iana=context.timezone_iana,
            utc_offset_seconds=context.utc_offset_seconds,
            is_working_day=context.is_working_day,
            working_day_source=context.working_day_source,
            producer_cutover_at=cutover_at,
        )

    # §24.7: when the departure primitive has ALREADY ruled this day an
    # unverified exit, the evidence layer is asked for the session as it
    # actually is — synthesized close included — so the day can be sealed and
    # deferred (§19 step 5 needs the intervals). The producer is not deciding
    # anything here: it is passing on the verdict it was handed. The default
    # stays False everywhere else, so no other path can seal an unverified
    # close by accident.
    #
    # FIRST SEALING ONLY. If a version already exists it is left alone, and the
    # established refusal stands. §24.7 describes a day moving from unverified
    # towards resolved; letting a synthesized close SUPERSEDE an already
    # verified sealed day would run that arrow backwards — quietly deferring a
    # settled day, withdrawing its contribution from a completed week. That is
    # a supersession-semantics change, and D18-F3-S was explicitly preserved.
    unverified_exit = (
        departure_verdict.is_unverified_exit
        and get_current_ledger(db, DayKey(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date)) is None
    )

    evidence: List[SessionEvidence] = []
    for session in sessions:
        resolved = resolve_session_evidence(
            db, session=session, organization_id=organization_id,
            user_id=user_id, allow_unverified_closure=unverified_exit)
        if isinstance(resolved, RefusalReason):
            # One inadmissible session refuses the whole day. A partial day
            # would be evidence that silently omits work.
            return _refused(
                organization_id=organization_id, user_id=user_id,
                work_date=work_date, reason=resolved,
                detail=f"clock_session:{session.id}",
                context=context, cutover_at=cutover_at,
            )
        # The query already scopes by the local day, but attribution is
        # START_DATE and the ledger key is derived from it, so the boundary is
        # verified rather than assumed.
        if not context.contains(resolved.clock_in_at):
            return _refused(
                organization_id=organization_id, user_id=user_id,
                work_date=work_date, reason=RefusalReason.WORK_DATE_MISMATCH,
                detail=f"clock_session:{session.id}",
                context=context, cutover_at=cutover_at,
            )
        evidence.append(resolved)

    intervals = build_intervals(evidence)
    key = DayKey(organization_id=organization_id, user_id=user_id,
                 work_date=work_date)
    pre_existing = get_current_ledger(db, key)

    # ── D18-F2: the day's FINAL verified departure ──────────────────────────
    # Recorded at day level because the WRK segment that would terminate at
    # the clock-out can legitimately be zero-length and absent. Every session
    # reaching here closed with a VERIFIED provenance, so this is never a
    # synthesized closure and is never fabricated.
    departure = final_departure(evidence)
    if departure is None:
        # Unreachable while `sessions` is non-empty, but a producer-sealed row
        # must never lack departure evidence, so this fails closed rather than
        # sealing without it.
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.BOUNDARY_PROVENANCE_MISSING,
            detail="no verified departure among the day's eligible sessions",
            context=context, cutover_at=cutover_at,
        )

    # ── D18-F3-S: the SOURCE-evidence fingerprint ───────────────────────────
    fingerprint = source_evidence_fingerprint(evidence)

    # ── D13-A: the classifier's verdict, PASSED THROUGH ─────────────────────
    # The classifier already decided whether work was expected; `context`
    # carries that frozen verdict. This line TRANSPORTS it into the ledger
    # state and interprets nothing: there is no schedule read here, no
    # weekday arithmetic, no inference from whether any work exists. The
    # producer remains NOT THE CLASSIFIER (D2-A) — it is the only component
    # positioned to freeze the verdict at seal time, which is a different job
    # from deciding it.
    #
    # An UNCLASSIFIED verdict never reaches this point: `resolve_day_context`
    # refused above and nothing was written (D8-A).
    seal_state = (LedgerState.SEALED if context.is_working_day
                  else LedgerState.NOT_A_WORKING_DAY)

    seal_kwargs = dict(
        intervals=intervals,
        timezone_iana=context.timezone_iana,
        utc_offset_seconds=context.utc_offset_seconds,
        is_working_day=context.is_working_day,
        working_day_source=context.working_day_source,
        # D-1.1: frozen from the SAME resolution as the working-day verdict.
        # The producer never calls the schedule resolver itself; both halves
        # arrive on the DayContext from one classifier call.
        scheduled_end_local=context.scheduled_end_local,
        scheduled_end_source=context.scheduled_end_source,
        departure_at=departure.clock_out_at,
        departure_provenance=departure.close_provenance.value,
        departure_source_ref=departure.departure_source_ref,
        source_fingerprint=fingerprint,
        state=seal_state,
        created_by=PRODUCER_ACTOR,
    )

    # ── D18-F3-S: bounded supersession (owner ruling Choice C) ──────────────
    if pre_existing is not None:
        decision = _supersession_decision(
            pre_existing, context=context, fingerprint=fingerprint)
        if decision is _Decision.CONTEXT_DIVERGED:
            # Cases 3 and 4. A context change may never trigger or accompany a
            # supersession: the new version would carry today's configuration
            # and retroactively re-attribute a historical day. Nothing is
            # mutated and the sealed frozen context stays authoritative.
            return _refused(
                organization_id=organization_id, user_id=user_id,
                work_date=work_date,
                reason=RefusalReason.FROZEN_CONTEXT_DIVERGENCE,
                detail=_context_divergence_detail(pre_existing, context),
                context=context, cutover_at=cutover_at,
            )
        if decision is _Decision.FINGERPRINT_UNAVAILABLE:
            # Audit Finding 1. The frozen context matches, but the sealed
            # version carries no fingerprint, so whether the source evidence
            # changed cannot be established. D18-F3-S allows supersession only
            # on a genuine DIFFERENCE, so this refuses and writes nothing.
            return _refused(
                organization_id=organization_id, user_id=user_id,
                work_date=work_date,
                reason=RefusalReason.SOURCE_FINGERPRINT_UNAVAILABLE,
                detail=(
                    f"ledger v{pre_existing.ledger_version} has no persisted "
                    f"source fingerprint; source equality cannot be "
                    f"established, so it is not superseded"
                ),
                context=context, cutover_at=cutover_at,
            )
        if decision is _Decision.IDENTICAL:
            # Case 1. Nothing written.
            return _sealed_result(
                outcome=ProducerOutcome.ALREADY_SEALED,
                organization_id=organization_id, user_id=user_id,
                work_date=work_date, ledger=pre_existing, context=context,
                cutover_at=cutover_at, intervals=intervals,
            )
        # Case 2: source differs, frozen context identical -> supersede.
        try:
            new_version = supersede_day(
                db, key,
                expected_version=int(pre_existing.ledger_version),
                **seal_kwargs,
            )
        except LedgerIntegrityFault as fault:
            # A correction whose evidence is inadmissible is refused; the
            # sealed version is left exactly as it was.
            return ProducerResult(
                outcome=ProducerOutcome.LEDGER_INTEGRITY_FAULT,
                organization_id=organization_id, user_id=user_id,
                work_date=work_date, detail=fault.result.fault_summary,
                timezone_iana=context.timezone_iana,
                utc_offset_seconds=context.utc_offset_seconds,
                is_working_day=context.is_working_day,
                working_day_source=context.working_day_source,
                producer_cutover_at=cutover_at,
            )
        return _sealed_result(
            outcome=ProducerOutcome.SUPERSEDED,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, ledger=new_version, context=context,
            cutover_at=cutover_at, intervals=intervals,
            superseded_from_version=int(pre_existing.ledger_version),
        )

    try:
        ledger = seal_day(db, key, **seal_kwargs)
    except LedgerIntegrityFault as fault:
        # §24.15: refused, never repaired. The fault is recorded so the blocked
        # day is explainable — but only when no sealed version already exists;
        # replacing one is a supersession decision, not the producer's.
        fault_row = None
        if pre_existing is None:
            # A fault day has no verified departure and no source evidence
            # worth pinning: NULL is the truthful value for both. The SCHEDULE
            # evidence is different in kind — it describes what the day was
            # expected to be, not what the (inadmissible) evidence says
            # happened — so it is frozen here too, and an operator reading a
            # blocked day can still see the schedule it was measured against.
            fault_row = record_integrity_fault(
                db, key,
                reason=fault.result.fault_summary,
                timezone_iana=context.timezone_iana,
                utc_offset_seconds=context.utc_offset_seconds,
                is_working_day=context.is_working_day,
                working_day_source=context.working_day_source,
                scheduled_end_local=context.scheduled_end_local,
                scheduled_end_source=context.scheduled_end_source,
                created_by=PRODUCER_ACTOR,
            )
        return ProducerResult(
            outcome=ProducerOutcome.LEDGER_INTEGRITY_FAULT,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date,
            detail=fault.result.fault_summary,
            timezone_iana=context.timezone_iana,
            utc_offset_seconds=context.utc_offset_seconds,
            is_working_day=context.is_working_day,
            working_day_source=context.working_day_source,
            producer_cutover_at=cutover_at,
            # Finding 4: the recorded fault row is addressable. `ledger_id`
            # stays absent, so `sealed` and `has_ledger` remain False.
            integrity_fault_ledger_id=(
                int(fault_row.id) if fault_row is not None else None),
        )
    except LedgerEvidenceConflict as conflict:
        return _refused(
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, reason=RefusalReason.EVIDENCE_CONFLICT,
            detail=str(conflict), context=context, cutover_at=cutover_at,
        )

    return _sealed_result(
        outcome=ProducerOutcome.SEALED,
        organization_id=organization_id, user_id=user_id,
        work_date=work_date, ledger=ledger, context=context,
        cutover_at=cutover_at, intervals=intervals,
    )


def _seal_missing_clock_out(
    db: Session, *, organization_id, user_id, work_date, context, cutover_at,
    verdict,
) -> Optional["ProducerResult"]:
    """Transport a MISSING_CLOCK_OUT verdict into the ledger (§24.6, Ruling 7).

    Returns ``None`` when a version already exists in another state, so the
    caller falls through to the established supersession path untouched.

    Every field is either frozen context or NULL. Nothing is computed and
    nothing is inferred:

    * **zero intervals** — an interval needs an end, and the end is precisely
      what does not exist. Manufacturing one would be the §7.2 fabrication;
    * **no departure fields** — ``departure_at``, ``departure_provenance`` and
      ``departure_source_ref`` stay NULL, which the table's own all-or-none
      CHECK already permits and which keeps "no verified departure" truthful
      rather than merely unrecorded;
    * **no source fingerprint** — there is no admissible source evidence to
      pin, and NULL is the honest value.

    A day already sealed is left alone. Correcting an existing version is a
    supersession decision, which D18-F3-S reserves to the bounded path above
    and which is not this verdict's to make — the same restraint
    ``record_integrity_fault`` observes.
    """
    key = DayKey(organization_id=organization_id, user_id=user_id,
                 work_date=work_date)
    pre_existing = get_current_ledger(db, key)
    if pre_existing is not None:
        if pre_existing.state == LedgerState.MISSING_CLOCK_OUT.value:
            # Idempotent replay: the day already records exactly this verdict.
            return _sealed_result(
                outcome=ProducerOutcome.ALREADY_SEALED,
                organization_id=organization_id, user_id=user_id,
                work_date=work_date, ledger=pre_existing, context=context,
                cutover_at=cutover_at, intervals=[],
            )
        # A day already sealed in some OTHER state is a supersession question,
        # and Ruling 9 leaves supersession semantics alone. Returning None hands
        # control back to the established path, which keeps its own refusal —
        # and its own accurate refusal REASON. Inventing a reason here would
        # report a synthesized closure as a still-open session.
        return None

    ledger = seal_day(
        db, key,
        intervals=(),
        timezone_iana=context.timezone_iana,
        utc_offset_seconds=context.utc_offset_seconds,
        is_working_day=context.is_working_day,
        working_day_source=context.working_day_source,
        # D-1.1: the schedule evidence is frozen even here. A day with no
        # clock-out still HAD a scheduled end, and freezing it is what lets a
        # later correction be measured against the schedule that governed the
        # day rather than the roster in force when the correction is made.
        scheduled_end_local=context.scheduled_end_local,
        scheduled_end_source=context.scheduled_end_source,
        # NULL, deliberately. §24.6: the system MUST NEVER assume a departure.
        departure_at=None,
        departure_provenance=None,
        departure_source_ref=None,
        source_fingerprint=None,
        state=LedgerState.MISSING_CLOCK_OUT,
        created_by=PRODUCER_ACTOR,
    )
    return _sealed_result(
        outcome=ProducerOutcome.SEALED,
        organization_id=organization_id, user_id=user_id,
        work_date=work_date, ledger=ledger, context=context,
        cutover_at=cutover_at, intervals=[],
    )


__all__ = [
    "produce_and_seal_work_day",
    "PRODUCER_ACTOR",
]
