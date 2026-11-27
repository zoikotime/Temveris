"""
The deterministic day evaluation.
=================================

EVAL-DAY, under owner rulings R5, R6, R10, R11, R12, R13, R15.

NOT WIRED
---------
No route, scheduler or existing service calls this. Nothing evaluates a real
day. ECO v1 remains disabled and ECO v2 rollout remains OFF.

WHAT IT DOES
------------
Folds one sealed Worker-day into one derived DayRecord: qualifying minutes,
gross shortfall, attributable shortfall. That is all. It does not accumulate a
week, emit an event, notify anybody, or classify a break.

WHAT IT REFUSES, AND WHY THAT IS THE POINT
------------------------------------------
This increment is authorised for the WRK-only, no-exclusion path. Every other
shape fails closed:

* **Any non-WRK interval** (R6) — TRV, ONC, LVE, OFF, UNK, BRK_M, BRK_R. None of
  them is "assumed to qualify", and deciding would require the classifier and
  the Legal Matrix, neither of which is authorised. Even OFF is refused rather
  than silently counted as zero: R6 says fail closed rather than invent
  semantics, and "obviously zero" is still a semantic nobody ruled.
* **Ledger integrity fault** (R10) — refused outright. §24.15: the day is NOT
  evaluated, NOT repaired, NOT clamped, NOT skipped. No record is written,
  because converting corrupt evidence into an evaluation result is the
  fabrication the rule exists to prevent.
* **Missing clock-out** (R10) — refused. §7.2 calls inferring an end of day
  "the single worst thing this control could do". Note the open decision: GV-16
  expects a DayRecord in state ``MISSING_CLOCK_OUT``, but R4's evaluation
  vocabulary does not contain it and R4 forbids adding it. Reported, not
  resolved.
* **Unresolved requirement** (R13) — recorded as REQUIREMENT_UNRESOLVED with no
  minutes at all. §10.2: shortfall MUST NOT be computed, assumed zero, or
  assumed to be the default. The legacy 8-hour fallback is never reachable from
  here.

SOURCES IT NEVER USES
---------------------
``_worked.py``, raw clock/break sessions, screenshots, activity, application
names, idle scoring, ML, client-side elapsed time, jurisdiction. Enforced by an
import-graph test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.requirement_resolution import (
    ConfigVersionProvider,
    ResolutionOutcome,
    resolve_requirement_strict,
)
from app.work_ledger import LedgerState, WorkState
from app.working_day_classifier import (
    ClassificationOutcome,
    classification_from_sealed,
)

from .errors import (
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_MISSING_CLOCK_OUT,
    REASON_LEDGER_NOT_SEALED,
    REASON_NON_WRK_STATE_PRESENT,
    REASON_ORGANIZATION_REQUIRED,
    DayRecordAuthorityContention,
    DayRecordConflict,
    EvaluationRefused,
    LeaveInputSnapshotRequired,
)
from .models import SUPERSESSION_FIELD, EcoDayRecord, compute_result_hash
from .rounding import round_half_up_minutes
from .states import (
    COMPUTED_STATES,
    DEFERRED_STATES,
    REASON_BEARING_STATES,
    EcoDayState,
    EvaluationDeferralReason,
    is_valid_deferral_reason,
)

#: Only WRK qualifies in this increment (R6). Deliberately a set of one so that
#: widening it is a visible, reviewable edit rather than a condition tweak.
QUALIFYING_STATES = frozenset({WorkState.WRK.value})


def schedule_variance_minutes(ledger) -> Optional[int]:
    """§18 step 14: ``variance := actual_end_local - scheduled_end_local``.

    THE ONE SITE. No other layer computes, re-derives or re-rounds this.

    Both operands come from the SEALED row and nowhere else: the frozen
    ``scheduled_end_local`` (D-1.1), and ``departure_at`` converted to local
    time through the row's own frozen ``timezone_iana`` (D-18E). The current
    schedule resolver is never consulted — that is the whole point of freezing
    the scheduled end, and an import-graph test already pins that this module
    cannot reach ``schedule_policy``.

    RETURNS ``None``, NEVER ``0``, WHENEVER AN OPERAND IS MISSING
    ------------------------------------------------------------
    Zero is a measurement: it asserts the Worker left exactly at the scheduled
    end. A day with no scheduled end (§D-2.1's ``system`` source, or a row
    sealed before the column existed) and a day with no verified departure are
    both days where nobody recorded the fact. Collapsing either onto zero would
    manufacture evidence, which is the failure mode the whole ruling exists to
    prevent.

    The result is SIGNED and never clamped: early departure is negative, an
    exact departure is zero, and a late departure is positive. It never
    participates in a threshold comparison — §10 "recorded; never drives the
    threshold", §35 "signed, never used in comparisons".
    """
    scheduled = getattr(ledger, "scheduled_end_local", None)
    departure = getattr(ledger, "departure_at", None)
    if scheduled is None or departure is None:
        return None

    zone_name = getattr(ledger, "timezone_iana", None)
    if not zone_name:
        # A sealed row always carries its zone; without one there is no local
        # time to compare against and inventing a zone would invent the answer.
        return None
    try:
        zone = ZoneInfo(str(zone_name))
    except (ZoneInfoNotFoundError, ValueError):
        return None

    if departure.tzinfo is None:
        # SQLite drops tzinfo on read while Postgres preserves it, so a value
        # round-tripped through the database arrives naive even though a true
        # UTC instant was written (§16.4). Reading it as UTC is the platform's
        # existing convention — `work_ledger.service._utc` and
        # `employees.router._utc_aware` both do exactly this, for exactly this
        # reason. Refusing instead would make the variance computable on
        # Postgres and NULL on SQLite, which is a difference in evidentiary
        # behaviour between backends rather than a safety property.
        departure = departure.replace(tzinfo=timezone.utc)
    local_end = departure.astimezone(zone)

    actual_minutes = local_end.hour * 60 + local_end.minute
    scheduled_minutes = scheduled.hour * 60 + scheduled.minute
    return actual_minutes - scheduled_minutes


#: §16.3's Worker-favourable default, and the only profile implemented.
#: §16.4 permits NEAREST / DOWN / UP / INTERVAL_6MIN / INTERVAL_15MIN where a
#: collective agreement requires one; none is built, so recording the profile
#: that actually ran is what will let those be told apart from this one.
ROUNDING_PROFILE_HALF_UP = "HALF_UP"

#: §35.1: "engine semver; changes force replay". Increment when a change to
#: this evaluator alters output for unchanged inputs -- that is the signal a
#: replay pass uses to decide a stored record needs recomputing. It is NOT part
#: of the content hash (see compute_result_hash).
CALCULATION_VERSION = "1.0.0"


def _provenance(ledger, requirement=None, netting_mode=None) -> dict:
    """The §35 provenance every day record carries, known or null.

    AC-16 asks for it on EVERY day record, not only computed ones. "Why was
    this day not evaluated" carries the same evidentiary weight as "why was
    this figure 20", and a deferred day whose timezone and engine version are
    unknown cannot be explained later either.

    What is genuinely unknown stays null rather than being guessed: a day that
    never resolved a requirement has no source rank, and saying so is the
    point.
    """
    return {
        "timezone_iana": getattr(ledger, "timezone_iana", None),
        "utc_offset_seconds": getattr(ledger, "utc_offset_seconds", None),
        "rounding_profile": ROUNDING_PROFILE_HALF_UP,
        "calculation_version": CALCULATION_VERSION,
        "netting_mode": netting_mode,
        "requirement_source": getattr(requirement, "source_tier", None),
        "requirement_source_rank": getattr(requirement, "source_rank", None),
        "requirement_source_ref": getattr(requirement, "source_ref", None),
    }


@dataclass(frozen=True)
class DayEvaluation:
    """The computed result, before persistence.

    A non-computed state structurally cannot carry minutes — the same guarantee
    Step 5's ``RequirementResolution`` makes, for the same reason: it stops an
    unevaluated day being read as a zero-shortfall day by a caller that forgot
    to check the state.
    """

    organization_id: int
    user_id: int
    work_date: date
    ledger_version: int
    state: EcoDayState
    requirement_outcome: str
    shortfall_computed: bool
    policy_version: Optional[str] = None
    config_version: Optional[str] = None
    required_minutes: Optional[int] = None
    qualifying_minutes: Optional[int] = None
    gross_shortfall_minutes: Optional[int] = None
    exclusion_minutes: Optional[int] = None
    attributable_minutes: Optional[int] = None
    #: The §11 policy grace used to decide ``attributable_minutes``, or None
    #: where no grace comparison was performed. NOT in ``_MINUTE_FIELDS``: a
    #: computed day may legitimately carry no grace (see ``__post_init__``).
    grace_minutes_applied: Optional[int] = None
    schedule_variance_minutes: Optional[int] = None
    #: §18 step 10 (D-6.4). Minutes of banked surplus this day consumed to
    #: reduce its own requirement gap, folded from EARLIER days in the same
    #: canonical workweek. A DERIVED day-level credit — never a balance, a
    #: counter or an account: §15.1 / INV-3 make the bank a fold over day
    #: records, and this is one day's share of that fold, recorded so the
    #: arithmetic on the row can be explained without recomputing the week.
    #:
    #: 0 under `NONE` and on any day that needed nothing. NULL only where no
    #: evaluation happened at all.
    bank_credit_minutes: Optional[int] = None
    contributes_to_accumulator_minutes: Optional[int] = None
    #: WHICH condition deferred the day (D-1). Only a deferred state may carry
    #: one; NULL on a deferred day means the ledger/departure deferral.
    evaluation_deferral_reason: Optional[str] = None
    #: The immutable classification input this evaluation used (C-1). Part of
    #: the record's IDENTITY, so it is passed to ``compute_result_hash``
    #: separately rather than through ``result_fields()``.
    classification_snapshot_key: Optional[str] = None
    #: The break-treatment profile that governed this evaluation (P2-D4). A
    #: RULE, so like ``policy_version`` it is identity rather than result.
    break_treatment_profile_ref: Optional[str] = None
    #: MODEL-NETTING (D-6.11). The content-addressed digest of the day records
    #: this evaluation FOLDED. Identity, not result: a day whose fold input
    #: changed is a different evaluation of the same day, and without it a
    #: corrected Monday would make Tuesday collide instead of supersede.
    #: NULL wherever no fold ran.
    bank_input_key: Optional[str] = None

    # ── §35 provenance (AC-16) ──────────────────────────────────────────────
    #: §35.1 / INV-8: the pre-rounding value, without which the figure cannot
    #: be verified against the profile that rounded it. §28.1 shows it to the
    #: Worker so the arithmetic is checkable by hand.
    qualifying_seconds: Optional[int] = None
    #: §10.2 -- which rank won, and which object supplied it.
    requirement_source: Optional[str] = None
    requirement_source_rank: Optional[int] = None
    requirement_source_ref: Optional[str] = None
    #: §16.6, §15, §12.2 -- the rules that GOVERNED this evaluation.
    rounding_profile: Optional[str] = None
    netting_mode: Optional[str] = None
    timezone_iana: Optional[str] = None
    utc_offset_seconds: Optional[int] = None
    #: §35.1 -- the engine that produced it.
    calculation_version: Optional[str] = None
    #: INV-4 / §35: observed and qualifying, per state. They differ wherever
    #: the profile does not admit a state -- which is the common case for meal.
    brk_m_seconds: Optional[int] = None
    brk_m_qualifying_seconds: Optional[int] = None
    brk_r_seconds: Optional[int] = None
    brk_r_qualifying_seconds: Optional[int] = None

    _MINUTE_FIELDS = (
        "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
        "exclusion_minutes", "attributable_minutes",
        # §10.2: an unevaluated day may not carry a credit either. A bank
        # credit asserts that a fold ran and produced a number.
        "bank_credit_minutes",
        "contributes_to_accumulator_minutes",
    )
    #: Everything a deferred day genuinely cannot know. Contribution is not
    #: here: a deferred day truthfully contributes nothing (S2).
    _MEASUREMENT_FIELDS = (
        "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
        "exclusion_minutes", "attributable_minutes", "schedule_variance_minutes",
        "grace_minutes_applied", "bank_credit_minutes",
    )

    def _check_deferral_reason(self) -> None:
        """D-1's fail-closed rule, enforced where the row is CONSTRUCTED.

        A deferral reason is meaningful only on the deferred carrier. On any
        other state it would assert that an evaluated — or explicitly
        unevaluated — day was deferred, which is a contradiction no caller
        should be able to persist. Unknown reasons are refused for the same
        reason the classification vocabulary refuses a near-miss: a value
        nobody governs must not become a determination.
        """
        reason = self.evaluation_deferral_reason
        if reason is None:
            return
        if self.state not in REASON_BEARING_STATES:
            raise ValueError(
                f"{self.state.value} must not carry "
                f"evaluation_deferral_reason {reason!r}; only "
                f"{sorted(s.value for s in REASON_BEARING_STATES)} may be "
                f"deferred, and a reason on any other state is a contradiction"
            )
        if not is_valid_deferral_reason(reason):
            raise ValueError(
                f"evaluation_deferral_reason {reason!r} is not one of "
                f"{sorted(r.value for r in EvaluationDeferralReason)}"
            )

    def __post_init__(self) -> None:
        self._check_deferral_reason()
        if self.state is EcoDayState.EVALUABLE:
            # S1: readiness is internal. It must never reach a DayRecord.
            raise ValueError(
                "EVALUABLE is a pre-computation readiness state and is never a "
                "DayEvaluation result; it must resolve to SHORTFALL or "
                "NO_SHORTFALL"
            )

        if self.state in COMPUTED_STATES:
            missing = [f for f in self._MINUTE_FIELDS if getattr(self, f) is None]
            if missing:
                raise ValueError(
                    f"{self.state.value} must carry every minute field; missing "
                    f"{missing}"
                )
            if not self.shortfall_computed:
                raise ValueError(
                    f"{self.state.value} carries minutes but shortfall_computed "
                    f"is False"
                )
            self._check_grace()
            return

        if self.state in DEFERRED_STATES:
            # Exactly one truthful zero, and no measurement at all (S2).
            invented = [
                f for f in self._MEASUREMENT_FIELDS if getattr(self, f) is not None
            ]
            if invented:
                raise ValueError(
                    f"{self.state.value} must not carry {invented}; a deferred "
                    f"day has no measurement to report"
                )
            if self.contributes_to_accumulator_minutes != 0:
                raise ValueError(
                    f"{self.state.value} must contribute exactly 0, got "
                    f"{self.contributes_to_accumulator_minutes!r}"
                )
            if self.shortfall_computed:
                raise ValueError(
                    f"{self.state.value} cannot report shortfall_computed=True"
                )
            return

        # Grace is included explicitly: it is not a ``_MINUTE_FIELDS`` member
        # (a computed day may carry none), but an UNEVALUATED day must not
        # carry it either — a policy that was never applied has no value here.
        present = [
            f for f in (*self._MINUTE_FIELDS, "grace_minutes_applied")
            if getattr(self, f) is not None
        ]
        if present:
            raise ValueError(
                f"{self.state.value} must not carry {present}; §10.2 forbids "
                f"an unevaluated day carrying a number"
            )
        if self.shortfall_computed:
            raise ValueError(
                f"{self.state.value} cannot report shortfall_computed=True"
            )

    def _check_grace(self) -> None:
        """A computed day's arithmetic must reconcile, or say why it does not.

        Without grace the identity is absolute::

            attributable = gross - exclusion

        §11 grace is the ONLY authorised way to break it, and it breaks it in
        exactly one direction: a remainder at or below the grace is zeroed
        while ``gross_shortfall_minutes`` and ``exclusion_minutes`` keep their
        true values. So a row where the identity fails is admissible only when
        ``grace_minutes_applied`` accounts for the whole difference.

        This is the structural half of the owner ruling that no persisted state
        may hold ``gross - exclusion != attributable`` without recording the
        grace that explains it. Enforced here rather than at the writer so that
        an unexplained row cannot be CONSTRUCTED, let alone stored — the same
        approach ``__post_init__`` already takes to §10.2.
        """
        grace = self.grace_minutes_applied
        if grace is not None and grace < 0:
            raise ValueError(
                f"grace_minutes_applied must be >= 0, got {grace!r}")

        remainder = self.gross_shortfall_minutes - self.exclusion_minutes
        if self.attributable_minutes == remainder:
            return

        if grace is None:
            raise ValueError(
                f"attributable_minutes {self.attributable_minutes} does not "
                f"reconcile with gross {self.gross_shortfall_minutes} - "
                f"exclusion {self.exclusion_minutes} = {remainder}, and no "
                f"grace_minutes_applied explains the difference"
            )
        if not (self.attributable_minutes == 0 and remainder <= grace):
            raise ValueError(
                f"grace_minutes_applied {grace} does not explain "
                f"attributable_minutes {self.attributable_minutes} against a "
                f"remainder of {remainder}; §11 grace zeroes a remainder at or "
                f"below it and does nothing else"
            )

    def result_fields(self) -> dict:
        """The normative result, for hashing and persistence."""
        return {
            "state": self.state.value,
            "shortfall_computed": self.shortfall_computed,
            **{f: getattr(self, f) for f in self._MINUTE_FIELDS},
            "schedule_variance_minutes": self.schedule_variance_minutes,
            # Part of the RESULT and therefore of the digest: two days
            # differing only by the bank they consumed are not the same
            # evaluation, and a replay whose fold moved must not read as
            # identical (D-6.12).
            "bank_credit_minutes": self.bank_credit_minutes,
            # Part of the RESULT, so it joins the digest: two days differing
            # only by the grace that governed them are not the same evaluation,
            # and a replay that silently changed it must not read as identical.
            "grace_minutes_applied": self.grace_minutes_applied,
            # D-1: "not cosmetic metadata … part of the authoritative
            # evaluation meaning". A day deferred for classification and one
            # deferred for a missing clock-out are different results, and the
            # digest has to say so.
            "evaluation_deferral_reason": self.evaluation_deferral_reason,
            # §35.1 / INV-8: part of the RESULT, so it joins the digest. Two
            # evaluations rounding to the same minute from different seconds
            # are not the same evaluation, and a replay whose fold moved
            # sub-minute must not read as identical.
            "qualifying_seconds": self.qualifying_seconds,
            # INV-4: a day whose meal was reclassified as rest is a different
            # evaluation even when the minutes land the same, and the digest
            # has to say so. GV-03 exists to catch exactly that collapse.
            "brk_m_seconds": self.brk_m_seconds,
            "brk_m_qualifying_seconds": self.brk_m_qualifying_seconds,
            "brk_r_seconds": self.brk_r_seconds,
            "brk_r_qualifying_seconds": self.brk_r_qualifying_seconds,
        }


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_unverified_exit_provenance(provenance) -> bool:
    """Is this provenance value a departure that nobody verified? (§24.7)

    Two conditions, and both are load-bearing:

    * it must be a **closure** provenance at all. The ledger's provenance
      vocabulary is wider than the departure vocabulary — ``PRODUCER_DERIVED``
      marks an interval that ends at a producer-authored carve-out rather than
      at a departure, and ``BREAK_DERIVED`` marks a break boundary. Neither is
      an exit, so neither can be an unverified one. Judging "anything that is
      not WORKER or ADMIN_OVERRIDE" would defer every ordinary day containing
      a break or an offline window.
    * among closure provenance, ``VERIFIED_DEPARTURE_PROVENANCE`` is the
      authority — read, never restated, so this cannot drift from the ledger's
      own vocabulary.

    Deliberately NOT the storage constraint's admissible set: that one widened
    for §24.7 so an unverified exit could be RECORDED, and reading it here
    would make a synthesized close count as verified — the exact conflation the
    widening had to avoid. NULL is not unverified either: absence of a
    departure is §24.6's business, not §24.7's.
    """
    from app.work_ledger.states import (
        SYNTHESIZED_CLOSURE_PROVENANCE,
        VERIFIED_DEPARTURE_PROVENANCE,
    )

    if provenance is None:
        return False
    closure = {p.value for p in
               VERIFIED_DEPARTURE_PROVENANCE | SYNTHESIZED_CLOSURE_PROVENANCE}
    if provenance not in closure:
        # Not a departure at all, and an unrecognised value is an integrity
        # question the ledger already owns — never silently a deferral.
        return False
    return provenance not in {p.value for p in VERIFIED_DEPARTURE_PROVENANCE}


def _has_unverified_exit(ledger, intervals: Sequence) -> bool:
    """Does this sealed day contain unresolved exit evidence? (§19 step 6)

    THE PREDICATE IS OVER THE WHOLE SEALED INTERVAL SET, not over the day's
    final departure. §19 step 6 asks ``intervals.hasUnresolvedExit`` and §9.3
    says "if the exit is unverified, qualifying minutes for THAT DAY are not
    treated as final" — both are properties of the day's evidence, not of
    whichever session happened to close last.

    An earlier version read only ``ledger.departure_provenance``. A day whose
    morning session was auto-closed and whose afternoon session the Worker
    closed properly has ``final_departure()`` — correctly — selecting the
    WORKER close, so that row said "verified" while the sealed intervals still
    held an unverified exit. The day evaluated normally and counted minutes
    derived from an exit nobody verified. Reading the interval set is what
    §19 asked for in the first place.

    ``final_departure()`` is unchanged and still selects the actual latest
    departure. A day may therefore legitimately carry a verified final
    departure AND unresolved evidence; this function is the one that notices.

    Canonical sealed evidence only — the ledger row and its frozen intervals.
    No ``ClockSession``, no raw session state, no timestamps, no inference.
    """
    if _is_unverified_exit_provenance(
            getattr(ledger, "departure_provenance", None)):
        return True
    return any(
        _is_unverified_exit_provenance(getattr(i, "provenance", None))
        for i in intervals
    )


def readiness(ledger_state: str, requirement) -> Optional[EcoDayState]:
    """Is this day eligible for computation? (S1)

    Returns ``EVALUABLE`` when the ledger is sealed and a requirement resolved —
    the two authoritative inputs the fold needs. Returns ``None`` otherwise,
    meaning some earlier branch owns the outcome.

    Pure and separately testable, so "the day is ready" is a stated condition
    rather than an implicit consequence of not having returned yet. The caller
    never persists ``EVALUABLE``.
    """
    if ledger_state != LedgerState.SEALED.value:
        return None
    if requirement is None or requirement.outcome is not ResolutionOutcome.RESOLVED:
        return None
    return EcoDayState.EVALUABLE


def evaluate_day(
    *,
    ledger,
    intervals: Sequence,
    provider: ConfigVersionProvider,
    leave_overlap_seconds: int = 0,
    policy_version: Optional[str] = None,
    grace_minutes: Optional[int] = None,
    classification_snapshot_key: Optional[str] = None,
    break_treatment_profile_ref: Optional[str] = None,
    qualifying_break_seconds: int = 0,
    bank_credit_minutes: int = 0,
    bank_input_key: Optional[str] = None,
    netting_mode: Optional[str] = None,
    brk_m_seconds: Optional[int] = None,
    brk_m_qualifying_seconds: Optional[int] = None,
    brk_r_seconds: Optional[int] = None,
    brk_r_qualifying_seconds: Optional[int] = None,
) -> DayEvaluation:
    """Fold one sealed Worker-day into a derived evaluation. Pure, no I/O.

    ``ledger`` is the authoritative ``WorkDayLedger`` row; ``intervals`` are its
    sealed ``WorkStateInterval`` rows. Both come from Step 1, which remains the
    authority for the evidence.

    ``leave_overlap_seconds`` is the approved-leave overlap for this work date,
    resolved by ``exclusion.approved_leave_overlap`` at the recording boundary —
    this function stays pure and reads no database. It is SECONDS so the
    conversion happens here, at the single rounding site, rather than in a
    second place. Zero means no approved leave overlapped the day, which is a
    determination the recording boundary made; an authority it could NOT
    establish is refused there and never reaches this function.

    ``policy_version`` and ``grace_minutes`` are the IDENTITY and the VALUE of
    the same §11 policy, resolved historically against this work date at the
    recording boundary. They are passed together and never separately: a grace
    with no policy identity is a number with no provenance, so this function
    refuses it rather than applying it (MODEL-GRACE). Nothing here reads a
    policy, a database or a clock — the resolution stays at the boundary and
    this function stays pure.

    Deterministic: the fold sums integer seconds and does not depend on
    iteration order, database ordering, or the current time. The only
    seconds-to-minutes conversion happens at the single rounding site.
    """
    if grace_minutes is not None:
        if policy_version is None:
            raise ValueError(
                "grace_minutes was supplied without a policy_version; §11 "
                "grace is an attribute of a versioned policy and must not be "
                "applied without the identity it came from"
            )
        if grace_minutes < 0:
            raise ValueError(
                f"grace_minutes must be >= 0, got {grace_minutes!r}")

    organization_id = getattr(ledger, "organization_id", None)
    if organization_id is None:
        raise EvaluationRefused(
            REASON_ORGANIZATION_REQUIRED,
            f"ledger {getattr(ledger, 'id', None)} has no organization_id",
        )

    user_id = ledger.user_id
    work_date = _as_date(ledger.work_date)
    ledger_version = ledger.ledger_version
    ledger_state = ledger.state

    # ── 1. Ledger conditions that forbid any evaluation (R10) ───────────────
    if ledger_state == LedgerState.LEDGER_INTEGRITY_FAULT.value:
        raise EvaluationRefused(
            REASON_LEDGER_INTEGRITY_FAULT,
            f"{work_date} holds inadmissible evidence "
            f"({ledger.integrity_fault_reason!r}); §24.15 refuses the day rather "
            f"than repairing, clamping or skipping it",
        )
    # ── 1b. Missing clock-out defers, it does not refuse (S2, S3) ───────────
    # The canonical ledger keeps its own MISSING_CLOCK_OUT condition; this is
    # the ECO-domain counterpart. A result IS persisted — deferral is a fact
    # worth recording, and §24.2's reasoning applies: a day the week fold skips
    # silently is a week nobody can explain. Nothing is fabricated: every
    # measurement stays NULL, and §7.2's prohibition on inferring an end of day
    # is untouched because no departure is assumed.
    if ledger_state == LedgerState.MISSING_CLOCK_OUT.value:
        return DayEvaluation(
            policy_version=policy_version,
            classification_snapshot_key=classification_snapshot_key,
            break_treatment_profile_ref=break_treatment_profile_ref,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, ledger_version=ledger_version,
            **_provenance(ledger, netting_mode=netting_mode),
            state=EcoDayState.DEFERRED_PENDING_LEDGER,
            # Deferral is decided before any requirement arithmetic: S3 permits
            # no more than is necessary to establish the state.
            requirement_outcome="NOT_APPLICABLE",
            shortfall_computed=False,
            contributes_to_accumulator_minutes=0,
        )

    # ── 1c. Unverified exit defers too — §24.7, and NOT §24.6 ──────────────
    # §19 puts this decision here on purpose: step 5 loads the sealed
    # intervals, step 6 tests the exit, and only step 7 asks whether a
    # clock-out exists at all. So the day IS sealed, with real intervals, and
    # the deferral is an evaluation judgement about frozen evidence rather
    # than anything the producer decided.
    #
    # The evidence being read is the whole sealed interval set plus the day's
    # departure columns, all frozen at seal and immutable thereafter — §19
    # step 6's predicate is ``intervals.hasUnresolvedExit``, so ANY unverified
    # exit in the day's evidence defers it, even when a later session closed
    # with a verified departure. A synthesized close — STALE_AUTOCLOSE,
    # ABANDONED_AUTOCLOSE, OFFBOARDING — is an exit that EXISTS and was not
    # performed by the Worker or an administrator, so §24.7 applies:
    # "Shortfall evaluation for that day is deferred … The day contributes 0
    # to the accumulator while deferred and is neither counted nor excused."
    #
    # Without this branch a SEALED row carrying a synthesized departure would
    # fall through and evaluate exactly like a genuine one — computing a
    # shortfall and contributing minutes, which is the opposite of §24.7 and
    # would erase the distinction §19.1 exists to protect.
    #
    # It reaches the SAME ECO state as a missing clock-out, which both §24.6
    # and §24.7 require. They stay distinguishable where it matters: the
    # ledger. This day has a departure instant; a missing clock-out has none.
    if ledger_state == LedgerState.SEALED.value and \
            _has_unverified_exit(ledger, intervals):
        return DayEvaluation(
            policy_version=policy_version,
            classification_snapshot_key=classification_snapshot_key,
            break_treatment_profile_ref=break_treatment_profile_ref,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, ledger_version=ledger_version,
            **_provenance(ledger, netting_mode=netting_mode),
            state=EcoDayState.DEFERRED_PENDING_LEDGER,
            # Identical shape to the missing-clock-out deferral above: no
            # requirement resolved, no measurement fabricated, every minute
            # field left NULL. §9.3 — "qualifying minutes for that day are not
            # treated as final" — is honoured by not computing them at all.
            requirement_outcome="NOT_APPLICABLE",
            shortfall_computed=False,
            contributes_to_accumulator_minutes=0,
        )

    # ── 2. Not a working day — the R5 short-circuit ─────────────────────────
    # Taken BEFORE requirement resolution: the zero here means "no work was
    # expected", not "a resolved requirement of zero", and Step 5 rightly
    # refuses to produce zero as a requirement.
    #
    # THE CLASSIFICATION IS READ, NEVER RE-RESOLVED (D3-A, D16-A)
    # ----------------------------------------------------------
    # `classification_from_sealed` reads only the ledger's frozen columns and
    # takes no session, so this branch structurally cannot consult current
    # schedule configuration for a historical day — a later change to a
    # roster, a working-week setting or the holiday table cannot move a sealed
    # day's classification. An import-graph test pins that this module reaches
    # neither `schedule_policy` nor `worker_context`.
    #
    # It is also why absence of work cannot classify: the classifier never
    # sees `intervals`, so a sealed working day with none stays a working day
    # and may become a shortfall (D17-A).
    classification = classification_from_sealed(ledger)
    if classification.outcome is ClassificationOutcome.NOT_A_WORKING_DAY:
        return DayEvaluation(
            policy_version=policy_version,
            classification_snapshot_key=classification_snapshot_key,
            break_treatment_profile_ref=break_treatment_profile_ref,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, ledger_version=ledger_version,
            **_provenance(ledger, netting_mode=netting_mode),
            state=EcoDayState.NOT_A_WORKING_DAY,
            requirement_outcome="NOT_APPLICABLE",
            shortfall_computed=True,
            required_minutes=0, qualifying_minutes=0,
            gross_shortfall_minutes=0, exclusion_minutes=0,
            attributable_minutes=0, schedule_variance_minutes=0,
            # R5 zeroes everything on a non-working day. No surplus can
            # arise from a day with no requirement, and none is banked.
            bank_credit_minutes=0,
            contributes_to_accumulator_minutes=0,
        )

    if ledger_state != LedgerState.SEALED.value:
        raise EvaluationRefused(
            REASON_LEDGER_NOT_SEALED,
            f"{work_date} is in ledger state {ledger_state!r}; only a SEALED day "
            f"is evaluable, and no evaluation state is authorised for the others",
        )

    # ── 3. Resolve the requirement (R13) ────────────────────────────────────
    requirement = resolve_requirement_strict(
        organization_id=organization_id, user_id=user_id,
        as_of=work_date, provider=provider,
    )
    if requirement.outcome is not ResolutionOutcome.RESOLVED:
        # Both NO_VERSION and REQUIREMENT_UNRESOLVED map here. R4 supplies one
        # unresolved evaluation state; the Step 5 outcome is preserved so the
        # distinction is not lost.
        return DayEvaluation(
            policy_version=policy_version,
            classification_snapshot_key=classification_snapshot_key,
            break_treatment_profile_ref=break_treatment_profile_ref,
            organization_id=organization_id, user_id=user_id,
            work_date=work_date, ledger_version=ledger_version,
            # `requirement` exists but did NOT resolve: the rank it reached is
            # still evidence, and §24.3 makes this a Tenant configuration
            # defect somebody has to diagnose.
            **_provenance(ledger, requirement, netting_mode=netting_mode),
            state=EcoDayState.REQUIREMENT_UNRESOLVED,
            requirement_outcome=requirement.outcome.value,
            shortfall_computed=False,
            config_version=requirement.config_version_id,
        )

    # ── 4. Only WRK qualifies in this increment (R6) ────────────────────────
    foreign = sorted({
        i.state for i in intervals if i.state not in QUALIFYING_STATES
    })
    if foreign:
        raise EvaluationRefused(
            REASON_NON_WRK_STATE_PRESENT,
            f"{work_date} contains {foreign}, whose qualification is not ruled. "
            f"R6 authorises WRK only; deciding the rest needs the classifier and "
            f"the Legal Matrix, neither of which is authorised",
        )

    # ── 5. Readiness, then the fold (S1, R12) ───────────────────────────────
    # EVALUABLE is the readiness gate: every authoritative input is present and
    # the day is eligible, but no classification has been produced yet. It is
    # computed here and never persisted — S1 requires a successful evaluation to
    # resolve to SHORTFALL or NO_SHORTFALL.
    if readiness(ledger_state, requirement) is not EcoDayState.EVALUABLE:
        raise EvaluationRefused(
            REASON_LEDGER_NOT_SEALED,
            f"{work_date} reached the fold without being evaluable; this is a "
            f"logic fault rather than a data condition",
        )

    # Integer seconds throughout. Sum is order-independent, so the result does
    # not depend on how the database returned the rows.
    #
    # ``qualifying_break_seconds`` is the P-2 contribution: already classified,
    # already treated under the governing profile, and already CAPPED — all of
    # it resolved at the recording boundary (owner ruling P2-D1) because this
    # function reads no database. It arrives in SECONDS for the same reason
    # ``leave_overlap_seconds`` does: so the single ROUND_HALF_UP site below
    # converts the whole qualifying pool ONCE. Adding a second rounding here
    # would contradict GV-12's locked ruling (P2-D2).
    #
    # Breaks are GAPS in the sealed ledger (D-18), so this can never
    # double-count an interval: no BRK_M or BRK_R interval exists to sum.
    qualifying_seconds = (sum(int(i.duration_seconds) for i in intervals)
                          + max(int(qualifying_break_seconds), 0))
    qualifying_minutes = round_half_up_minutes(qualifying_seconds)

    required_minutes = requirement.required_minutes

    # ── §18 steps 10-11: the bank, BEFORE gross (D-6.4) ───────────────
    #
    #     10  bank  := bankCredit(worker, work_date, policy.netting_mode)
    #     11  gross := MAX(0, req.minutes - q - bank)
    #
    # Folded at the recording boundary (MODEL-NETTING reads other day
    # records; this function reads no database), and arriving already
    # capped per day and per period. It is subtracted from the REQUIREMENT,
    # so exclusions at step 12 and grace at step 13 both act on a gap the
    # bank has already reduced — which is what stops leave becoming surplus
    # and stops grace returning credit to the bank.
    #
    # Clamped at zero here as a second line of defence only: the fold never
    # returns a negative credit, and a negative arriving would mean a bank
    # that owed minutes rather than supplied them.
    bank_credit = max(int(bank_credit_minutes), 0)
    gross = max(required_minutes - qualifying_minutes - bank_credit, 0)

    # ── MODEL-EXCLUSION (authorised; GV-13/14/15) ───────────────────────────
    # Leave overlap arrives as SECONDS and is converted here, at the one
    # rounding site, by the same function that rounds qualifying minutes. The
    # total is rounded ONCE, exactly as qualifying seconds are — not per
    # interval, and never again downstream.
    leave_overlap_minutes = round_half_up_minutes(max(int(leave_overlap_seconds), 0))

    # Leave reduces an attributable shortfall and never creates credit: capped
    # at the gross gap, so coverage beyond it is simply unused. A day with no
    # gap therefore excludes nothing, however much leave overlapped it.
    exclusion_minutes = min(gross, leave_overlap_minutes)
    attributable = max(gross - exclusion_minutes, 0)

    # ── 6. §11 grace, AFTER the exclusion arithmetic (step 13) ──────────────
    #
    #     IF attributable <= req.grace_minutes: attributable := 0
    #
    # De-minimis, and nothing more. It zeroes what is attributed; it does not
    # touch the gross gap or the exclusion, which stay exactly what the evidence
    # and the leave made them. §11 introduces the attribute so this decision has
    # ONE named home rather than becoming "a magic number in the threshold
    # comparison, which is where it does the most damage" — so no threshold is
    # consulted here, and nothing downstream re-applies it.
    #
    # ``grace_minutes_applied`` records the grace this day was actually judged
    # against, not only the cases where it changed the answer: a reader asking
    # "was three minutes forgiven, or was there nothing to forgive?" needs the
    # value either way, and ``DayEvaluation`` requires it wherever the identity
    # gross - exclusion = attributable no longer holds.
    if grace_minutes is not None and attributable <= grace_minutes:
        attributable = 0

    return DayEvaluation(
        policy_version=policy_version,
        classification_snapshot_key=classification_snapshot_key,
        break_treatment_profile_ref=break_treatment_profile_ref,
        grace_minutes_applied=grace_minutes,
        organization_id=organization_id, user_id=user_id,
        work_date=work_date, ledger_version=ledger_version,
        **_provenance(ledger, requirement, netting_mode=netting_mode),
        qualifying_seconds=qualifying_seconds,
        brk_m_seconds=brk_m_seconds,
        brk_m_qualifying_seconds=brk_m_qualifying_seconds,
        brk_r_seconds=brk_r_seconds,
        brk_r_qualifying_seconds=brk_r_qualifying_seconds,
        state=EcoDayState.SHORTFALL if gross > 0 else EcoDayState.NO_SHORTFALL,
        requirement_outcome=requirement.outcome.value,
        shortfall_computed=True,
        config_version=requirement.config_version_id,
        required_minutes=required_minutes,
        qualifying_minutes=qualifying_minutes,
        gross_shortfall_minutes=gross,
        exclusion_minutes=exclusion_minutes,
        attributable_minutes=attributable,
        # §18 step 14, computed at the single site above from FROZEN evidence
        # only. None when either operand is absent — never 0 (D-2.1).
        schedule_variance_minutes=schedule_variance_minutes(ledger),
        # §18 step 15 records the credit on the day that spent it.
        bank_credit_minutes=bank_credit,
        # D-6.11: WHICH fold produced it. Identity, so a corrected upstream
        # day supersedes rather than collides.
        bank_input_key=bank_input_key,
        # §20: the ACCUMULATOR takes the attributable figure, which is
        # already net of the bank (via gross), of exclusions and of grace.
        # Three distinct quantities: `bank_credit_minutes` is what the week
        # supplied, `attributable_minutes` is what this day is answerable
        # for, and this is what reaches the weekly threshold.
        contributes_to_accumulator_minutes=attributable,
    )


# ── Persistence (R1, R2, R15) ───────────────────────────────────────────────
#: Bounded retries when a competitor claims the authoritative generation first.
#: Small and finite: sustained contention should surface, not spin.
_MAX_AUTHORITY_ATTEMPTS = 4


def _existing(
    db: Session, ev: DayEvaluation, leave_input_snapshot_key: str
) -> Optional[EcoDayRecord]:
    """The record for this exact ELEVEN-part snapshot identity, if any.

    Seven components until owner rulings C-1 and D-1 added the classification
    snapshot and the deferral reason. Both must be matched here or the lookup
    would disagree with ``uq_eco_day_record_snapshot``: a day re-evaluated
    under a new classification would find its predecessor, be read as a replay
    with a different hash, and raise a conflict instead of superseding.
    """
    stmt = select(EcoDayRecord).where(
        EcoDayRecord.organization_id == ev.organization_id,
        EcoDayRecord.user_id == ev.user_id,
        EcoDayRecord.work_date == ev.work_date,
        EcoDayRecord.ledger_version == ev.ledger_version,
        EcoDayRecord.leave_input_snapshot_key == leave_input_snapshot_key,
    )
    for column, value in (
        (EcoDayRecord.policy_version, ev.policy_version),
        (EcoDayRecord.config_version, ev.config_version),
        # D-6.11: the fold's inputs. Matched here or a corrected upstream day
        # would find its predecessor, be read as a replay with a different
        # hash, and raise a conflict instead of superseding.
        (EcoDayRecord.bank_input_key, ev.bank_input_key),
        (EcoDayRecord.classification_snapshot_key,
         ev.classification_snapshot_key),
        (EcoDayRecord.evaluation_deferral_reason,
         ev.evaluation_deferral_reason),
        (EcoDayRecord.break_treatment_profile_ref,
         ev.break_treatment_profile_ref),
    ):
        stmt = stmt.where(column.is_(None) if value is None else column == value)
    return db.execute(stmt).scalars().first()


def _authoritative(db: Session, ev: DayEvaluation) -> Optional[EcoDayRecord]:
    """The current record for this Worker-day — ``superseded_by IS NULL``."""
    return db.execute(
        select(EcoDayRecord)
        .where(
            EcoDayRecord.organization_id == ev.organization_id,
            EcoDayRecord.user_id == ev.user_id,
            EcoDayRecord.work_date == ev.work_date,
            EcoDayRecord.superseded_by.is_(None),
        )
        .order_by(EcoDayRecord.evaluation_generation.desc())
    ).scalars().first()


class _StalePredecessor(Exception):
    """The predecessor this attempt read was no longer current when linked.

    Internal control flow, never raised to a caller: the attempt is rolled back
    and retried against a freshly read predecessor.
    """


def _generation_after(predecessor: Optional[EcoDayRecord]) -> int:
    """The generation a successor to this predecessor must claim.

    DERIVED FROM THE PREDECESSOR, deliberately — never from
    ``MAX(evaluation_generation)`` read separately. The two reads could
    disagree: a competitor committing between them yields a FRESH generation
    beside a STALE predecessor, so the unique generation constraint accepts the
    insert and the predecessor link then overwrites the competitor's. Deriving
    the generation from the predecessor makes the constraint guard the very row
    the attempt read — which is exactly why ``WorkDayLedger.supersede_day``
    uses ``current.ledger_version + 1`` rather than a separate maximum.

    No predecessor means this is the day's first record, so generation 1. If
    rows nonetheless exist for the day, the insert collides and the error is
    re-raised rather than worked around: "superseded rows exist but none is
    current" is a broken state, not something to allocate past.
    """
    if predecessor is None:
        return 1
    return int(predecessor.evaluation_generation) + 1


def _generation_taken(db: Session, ev: DayEvaluation, generation: int) -> bool:
    """Whether some row already holds this generation for this Worker-day.

    Asked POSITIVELY after a failed insert, so a generation collision is
    identified rather than assumed. Anything that is neither a duplicate
    identity nor a taken generation is a different constraint failing, and the
    caller must see that error unchanged.
    """
    return db.execute(
        select(func.count()).select_from(EcoDayRecord).where(
            EcoDayRecord.organization_id == ev.organization_id,
            EcoDayRecord.user_id == ev.user_id,
            EcoDayRecord.work_date == ev.work_date,
            EcoDayRecord.evaluation_generation == generation,
        )
    ).scalar() > 0


def _conflict(evaluation: DayEvaluation) -> DayRecordConflict:
    return DayRecordConflict(
        f"snapshot (org={evaluation.organization_id}, "
        f"user={evaluation.user_id}, {evaluation.work_date}, "
        f"ledger_version={evaluation.ledger_version}) already holds a "
        f"different result for the same authoritative inputs. Every "
        f"authoritative input — ledger, configuration, policy and leave "
        f"snapshot — is named in the snapshot identity, so the evaluation "
        f"logic itself produced a different answer."
    )


def record_evaluation(
    db: Session,
    evaluation: DayEvaluation,
    *,
    leave_input_snapshot_key: str,
) -> EcoDayRecord:
    """Persist one evaluation, idempotently against its snapshot identity.

    ``leave_input_snapshot_key`` is the seventh identity component (Owner
    Ruling 2, F) — the key of the immutable ``LeaveInputSnapshot`` this
    evaluation's exclusion was computed from. It is REQUIRED: a day with no
    contributing leave references an explicit EMPTY snapshot, so there is no
    "no leave input" case to default for, and defaulting one would fabricate
    provenance for a day that actually had leave.

    IDEMPOTENCY AND CONFLICT
    ------------------------
    Re-evaluating the same seven-part snapshot returns the existing record and
    writes nothing. The same snapshot re-evaluating to a DIFFERENT result raises
    :class:`DayRecordConflict` — and with leave now inside the identity, that
    genuinely means the evaluation logic changed rather than that an input
    moved.

    AUTHORITY (Owner Ruling 2, H, J, K)
    -----------------------------------
    A changed authoritative input yields a NEW identity, so a new record. The
    predecessor is superseded rather than rewritten: the new row records
    ``supersedes``, and the predecessor's ``superseded_by`` is pointed at the
    new row inside the same transaction — the one mutation the immutability
    rule permits.

    ``evaluation_generation`` is what serializes this, NOT the pointer. Two
    writers with different inputs have different identities, so both inserts
    would otherwise succeed and both rows would read as authoritative. The
    generation is DERIVED FROM THE PREDECESSOR THIS ATTEMPT READ, so the unique
    constraint guards that read: a competitor that superseded the predecessor
    first has already taken the generation, and this insert is rejected. See
    ``_generation_after``.

    The predecessor link is then written as a GUARDED statement —
    ``WHERE id = :predecessor AND superseded_by IS NULL`` — and exactly one
    affected row is required. Zero means the predecessor stopped being current
    after it was read, so the attempt is abandoned and retried rather than
    overwriting whoever superseded it. The generation coupling and the guarded
    pointer are independent defences; either alone leaves the other's window.

    Every recovery is specific rather than a blanket ``except IntegrityError``,
    and there are four distinct outcomes after a failed attempt:

    * the snapshot identity now exists → the duplicate-identity race, so the
      existing record is returned (or a conflict raised if the result differs);
    * this generation is now taken → a competitor won it, so retry;
    * the guarded pointer matched no row → a stale predecessor, so retry;
    * anything else → the original error is re-raised IMMEDIATELY. An unrelated
      constraint failure is never retried and never reported as contention.

    Each attempt runs inside a SAVEPOINT, so a lost race rolls back that insert
    alone and leaves the caller's transaction intact. The caller still owns the
    transaction boundary: this flushes and never commits.
    """
    if not (leave_input_snapshot_key or "").strip():
        raise LeaveInputSnapshotRequired(
            f"no leave input snapshot key was supplied for "
            f"(org={evaluation.organization_id}, user={evaluation.user_id}, "
            f"{evaluation.work_date}); a day with no contributing leave "
            f"references an explicit empty snapshot"
        )

    fields = evaluation.result_fields()
    digest = compute_result_hash(
        bank_input_key=evaluation.bank_input_key,
        organization_id=evaluation.organization_id,
        user_id=evaluation.user_id,
        work_date=evaluation.work_date,
        ledger_version=evaluation.ledger_version,
        policy_version=evaluation.policy_version,
        config_version=evaluation.config_version,
        leave_input_snapshot_key=leave_input_snapshot_key,
        classification_snapshot_key=evaluation.classification_snapshot_key,
        break_treatment_profile_ref=evaluation.break_treatment_profile_ref,
        requirement_source=evaluation.requirement_source,
        requirement_source_rank=evaluation.requirement_source_rank,
        requirement_source_ref=evaluation.requirement_source_ref,
        rounding_profile=evaluation.rounding_profile,
        netting_mode=evaluation.netting_mode,
        timezone_iana=evaluation.timezone_iana,
        utc_offset_seconds=evaluation.utc_offset_seconds,
        result=fields,
    )

    for _ in range(_MAX_AUTHORITY_ATTEMPTS):
        existing = _existing(db, evaluation, leave_input_snapshot_key)
        if existing is not None:
            if existing.content_hash != digest:
                raise _conflict(evaluation)
            # Exact replay: no new generation, no supersession, nothing written.
            return existing

        # THE SERIALIZATION POINT. One read: the generation is derived from
        # this predecessor rather than looked up separately, so the two can
        # never disagree about which row is current.
        predecessor = _authoritative(db, evaluation)
        generation = _generation_after(predecessor)

        row = EcoDayRecord(
            organization_id=evaluation.organization_id,
            user_id=evaluation.user_id,
            work_date=evaluation.work_date,
            ledger_version=evaluation.ledger_version,
            policy_version=evaluation.policy_version,
            config_version=evaluation.config_version,
            leave_input_snapshot_key=leave_input_snapshot_key,
            classification_snapshot_key=evaluation.classification_snapshot_key,
            break_treatment_profile_ref=evaluation.break_treatment_profile_ref,
            evaluation_deferral_reason=evaluation.evaluation_deferral_reason,
            evaluation_generation=generation,
            supersedes=(predecessor.id if predecessor is not None else None),
            requirement_outcome=evaluation.requirement_outcome,
            content_hash=digest,
            state=evaluation.state.value,
            shortfall_computed=evaluation.shortfall_computed,
            required_minutes=evaluation.required_minutes,
            qualifying_minutes=evaluation.qualifying_minutes,
            gross_shortfall_minutes=evaluation.gross_shortfall_minutes,
            exclusion_minutes=evaluation.exclusion_minutes,
            attributable_minutes=evaluation.attributable_minutes,
            grace_minutes_applied=evaluation.grace_minutes_applied,
            schedule_variance_minutes=evaluation.schedule_variance_minutes,
            bank_credit_minutes=evaluation.bank_credit_minutes,
            bank_input_key=evaluation.bank_input_key,
            contributes_to_accumulator_minutes=(
                evaluation.contributes_to_accumulator_minutes
            ),
            qualifying_seconds=evaluation.qualifying_seconds,
            requirement_source=evaluation.requirement_source,
            requirement_source_rank=evaluation.requirement_source_rank,
            requirement_source_ref=evaluation.requirement_source_ref,
            rounding_profile=evaluation.rounding_profile,
            netting_mode=evaluation.netting_mode,
            timezone_iana=evaluation.timezone_iana,
            utc_offset_seconds=evaluation.utc_offset_seconds,
            calculation_version=evaluation.calculation_version,
            brk_m_seconds=evaluation.brk_m_seconds,
            brk_m_qualifying_seconds=evaluation.brk_m_qualifying_seconds,
            brk_r_seconds=evaluation.brk_r_seconds,
            brk_r_qualifying_seconds=evaluation.brk_r_qualifying_seconds,
            evaluated_at_utc=datetime.now(timezone.utc),
        )

        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
                if predecessor is not None:
                    # Ruling K, enforced by the DATABASE rather than by having
                    # read the row earlier. The predicate is the proof: the
                    # statement can only match while the predecessor is still
                    # unsuperseded, so a competitor that got there first leaves
                    # rowcount 0 and this attempt is abandoned. A Core UPDATE
                    # on purpose — the guarded predicate is strictly stronger
                    # than the ORM listener's NULL check, which is why the
                    # listener is left in place for every other path.
                    affected = db.execute(
                        sa_update(EcoDayRecord)
                        .where(
                            EcoDayRecord.id == predecessor.id,
                            EcoDayRecord.superseded_by.is_(None),
                        )
                        .values(superseded_by=row.id)
                        .execution_options(synchronize_session=False)
                    ).rowcount
                    if affected != 1:
                        raise _StalePredecessor(
                            f"predecessor {predecessor.id} was superseded by "
                            f"another writer ({affected} rows matched)"
                        )
                    # The session's copy still says NULL; the database does not.
                    db.expire(predecessor, [SUPERSESSION_FIELD])
        except _StalePredecessor:
            if row in db:
                db.expunge(row)
            # Drop the stale copy so the retry's read is answered by the
            # database rather than the identity map.
            db.expire(predecessor)
            continue
        except IntegrityError as exc:
            # Discard the rejected pending row, or the next flush retries it.
            if row in db:
                db.expunge(row)

            raced = _existing(db, evaluation, leave_input_snapshot_key)
            if raced is not None:
                if raced.content_hash != digest:
                    raise _conflict(evaluation) from exc
                return raced

            if _generation_taken(db, evaluation, generation):
                # A competitor holds the generation this attempt derived.
                # Re-read the predecessor and retry.
                continue
            # Neither a duplicate identity nor a contended generation, so some
            # OTHER constraint failed. Re-raise it unchanged and immediately:
            # retrying cannot help, and reporting it as contention would hide
            # the real cause.
            raise

        return row

    raise DayRecordAuthorityContention(
        f"could not claim authority for (org={evaluation.organization_id}, "
        f"user={evaluation.user_id}, {evaluation.work_date}) in "
        f"{_MAX_AUTHORITY_ATTEMPTS} attempts: the current record kept moving "
        f"under this writer. Raised ONLY for repeated contention — an "
        f"unrelated constraint failure is re-raised as itself. Re-read and "
        f"re-run"
    )


def get_day_record(
    db: Session, *, organization_id: int, user_id: int, work_date: date
) -> Sequence[EcoDayRecord]:
    """Every derived evaluation for a Worker-day, newest snapshot last.

    Plural by design: a day legitimately has one record per snapshot, and a
    correction to the ledger or the configuration produces another. Collapsing
    them to "the" record would hide the supersession R1 requires.
    """
    return tuple(db.execute(
        select(EcoDayRecord)
        .where(
            EcoDayRecord.organization_id == organization_id,
            EcoDayRecord.user_id == user_id,
            EcoDayRecord.work_date == work_date,
        )
        .order_by(EcoDayRecord.ledger_version, EcoDayRecord.id)
    ).scalars().all())


__all__ = [
    "QUALIFYING_STATES",
    "DayEvaluation",
    "readiness",
    "evaluate_day",
    "record_evaluation",
    "get_day_record",
]
