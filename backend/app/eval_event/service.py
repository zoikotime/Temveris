"""
The EVAL-EVENT entry point: the deterministic threshold lifecycle.
==================================================================

    evaluate_threshold_event(db, organization_id=..., user_id=...,
                             workweek_key=..., mode=...)

Consumes the authoritative ``W`` from EVAL-WEEK and the authoritative ``T``
from the threshold reader, compares them, and appends at most one row.

THE COMPARISON IS ``W >= T`` (§21.1)
------------------------------------
Equality crosses. "Exactly 30 triggers; strictly-greater-than is a defect."

WHAT THIS MODULE DOES NOT DO (D7)
---------------------------------
No notification, no delivery, no email, no dispatch, no copy, no scheduler, no
endpoint, no UI, no rollout write, no notification-eligibility decision. It
imports nothing from ``app.notifications`` and writes nothing to ``app.rollout``
— an AST guard in the test suite pins both.

It also does NOT decide the retroactive notification horizon. D3: that decision
belongs to MODEL-HORIZON and ALERT. This module records ``is_retroactive`` and
stops; it forms no opinion about whether anyone may be told.

READS CARRY THEIR FULL CONTEXT
-----------------------------
Every read of ``eco_threshold_event`` filters on tenant, Worker, week, tier and
mode — including the withdrawal-recovery lookup, which could technically match
on a globally unique id alone. Relying on that one mechanism is what a tenant
boundary should never do.

PROVENANCE FAILS CLOSED
-----------------------
An event is recorded only when every ``EcoDayRecord`` the weekly evaluation
named can be read and hashed. Truncated provenance — a hash list shorter than
the id list it claims to describe — raises ``ProvenanceIncomplete`` instead.

NO CLOCK IS READ FOR A DECISION
-------------------------------
D4 makes retroactivity a property of causal lineage, not of the current time.
The only timestamp taken is the audit ``created_at_utc`` of the row being
written, which no branch reads. Two runs over the same persisted state produce
the same determination however far apart they happen.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.eco_threshold import (
    DatabaseThresholdProvider,
    ThresholdOutcome,
    resolve_threshold,
)
from app.eval_day.models import EcoDayRecord
from app.recipient_authority import (
    build_recipient_snapshot,
    resolve_eco_recipients,
)
from app.eval_week import WeeklyState, evaluate_workweek

from .models import EcoThresholdEvent
from .outcomes import (
    EventMode,
    EventOutcome,
    EventTier,
    EventType,
    RefusalReason,
    RetroactiveLineage,
    ThresholdEventResult,
    WeekProvenance,
)

#: The actor recorded on rows this module writes. Follows the established
#: durable-attribution convention — an id where one exists, a system label
#: otherwise (``ConfigVersion.captured_by_label``, ``RolloutControl``).
EVAL_EVENT_ACTOR = "EVAL_EVENT"


class ProvenanceIncomplete(Exception):
    """The week's frozen evidence could not be assembled in full.

    Raised rather than returned, deliberately. Every ``RefusalReason`` describes
    an EXPECTED state of the inputs — the week is not final, no threshold is
    configured, the key is malformed — and a caller can reasonably carry on to
    the next Worker. This is not one of those: an ``EcoDayRecord`` that
    EVAL-WEEK named moments earlier has become unreadable, which means the
    immutable evidence store has been violated. Continuing quietly past that,
    or recording an event whose provenance is a subset of what it claims,
    would put a confident-looking row on top of broken evidence.

    Under the current model this should be unreachable: ``evaluate_workweek``
    itself returns ``DAY_RECORD_MISSING`` when a record is absent, so the week
    would already have been refused. The guard exists because the provenance
    contract must fail closed IF the invariant is ever violated — not because
    a path to it is known.
    """


def _parse_workweek_key(workweek_key: str) -> Optional[date]:
    """The canonical ``YYYY-MM-DD`` start date, or None if not canonical.

    The same strictness ``eval_week.membership.canonical_dates`` applies: the
    key is the week's identity, so a key nobody can parse identifies no week.
    """
    if not isinstance(workweek_key, str):
        return None
    candidate = workweek_key.strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return None
    try:
        parsed = date.fromisoformat(candidate)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.isoformat() == candidate else None


def _open_crossing(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    tier: str, mode: str,
) -> Optional[EcoThresholdEvent]:
    """The crossing for this episode that no withdrawal has closed, if any.

    "Open" is a READ, not a stored flag: the original row is immutable, so it
    cannot carry a ``withdrawn_by`` pointer. A crossing is open when no
    withdrawal row names it.

    Strictly scoped to one ``mode``: a SHADOW row can never satisfy an
    OPERATIONAL lookup, nor the reverse (D8).
    """
    crossings = db.execute(
        select(EcoThresholdEvent).where(and_(
            EcoThresholdEvent.organization_id == organization_id,
            EcoThresholdEvent.user_id == user_id,
            EcoThresholdEvent.workweek_key == workweek_key,
            EcoThresholdEvent.tier == tier,
            EcoThresholdEvent.mode == mode,
            EcoThresholdEvent.event_type == EventType.ECO_THRESHOLD_REACHED.value,
        )).order_by(EcoThresholdEvent.sequence.desc())
    ).scalars().all()
    if not crossings:
        return None

    withdrawn_ids = set(db.execute(
        select(EcoThresholdEvent.supersedes_event_id).where(and_(
            EcoThresholdEvent.organization_id == organization_id,
            EcoThresholdEvent.user_id == user_id,
            EcoThresholdEvent.workweek_key == workweek_key,
            EcoThresholdEvent.tier == tier,
            EcoThresholdEvent.mode == mode,
            EcoThresholdEvent.event_type
            == EventType.ECO_THRESHOLD_WITHDRAWN.value,
        ))
    ).scalars().all())

    for crossing in crossings:
        if crossing.id not in withdrawn_ids:
            return crossing
    return None


def _withdrawal_of(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    tier: str, mode: str, crossing_id: int,
) -> Optional[EcoThresholdEvent]:
    """The withdrawal that closed one specific crossing, within ONE context.

    ``supersedes_event_id`` is globally unique, so matching on it alone would
    already find the right row — but only by trusting a single mechanism. Every
    other read in this module carries the full tenant and mode context, and a
    boundary resting on one mechanism has exactly one way to fail. So the
    context the service already holds is stated explicitly: a row that somehow
    references a crossing outside this tenant, Worker, week, tier or mode is
    not this episode's withdrawal and must not be returned as one.

    This does not weaken ``uq_eco_threshold_event_withdrawal_target``: that
    constraint still makes a second withdrawal of any crossing impossible. The
    filter narrows what is READ, never what is permitted to exist.
    """
    return db.execute(
        select(EcoThresholdEvent).where(and_(
            EcoThresholdEvent.organization_id == organization_id,
            EcoThresholdEvent.user_id == user_id,
            EcoThresholdEvent.workweek_key == workweek_key,
            EcoThresholdEvent.tier == tier,
            EcoThresholdEvent.mode == mode,
            EcoThresholdEvent.event_type
            == EventType.ECO_THRESHOLD_WITHDRAWN.value,
            EcoThresholdEvent.supersedes_event_id == int(crossing_id),
        ))
    ).scalars().first()


def _next_sequence(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    tier: str, mode: str,
) -> int:
    """One past the number of CLOSED lifecycles in this episode and mode.

    Counted from WITHDRAWALS, deliberately — not from ``max(sequence)`` of the
    crossings — and that choice is what makes the open-primary invariant a
    database guarantee rather than an application hope.

    Derived from ``max(sequence) + 1`` it is NOT a guarantee. Consider two runs
    racing on a week with no events yet:

        A: reads "nothing open", reads max(sequence) -> none, inserts seq 1,
           commits;
        B: read "nothing open" BEFORE A committed, but reads max(sequence)
           AFTER -> sees 1, so computes 2, and its insert lands at a DIFFERENT
           identity and succeeds.

    Two open primaries, and no constraint violated. Under READ COMMITTED each
    statement takes a fresh snapshot, so that interleaving is ordinary, not
    exotic.

    Counting withdrawals closes it. A crossing may legitimately exist at
    sequence N only when N-1 lifecycles have been closed, so both racers above
    compute 1, collide on ``uq_eco_threshold_event_identity``, and the loser
    re-reads the winner. The arithmetic holds generally: with W withdrawals
    there is at most one crossing at each of sequences 1..W+1 (identity
    constraint) and exactly W of them are withdrawn — each withdrawal naming a
    distinct crossing (``uq_eco_threshold_event_withdrawal_target``) — so at
    most ONE crossing is left open.

    §21.4 is satisfied by the same rule rather than in spite of it: after one
    withdrawal the re-crossing is sequence 2.
    """
    closed = db.execute(
        select(func.count()).select_from(EcoThresholdEvent).where(and_(
            EcoThresholdEvent.organization_id == organization_id,
            EcoThresholdEvent.user_id == user_id,
            EcoThresholdEvent.workweek_key == workweek_key,
            EcoThresholdEvent.tier == tier,
            EcoThresholdEvent.mode == mode,
            EcoThresholdEvent.event_type
            == EventType.ECO_THRESHOLD_WITHDRAWN.value,
        ))
    ).scalar()
    return int(closed or 0) + 1


def _freeze_provenance(
    db: Session, week
) -> Tuple[WeekProvenance, RetroactiveLineage]:
    """Freeze the week's evidence, and decide retroactivity from it (D2, D4).

    Day content hashes come from the immutable ``EcoDayRecord`` rows the
    evaluation names, so the event pins the exact records it was built from.
    Nothing is fabricated: a NULL policy or config version stays NULL.

    FAIL CLOSED ON INCOMPLETE EVIDENCE
    ----------------------------------
    Every id in ``day_record_ids`` must resolve, and the hashes are built in
    that exact order so position N always describes record N. Skipping an
    unresolvable id would produce a SHORTER hash list that still looks
    well-formed — frozen provenance that silently describes fewer days than it
    references. That is worse than no event at all, so it raises instead.

    No replacement record is ever substituted: the lookup is by primary key
    against the immutable store, never by a "current record for this day"
    query, which would let a later supersession rewrite history.
    """
    records = []
    if week.day_record_ids:
        rows = db.execute(
            select(EcoDayRecord).where(
                EcoDayRecord.id.in_(list(week.day_record_ids)))
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        missing = [int(i) for i in week.day_record_ids if i not in by_id]
        if missing:
            raise ProvenanceIncomplete(
                f"week {week.workweek_key} for (org={week.organization_id}, "
                f"user={week.user_id}) references EcoDayRecord "
                f"{missing} which cannot be read; the event's frozen "
                f"provenance would describe fewer days than it references, so "
                f"no event is recorded"
            )
        records = [by_id[i] for i in week.day_record_ids]
        blank = [int(r.id) for r in records if not (r.content_hash or "").strip()]
        if blank:
            raise ProvenanceIncomplete(
                f"EcoDayRecord {blank} carries no content hash; the event "
                f"cannot pin the evidence it was built from"
            )

    provenance = WeekProvenance(
        canonical_dates=tuple(d.isoformat() for d in week.canonical_dates),
        day_record_ids=tuple(int(i) for i in week.day_record_ids),
        day_content_hashes=tuple(str(r.content_hash) for r in records),
        ledger_versions=tuple(int(v) for v in week.ledger_versions),
        policy_versions=tuple(week.policy_versions),
        config_versions=tuple(week.config_versions),
        observed_states=tuple(week.observed_states),
        blocking_dates=tuple(d.isoformat() for d in week.blocking_dates),
    )

    # The invariant restated against the assembled value, not merely against
    # the lookup above. Two independent checks, because the one that matters
    # is what the EVENT carries, not what an intermediate list held.
    if len(provenance.day_content_hashes) != len(provenance.day_record_ids):
        raise ProvenanceIncomplete(
            f"provenance parity violated: "
            f"{len(provenance.day_record_ids)} day_record_ids but "
            f"{len(provenance.day_content_hashes)} day_content_hashes"
        )

    # ── D4: retroactivity, from persisted lineage only ──────────────────────
    # The flag says the week rests on evidence that was itself corrected — a
    # day record superseding an earlier one, a re-evaluated generation, or a
    # superseded ledger version. All three are durable facts about the records
    # the evaluation folded, so the determination replays identically and never
    # consults the clock.
    #
    # It is an INDICATOR, not causal proof: a re-evaluation that left the
    # weekly total unchanged raises it exactly as one that caused the crossing
    # does, and because NOT_CROSSED writes nothing there is no earlier
    # conclusion to compare against. The precision limit is documented on
    # ``RetroactiveLineage`` and is not papered over with a guess.
    corrected_ids = tuple(
        int(r.id) for r in records if r.supersedes is not None)
    re_evaluated = tuple(
        int(r.evaluation_generation) for r in records
        if int(r.evaluation_generation or 1) > 1)
    corrected_ledgers = tuple(
        int(v) for v in week.ledger_versions if int(v) > 1)

    lineage = RetroactiveLineage(
        is_retroactive=bool(corrected_ids or re_evaluated or corrected_ledgers),
        corrected_day_record_ids=corrected_ids,
        corrected_ledger_versions=corrected_ledgers,
        re_evaluated_generations=re_evaluated,
    )
    return provenance, lineage


def _append(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    tier: str, mode: str, sequence: int, event_type: EventType,
    weekly_total_minutes: int, threshold_minutes: int,
    threshold_config_version: Optional[str],
    provenance: WeekProvenance, lineage: RetroactiveLineage,
    supersedes_event_id: Optional[int], recipient_snapshot: dict,
    created_by: str,
) -> EcoThresholdEvent:
    """Append one row. The caller owns the transaction; this never commits."""
    row = EcoThresholdEvent(
        organization_id=organization_id,
        user_id=user_id,
        workweek_key=workweek_key,
        tier=tier,
        sequence=sequence,
        mode=mode,
        event_type=event_type.value,
        supersedes_event_id=supersedes_event_id,
        weekly_total_minutes=weekly_total_minutes,
        threshold_minutes=threshold_minutes,
        threshold_config_version=threshold_config_version,
        is_retroactive=lineage.is_retroactive,
        caused_by=dict(lineage.as_json()),
        week_provenance=dict(provenance.as_json()),
        recipient_snapshot=recipient_snapshot,
        worker_recipient=user_id,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def _refused(reason: RefusalReason, detail: str, *, organization_id: int,
             user_id: int, workweek_key: str, mode: EventMode
             ) -> ThresholdEventResult:
    return ThresholdEventResult(
        outcome=EventOutcome.REFUSED, organization_id=organization_id,
        user_id=user_id, workweek_key=workweek_key, mode=mode,
        refusal=reason, detail=detail,
    )


def evaluate_threshold_event(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    workweek_key: str,
    mode: EventMode = EventMode.OPERATIONAL,
    created_by: str = EVAL_EVENT_ACTOR,
) -> ThresholdEventResult:
    """Evaluate one Worker-week against its threshold and append at most one row.

    ``mode`` arrives from the caller — the already-authorized context boundary.
    This module reads no rollout state and writes none: whether a tenant is
    OFF, SHADOW or ENABLED is the caller's to know, and inventing a rollout API
    here is explicitly out of scope (D8).

    Returns, never raises, for every ordinary outcome. The caller owns the
    transaction: this flushes and never commits.
    """
    tier = EventTier.PRIMARY.value
    mode_value = EventMode(mode).value

    if _parse_workweek_key(workweek_key) is None:
        return _refused(
            RefusalReason.MALFORMED_WORKWEEK_KEY,
            f"{workweek_key!r} is not a canonical YYYY-MM-DD workweek start",
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode)

    # ── 1. W, from the authoritative weekly evaluation ───────────────────────
    week = evaluate_workweek(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key)
    if week.state is not WeeklyState.COMPLETE or \
            week.weekly_attributable_shortfall_minutes is None:
        # A week that is not final has no total to compare, and comparing a
        # partial week to a threshold is exactly the confident-wrong-number
        # failure EVAL-WEEK's fail-closed rules exist to prevent.
        return _refused(
            RefusalReason.WEEK_NOT_FINAL,
            f"the week is {week.state.value}; no weekly total is established",
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode)
    weekly_total = int(week.weekly_attributable_shortfall_minutes)

    # ── 2. T, from the authoritative threshold reader ────────────────────────
    threshold = resolve_threshold(
        organization_id=organization_id,
        as_of_workweek_start=_parse_workweek_key(workweek_key),
        provider=DatabaseThresholdProvider(db))
    if threshold.outcome is not ThresholdOutcome.RESOLVED or \
            threshold.threshold_minutes is None:
        return _refused(
            RefusalReason.THRESHOLD_NOT_RESOLVED,
            f"no threshold governs this workweek ({threshold.outcome.value})",
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode)
    threshold_minutes = int(threshold.threshold_minutes)

    open_crossing = _open_crossing(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key, tier=tier, mode=mode_value)

    # ── 3. The comparison. §21.1: equality crosses. ──────────────────────────
    crossed = weekly_total >= threshold_minutes

    if not crossed:
        if open_crossing is None:
            return ThresholdEventResult(
                outcome=EventOutcome.NOT_CROSSED,
                organization_id=organization_id, user_id=user_id,
                workweek_key=workweek_key, mode=mode,
                weekly_total_minutes=weekly_total,
                threshold_minutes=threshold_minutes)

        # ── De-crossing (§21.3). A NEW row; the original is untouched. ───────
        provenance, lineage = _freeze_provenance(db, week)
        try:
            with db.begin_nested():
                withdrawal = _append(
                    db, organization_id=organization_id, user_id=user_id,
                    workweek_key=workweek_key, tier=tier, mode=mode_value,
                    # The withdrawal shares the sequence of the crossing it
                    # closes, so one numbered sequence is one full lifecycle.
                    sequence=int(open_crossing.sequence),
                    event_type=EventType.ECO_THRESHOLD_WITHDRAWN,
                    weekly_total_minutes=weekly_total,
                    threshold_minutes=threshold_minutes,
                    threshold_config_version=threshold.config_version_id,
                    provenance=provenance, lineage=lineage,
                    supersedes_event_id=int(open_crossing.id),
                    # D5: the ORIGINAL frozen recipient snapshot, never
                    # re-resolved. The people a withdrawal concerns are the
                    # people the crossing named, whatever has changed since.
                    recipient_snapshot=dict(
                        open_crossing.recipient_snapshot or {}),
                    created_by=created_by)
        except IntegrityError:
            # A concurrent run withdrew the same crossing first. The DB is the
            # authority (D6): the crossing is withdrawn exactly once, and this
            # run reports the withdrawal that won rather than a second one.
            existing = _withdrawal_of(
                db, organization_id=organization_id, user_id=user_id,
                workweek_key=workweek_key, tier=tier, mode=mode_value,
                crossing_id=int(open_crossing.id))
            if existing is None:
                raise
            return ThresholdEventResult(
                outcome=EventOutcome.WITHDRAWN,
                organization_id=organization_id, user_id=user_id,
                workweek_key=workweek_key, mode=mode,
                event_id=int(existing.id),
                event_type=EventType.ECO_THRESHOLD_WITHDRAWN,
                sequence=int(existing.sequence),
                weekly_total_minutes=weekly_total,
                threshold_minutes=threshold_minutes,
                detail="concurrent withdrawal already recorded")
        return ThresholdEventResult(
            outcome=EventOutcome.WITHDRAWN,
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode, event_id=int(withdrawal.id),
            event_type=EventType.ECO_THRESHOLD_WITHDRAWN,
            sequence=int(withdrawal.sequence),
            weekly_total_minutes=weekly_total,
            threshold_minutes=threshold_minutes)

    # ── 4. W >= T ────────────────────────────────────────────────────────────
    if open_crossing is not None:
        # §21.2: the natural key already holds a crossing for this episode, so
        # a replay or a further shortfall writes nothing. Not a boolean, not a
        # process-local memo — the existing row IS the fact.
        return ThresholdEventResult(
            outcome=EventOutcome.ALREADY_CROSSED,
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode,
            event_id=int(open_crossing.id),
            event_type=EventType.ECO_THRESHOLD_REACHED,
            sequence=int(open_crossing.sequence),
            weekly_total_minutes=weekly_total,
            threshold_minutes=threshold_minutes)

    sequence = _next_sequence(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key, tier=tier, mode=mode_value)
    provenance, lineage = _freeze_provenance(db, week)

    # A re-crossing names the withdrawal that preceded it (§21.4).
    prior_withdrawal_id = None
    if sequence > 1:
        prior_withdrawal_id = db.execute(
            select(EcoThresholdEvent.id).where(and_(
                EcoThresholdEvent.organization_id == organization_id,
                EcoThresholdEvent.user_id == user_id,
                EcoThresholdEvent.workweek_key == workweek_key,
                EcoThresholdEvent.tier == tier,
                EcoThresholdEvent.mode == mode_value,
                EcoThresholdEvent.event_type
                == EventType.ECO_THRESHOLD_WITHDRAWN.value,
            )).order_by(EcoThresholdEvent.sequence.desc()).limit(1)
        ).scalar()

    # §33.1 / ruling D-3: the four audiences, resolved from the authoritative
    # designation model and frozen here PERMANENTLY. This is the only site that
    # resolves current authority; every later read works from the frozen
    # structure, never from today's designations (D-3.7).
    #
    # A cross-tenant recipient raises rather than resolving to nobody, so a
    # boundary violation cannot be mistaken for an unconfigured tenant. An
    # audience that is simply absent is recorded WITH ITS REASON rather than
    # omitted — nothing is silently dropped.
    recipient_snapshot = build_recipient_snapshot(resolve_eco_recipients(
        db, organization_id=organization_id, worker_user_id=user_id))

    try:
        with db.begin_nested():
            crossing = _append(
                db, organization_id=organization_id, user_id=user_id,
                workweek_key=workweek_key, tier=tier, mode=mode_value,
                sequence=sequence,
                event_type=EventType.ECO_THRESHOLD_REACHED,
                weekly_total_minutes=weekly_total,
                threshold_minutes=threshold_minutes,
                threshold_config_version=threshold.config_version_id,
                provenance=provenance, lineage=lineage,
                supersedes_event_id=(int(prior_withdrawal_id)
                                     if prior_withdrawal_id else None),
                recipient_snapshot=recipient_snapshot,
                created_by=created_by)
    except IntegrityError as exc:
        # A concurrent run won the race on the uniqueness boundary. The
        # database is the authority (D6): roll back only this savepoint,
        # re-read, and return the winner. An IntegrityError that is NOT this
        # conflict is re-raised rather than swallowed.
        existing = _open_crossing(
            db, organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, tier=tier, mode=mode_value)
        if existing is None:
            raise
        return ThresholdEventResult(
            outcome=EventOutcome.ALREADY_CROSSED,
            organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=mode, event_id=int(existing.id),
            event_type=EventType.ECO_THRESHOLD_REACHED,
            sequence=int(existing.sequence),
            weekly_total_minutes=weekly_total,
            threshold_minutes=threshold_minutes,
            detail=f"concurrent crossing already recorded ({exc.__class__.__name__})")

    return ThresholdEventResult(
        outcome=(EventOutcome.RE_CROSSED if sequence > 1
                 else EventOutcome.CROSSED),
        organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key, mode=mode, event_id=int(crossing.id),
        event_type=EventType.ECO_THRESHOLD_REACHED,
        sequence=sequence, weekly_total_minutes=weekly_total,
        threshold_minutes=threshold_minutes)


def read_events(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    mode: EventMode = EventMode.OPERATIONAL,
) -> List[EcoThresholdEvent]:
    """The episode's event history, oldest first, for ONE mode.

    Named explicitly by mode so a caller cannot accidentally read SHADOW rows
    into an operational surface (D8).
    """
    return list(db.execute(
        select(EcoThresholdEvent).where(and_(
            EcoThresholdEvent.organization_id == organization_id,
            EcoThresholdEvent.user_id == user_id,
            EcoThresholdEvent.workweek_key == workweek_key,
            EcoThresholdEvent.mode == EventMode(mode).value,
        )).order_by(EcoThresholdEvent.id)
    ).scalars().all())


__all__ = [
    "evaluate_threshold_event",
    "read_events",
    "EVAL_EVENT_ACTOR",
]
