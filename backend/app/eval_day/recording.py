"""
The EVAL-DAY recording boundary.
================================

    evaluate_and_record_day(db, organization_id=..., user_id=..., work_date=...)

The one production path from sealed evidence to a persisted ECO evaluation:

    sealed WorkDayLedger  ->  EVAL-DAY  ->  immutable EcoDayRecord

STATUS: IMPLEMENTED, NOT WIRED TO ANY TRIGGER. No route, service or scheduler
calls it, and none is created here — the audit found no scheduler in the
application and ruling 7 forbids inventing one. This increment creates the
callable boundary; automatic triggering is a later increment.

ORCHESTRATION ONLY
------------------
No arithmetic lives here. The ledger is read through ``work_ledger.service``,
the evaluation is produced by the pure ``evaluate_day``, and persistence goes
through ``record_evaluation``. This module decides nothing about minutes,
rounding, requirements or thresholds; it sequences the three seams that already
exist and turns their refusals into values.

WHY THERE IS NO ``provider`` PARAMETER — THE GOVERNANCE RULE (rulings 3, 5, 7)
------------------------------------------------------------------------------
Owner ruling: for a work date D, the authoritative evaluation is the one derived
from the configuration version authoritative **as of D**, and that authority is
established HERE, at the recording boundary — not inside EVAL-WEEK.

So the configuration source is not a parameter. The service constructs
``DatabaseConfigVersionProvider`` itself, and the requirement is resolved by the
already-authorized strict resolver, whose ``version_as_of(D)`` is the single
existing algorithm for "which configuration governs this date". A caller cannot
inject a stale or alternative provider, because there is nothing to inject
into. That is deliberate: ruling 7 requires that a stale snapshot never
silently become the authoritative weekly input, and the surest way to honour it
is to make a non-authoritative configuration source unexpressible rather than
merely discouraged.

No second configuration-resolution algorithm is introduced. There is one, in
Step 6, and this path calls it.

WHAT IT REFUSES, AND WHY REFUSALS ARE VALUES
--------------------------------------------
``evaluate_day`` raises for the conditions where no ECO result may be invented.
A recording boundary that propagated those as exceptions would make the
ordinary "this day is not evaluable yet" case indistinguishable from a defect,
so they are caught and returned as typed outcomes carrying the evaluator's own
stable reason string. Nothing is fabricated: a refusal writes no record.

The two ledger conditions that DO produce a record are passed straight through,
because the accepted EVAL-DAY contract already decides them:

* ``MISSING_CLOCK_OUT`` -> ``DEFERRED_PENDING_LEDGER``, contributing 0, every
  measurement NULL. A deferral is a fact worth recording (S2, S3).
* ``NOT_A_WORKING_DAY`` -> a zeroed record, so the week fold stays complete and
  explainable (R5, §24.2).

RE-EVALUATION
-------------
Idempotent against the full snapshot identity. Re-running a day whose inputs
have not changed returns the existing record and writes nothing. A completed
day cannot change configuration snapshot at all — no version can become
effective at or before a date in a completed workweek, so ``version_as_of(D)``
for such a day is frozen and re-evaluation reproduces the identical identity.

Inside the current open workweek a configuration version effective earlier in
that same week can legitimately produce a NEW immutable historical snapshot.
Both rows then exist, and EVAL-WEEK's ``DAY_RECORD_AMBIGUOUS`` remains
authoritative — this module does not choose between them, and no
current-marker column is added (rulings 4, 8).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.work_ledger import (
    DayKey,
    LedgerState,
    get_current_ledger,
    read_intervals,
)
from app.working_day_classifier import (
    ClassificationOutcome,
    classification_from_sealed,
)

from .errors import (
    DayRecordConflict,
    EvaluationRefused,
    REASON_LEDGER_NOT_SEALED,
)
from .classification import BreakTreatmentOutcome, break_treatment_for_day
from .evaluator import DayEvaluation, evaluate_day, record_evaluation
from .exclusion import approved_leave_overlap
from .netting import resolve_bank_credit
from .leave_snapshot import get_or_create_leave_snapshot
from .models import EcoDayRecord
from .states import EcoDayState, EvaluationDeferralReason

#: No current ledger row exists for the requested Worker-day.
#:
#: Distinct from an unsealed day: there is nothing to evaluate at all, as
#: opposed to evidence that is not yet admissible. Returned rather than treated
#: as an empty day, because "no ledger" and "a ledger with no work" are
#: different facts and only one of them is a zero.
REASON_NO_LEDGER_DAY = "NO_LEDGER_DAY"

#: The same snapshot already holds a different result — the evaluator changed.
#: Surfaced as a value here while ``record_evaluation`` keeps raising
#: ``DayRecordConflict`` for its existing callers.
REASON_RESULT_CONFLICT = "RESULT_CONFLICT"


class RecordingOutcome(str, enum.Enum):
    RECORDED = "RECORDED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class DayRecordingResult:
    """What happened, in a form the caller cannot misread as success."""

    outcome: RecordingOutcome
    organization_id: int
    user_id: int
    work_date: date

    #: The persisted record. Populated only on ``RECORDED``.
    record: Optional[EcoDayRecord] = None

    #: The ledger state that governed the attempt, when one was found.
    ledger_state: Optional[str] = None
    ledger_version: Optional[int] = None

    #: The evaluator's own stable reason string on ``REFUSED``.
    reason: Optional[str] = None
    detail: Optional[str] = None

    @property
    def is_recorded(self) -> bool:
        return self.outcome is RecordingOutcome.RECORDED

    @property
    def state(self) -> Optional[str]:
        """The ECO day state that was persisted, if any."""
        return self.record.state if self.record is not None else None

    def __post_init__(self) -> None:
        if self.outcome is RecordingOutcome.RECORDED:
            if self.record is None:
                raise ValueError("RECORDED must carry the persisted record")
            if self.reason is not None:
                raise ValueError("RECORDED carries no refusal reason")
        else:
            if self.reason is None:
                raise ValueError("REFUSED must state its reason")
            if self.record is not None:
                raise ValueError(
                    f"{self.reason} wrote no record, so it must not carry one"
                )


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def evaluate_and_record_day(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date | datetime,
) -> DayRecordingResult:
    """Evaluate one Worker-day from sealed evidence and persist the result.

    Four parameters, deliberately. There is no ``ledger_version`` parameter —
    the current version is resolved here, so a caller cannot evaluate a
    superseded one and have it recorded as though it were in force. There is no
    ``provider``, ``config_version``, ``policy_version``, ``tier`` or ``key``
    parameter — every one of those is determined by the authorities that own it.

    Returns, never raises, for every ordinary outcome. The caller owns the
    transaction: this flushes and never commits.
    """
    on = _as_date(work_date)

    def _refuse(reason: str, detail: str, ledger=None) -> DayRecordingResult:
        return DayRecordingResult(
            outcome=RecordingOutcome.REFUSED,
            organization_id=organization_id,
            user_id=user_id,
            work_date=on,
            ledger_state=(ledger.state if ledger is not None else None),
            ledger_version=(
                int(ledger.ledger_version) if ledger is not None else None),
            reason=reason,
            detail=detail,
        )

    # ── 1. The current ledger version for the day ────────────────────────────
    # Read through Step 1's own accessor, which selects the non-superseded row
    # and is strictly tenant-scoped. A correction therefore moves this path onto
    # the new version automatically, and a superseded version is never chosen.
    ledger = get_current_ledger(
        db, DayKey(organization_id=organization_id, user_id=user_id, work_date=on)
    )
    if ledger is None:
        return _refuse(
            REASON_NO_LEDGER_DAY,
            f"no current ledger day exists for user {user_id} on "
            f"{on.isoformat()} in organization {organization_id}",
        )

    # ── 2. An unsealed day is not evidence yet ───────────────────────────────
    # Refused here as well as inside the evaluator, so the boundary states the
    # condition rather than relying on a downstream raise. §7.2 forbids
    # inferring an end of day, so nothing is assumed about an open one.
    if ledger.state == LedgerState.OPEN.value:
        return _refuse(
            REASON_LEDGER_NOT_SEALED,
            f"{on.isoformat()} is still OPEN; only sealed evidence may be "
            f"evaluated, and no departure is inferred for an open day",
            ledger,
        )

    # ── 3. Evaluate, through the pure evaluator ──────────────────────────────
    # Deferred import: the provider is constructed here so no caller can supply
    # a different configuration source (see the module docstring). The import
    # is local to keep the module graph acyclic, the idiom the ledger and
    # schedule packages already use.
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.policy_version import resolve_policy_as_of

    # ── 3a. THE POLICY COMES FIRST — §11's evaluateDay step 1 ───────────────
    #
    # Resolved historically, against the work date, before any
    # requirement-dependent arithmetic. A Tenant with no applicable policy is
    # REFUSED rather than evaluated under an assumed one: §15 forbids assuming
    # ``NONE`` netting, and the owner declined a neutral default outright, so
    # there is no policy to fall back to and none is invented.
    #
    # Of the five attributes it carries, ``grace_minutes`` is applied
    # (MODEL-GRACE) and is threaded into the pure evaluator below together with
    # the policy identity it came from. Netting, break treatment and rounding
    # profiles remain their own increments and are still not applied.
    policy = resolve_policy_as_of(db, organization_id=organization_id, as_of=on)

    # ── 3b. The working-day classification (D9-A) ───────────────────────────
    # Read from the ledger's frozen context; no configuration is consulted.
    #
    # WHY THIS PRECEDES LEAVE RESOLUTION
    # ----------------------------------
    # Leave is irrelevant to a non-working day: it can neither turn one into a
    # working day nor create a shortfall on one. Resolving leave first meant an
    # authority nobody could confirm blocked the zeroed record entirely — a
    # rest day left with no record at all, which is precisely what §24.2 says
    # must not happen, because a day the week fold skips silently is a week
    # nobody can explain.
    #
    # So for a valid NOT_A_WORKING_DAY the leave authority is not consulted at
    # all. That is not the same as treating it as zero: nothing is asked, so
    # nothing is assumed, and no leave contribution is fabricated.
    classification = classification_from_sealed(ledger)
    is_non_working = (
        classification.outcome is ClassificationOutcome.NOT_A_WORKING_DAY)

    if is_non_working or not policy.is_resolved:
        # D15-A: the canonical EMPTY snapshot, exactly as ruling E already
        # creates for a working day with no contributing leave — never a NULL
        # reference, and content-addressed, so an exact replay reuses it and
        # writes nothing new.
        overlap_seconds = 0
        contributions = ()
    else:
        # ── Approved-leave overlap (MODEL-EXCLUSION) ─────────────────────────
        # Resolved here, where the session lives, so the evaluator stays pure.
        # An authority that cannot be established is REFUSED rather than
        # counted as zero — "no leave" and "leave whose grant nobody can
        # confirm" are different facts, and only the first is a zero. Ordinary
        # working-day behaviour is unchanged by D9-A.
        overlap = approved_leave_overlap(
            db, organization_id=organization_id, user_id=user_id, work_date=on,
        )
        if not overlap.is_resolved:
            return _refuse(overlap.reason, overlap.detail or "", ledger)
        overlap_seconds = overlap.overlap_seconds
        contributions = overlap.contributions

    if not policy.is_resolved:
        # Stop before any calculation. POLICY_UNRESOLVED is an UNCOMPUTED state,
        # so ``DayEvaluation`` structurally refuses to let it carry a minute
        # field — including the contribution, which stays NULL rather than a
        # fabricated zero. The record IS written: a day the week fold cannot
        # see is a week nobody can explain (§24.2), and EVAL-WEEK blocks on an
        # unplaced state rather than summing around it.
        evaluation = DayEvaluation(
            organization_id=organization_id,
            user_id=user_id,
            work_date=on,
            ledger_version=int(ledger.ledger_version),
            state=EcoDayState.POLICY_UNRESOLVED,
            requirement_outcome="NOT_APPLICABLE",
            shortfall_computed=False,
        )
    else:
        # ── 3c. Break classification (P-1, P-2, C-1, D-1) ────────────────────
        # POST-SEAL and boundary-side: the evaluator stays pure and the
        # producer stays classification-blind (D-18 untouched).
        #
        # WHEN A DAY REQUIRES CLASSIFICATION
        # ----------------------------------
        # Only when a BreakTreatmentProfile governs the work date. That is not
        # a new rule — it follows from P-4, which gates rest qualification on
        # ``the resolved break-treatment profile``. With no profile in force
        # there is no rule by which a classification could change any
        # arithmetic, so a day with breaks evaluates exactly as it always has
        # and ``classification_snapshot_key`` stays NULL. A Tenant opts in by
        # capturing a profile; until then nothing about their days changes.
        treatment = break_treatment_for_day(
            db, organization_id=organization_id, user_id=user_id,
            work_date=on, ledger=ledger)

        # Three shapes: None (no profile governs), a resolved contribution, or
        # a ClassificationResolution that either refuses or defers.
        resolved = treatment if isinstance(treatment, BreakTreatmentOutcome)             else None
        classification = (treatment.classification if resolved is not None
                          else treatment)

        if classification is not None and classification.is_refused:
            # An unsealed ledger, an integrity fault, conflicting assertions,
            # an unresolvable profile, or break evidence with no authoritative
            # end. No ECO result may be derived, so none is.
            return _refuse(classification.reason, classification.detail or "",
                           ledger)

        if classification is not None and classification.defers:
            # D-1: the deferred CARRIER with the classification REASON. Every
            # measurement stays NULL and the contribution is a truthful 0 —
            # the same S2 shape the departure deferral has always carried,
            # distinguished only by the reason. No shortfall is calculated, no
            # threshold is consulted, and nothing downstream is emitted.
            evaluation = DayEvaluation(
                organization_id=organization_id,
                user_id=user_id,
                work_date=on,
                ledger_version=int(ledger.ledger_version),
                state=EcoDayState.DEFERRED_PENDING_LEDGER,
                requirement_outcome="NOT_APPLICABLE",
                shortfall_computed=False,
                contributes_to_accumulator_minutes=0,
                policy_version=policy.policy_version,
                evaluation_deferral_reason=(
                    EvaluationDeferralReason.CLASSIFICATION_UNRESOLVED.value),
                classification_snapshot_key=classification.snapshot_key,
            )
        else:
            # ── §18 step 10: the bank credit (MODEL-NETTING) ────────────
            # Resolved here rather than in the evaluator because the fold
            # reads OTHER day records — the same reason leave overlap and
            # the break contribution are resolved at this boundary.
            #
            # It needs this day's requirement and qualifying minutes, and
            # the only authority for those is the evaluator itself. So the
            # day is evaluated ONCE with no credit to obtain them, and
            # re-evaluated with the credit when — and only when — a banking
            # policy governs and the first pass produced a real gap.
            #
            # The double evaluation is pure and cheap (no I/O in
            # `evaluate_day`), and it is what keeps the fold out of the
            # pure function. Under `NONE` the second pass never happens, so
            # the overwhelmingly common path is unchanged.
            try:
                evaluation = evaluate_day(
                    ledger=ledger,
                    intervals=read_intervals(db, ledger),
                    provider=DatabaseConfigVersionProvider(db),
                    leave_overlap_seconds=overlap_seconds,
                    # Identity and value from the SAME historical resolution,
                    # for the SAME work date. Never the current policy, and
                    # never one without the other — the evaluator refuses a
                    # grace that arrives with no policy identity.
                    policy_version=policy.policy_version,
                    grace_minutes=policy.grace_minutes,
                    # AC-16 / 15: the mode that GOVERNED the fold, recorded on
                    # the day record whether or not a bank was consumed. A day
                    # under NONE and one under WEEKLY_BANK that happen to net
                    # to the same minutes are not the same evaluation.
                    netting_mode=policy.netting_mode,
                    # C-1: the classification input this evaluation used, so a
                    # later correction is a different identity and therefore a
                    # new generation. None where no profile governs the date.
                    classification_snapshot_key=(
                        None if classification is None
                        else classification.snapshot_key),
                    # P2-D4: the governing RULE, its own identity component.
                    break_treatment_profile_ref=(
                        None if resolved is None else resolved.profile_ref),
                    # P2-D1/P2-D2: already classified, already treated under
                    # that profile, and already CAPPED. Seconds, so the
                    # evaluator's single rounding site converts the whole
                    # qualifying pool once.
                    qualifying_break_seconds=(
                        0 if resolved is None
                        else resolved.qualifying_break_seconds),
                    # INV-4 evidence: observed and qualifying, per state.
                    brk_m_seconds=(
                        None if resolved is None else resolved.observed_meal_seconds),
                    brk_m_qualifying_seconds=(
                        None if resolved is None else resolved.qualifying_meal_seconds),
                    brk_r_seconds=(
                        None if resolved is None else resolved.observed_rest_seconds),
                    brk_r_qualifying_seconds=(
                        None if resolved is None else resolved.capped_rest_seconds),
                )
            except EvaluationRefused as refused:
                # LEDGER_INTEGRITY_FAULT, NON_WRK_STATE_PRESENT and the rest.
                # Each means no ECO result may be invented, so none is — and
                # the evaluator's own reason is carried through rather than
                # re-worded.
                return _refuse(refused.reason, refused.detail, ledger)

            bank = resolve_bank_credit(
                db,
                organization_id=organization_id,
                user_id=user_id,
                work_date=on,
                # CANONICAL membership, frozen on the ledger at seal —
                # never ISO weeks and never today's configuration (W4-3).
                workweek_key=ledger.workweek_key,
                netting_mode=policy.netting_mode,
                cap_per_day_minutes=policy.surplus_cap_per_day_minutes,
                cap_per_period_minutes=policy.surplus_cap_per_period_minutes,
                # The first-pass evaluation, whole. MODEL-NETTING reads the
                # measurements off it; this boundary names none of them,
                # because it performs no arithmetic of its own.
                evaluation=evaluation,
            )
            if bank.is_refused:
                # A banking policy with no cap, or PERIOD_BANK. No ECO
                # result may be derived from a bank nobody bounded.
                return _refuse(bank.reason, bank.detail or "", ledger)

            if bank.minutes:
                # Re-run the SAME pure evaluation with the credit applied at
                # step 11. Only when the fold actually supplied something:
                # a zero credit cannot change any of the arithmetic, and
                # re-evaluating for it would burn a pass for no effect.
                try:
                    evaluation = evaluate_day(
                        ledger=ledger,
                        intervals=read_intervals(db, ledger),
                        provider=DatabaseConfigVersionProvider(db),
                        leave_overlap_seconds=overlap_seconds,
                        policy_version=policy.policy_version,
                        grace_minutes=policy.grace_minutes,
                        # AC-16 / 15: the mode that GOVERNED the fold, recorded on
                        # the day record whether or not a bank was consumed. A day
                        # under NONE and one under WEEKLY_BANK that happen to net
                        # to the same minutes are not the same evaluation.
                        netting_mode=policy.netting_mode,
                        classification_snapshot_key=(
                            None if classification is None
                            else classification.snapshot_key),
                        break_treatment_profile_ref=(
                            None if resolved is None else resolved.profile_ref),
                        qualifying_break_seconds=(
                            0 if resolved is None
                            else resolved.qualifying_break_seconds),
                        # INV-4 evidence: observed and qualifying, per state.
                        brk_m_seconds=(
                            None if resolved is None else resolved.observed_meal_seconds),
                        brk_m_qualifying_seconds=(
                            None if resolved is None else resolved.qualifying_meal_seconds),
                        brk_r_seconds=(
                            None if resolved is None else resolved.observed_rest_seconds),
                        brk_r_qualifying_seconds=(
                            None if resolved is None else resolved.capped_rest_seconds),
                        bank_credit_minutes=bank.minutes,
                        bank_input_key=bank.input_key,
                    )
                except EvaluationRefused as refused:
                    return _refuse(refused.reason, refused.detail, ledger)
            # No `else` branch, deliberately. A computed day already carries
            # `bank_credit_minutes=0` from the first pass — the evaluator
            # records the credit it applied, and that is 0 when the fold
            # supplied nothing. Setting it here as well would also reach the
            # UNCOMPUTED states, where §10.2 structurally forbids a number.

    # ── 4. Freeze the leave input, then persist ──────────────────────────────
    # Content-addressed, so an exact replay reuses the same snapshot and writes
    # nothing new. An explicit EMPTY snapshot is created for a day with no
    # contributing leave (ruling E) — never a NULL reference.
    snapshot = get_or_create_leave_snapshot(
        db,
        organization_id=organization_id,
        user_id=user_id,
        work_date=on,
        overlap_seconds=overlap_seconds,
        contributions=contributions,
    )

    try:
        record = record_evaluation(
            db, evaluation,
            leave_input_snapshot_key=snapshot.snapshot_key,
        )
    except DayRecordConflict as conflict:
        return _refuse(REASON_RESULT_CONFLICT, str(conflict), ledger)

    return DayRecordingResult(
        outcome=RecordingOutcome.RECORDED,
        organization_id=organization_id,
        user_id=user_id,
        work_date=on,
        record=record,
        ledger_state=ledger.state,
        ledger_version=int(ledger.ledger_version),
    )


__all__ = [
    "evaluate_and_record_day",
    "DayRecordingResult",
    "RecordingOutcome",
    "REASON_NO_LEDGER_DAY",
    "REASON_RESULT_CONFLICT",
]
