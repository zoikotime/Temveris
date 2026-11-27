"""
The EVAL-DAY side of break classification — boundary-side, never pure.
======================================================================

``evaluate_day`` reads no database; this module does the reading, exactly as
``exclusion.approved_leave_overlap`` does for leave. It answers one question:

    does this sealed day require break classification, and if so, how many
    qualifying break SECONDS does the governing profile admit?

WHEN A DAY REQUIRES CLASSIFICATION
----------------------------------
Only when a ``BreakTreatmentProfile`` governs the work date. That is derived
from owner ruling P-4, which gates rest qualification on "the resolved
break-treatment profile" — with no profile in force there is no rule by which a
classification could change any arithmetic, so the day evaluates exactly as it
always has and ``classification_snapshot_key`` stays NULL.

This is deliberately NOT a fail-open. Once a profile exists, every break on the
day must carry a live authoritative assertion or the day defers (D-1). What the
profile gate decides is whether the CONTROL applies at all, not whether an
applicable control may be skipped. And only ``NO_VERSION`` means "no profile":
``AMBIGUOUS`` and ``INVALID`` are resolution FAILURES and refuse the day.

WHERE THE DURATION COMES FROM (owner ruling P2-D1)
---------------------------------------------------
Breaks are GAPS in the canonical ledger (D-18), so they are not intervals and
their durations cannot be read off one. They are read here, at the boundary,
from the break sessions the producer already carves out — keyed by the identical
``break_session:<id>`` source ref the producer writes into its windows, so the
assertion and the carve-out name the same thing and neither has to translate.

That does NOT weaken replay. ``source_evidence_fingerprint`` digests
``[w.kind, w.source_ref, start, end]`` for every window, so a change to a
break's authoritative boundaries changes the fingerprint, supersedes the sealed
day, changes ``ledger_version`` — and ``ledger_version`` is already part of the
record identity. A duration cannot move without a new generation.

Reading that evidence is not classifying it. Nothing here consults
``break_sessions.break_type``: P-1 makes the worker-selected label
observational only, and static guards assert this module never reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.break_classification import (
    BREAK_SOURCE_PREFIX,
    REASON_PROFILE_AMBIGUOUS,
    REASON_PROFILE_INVALID,
    ClassificationOutcome,
    ClassificationResolution,
    ClassificationResult,
    resolve_classification_for_day,
)
from app.break_treatment import ProfileOutcome, resolve_profile_as_of

#: Which refusal each profile-resolution FAILURE carries. ``NO_VERSION`` is
#: deliberately absent: it is the one outcome that legitimately means "no
#: applicable profile", and it is handled before this table is consulted.
_PROFILE_REFUSAL_REASON = {
    ProfileOutcome.AMBIGUOUS: REASON_PROFILE_AMBIGUOUS,
    ProfileOutcome.INVALID: REASON_PROFILE_INVALID,
}

#: A break whose authoritative boundaries are incomplete. Its own reason: this
#: is missing EVIDENCE, not a missing determination, and conflating it with
#: ``CLASSIFICATION_UNRESOLVED`` would send an operator to the wrong place.
REASON_BREAK_EVIDENCE_UNAVAILABLE = "BREAK_EVIDENCE_UNAVAILABLE"


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class BreakTreatmentOutcome:
    """The boundary's resolved break contribution, with its provenance.

    A TRANSIENT result, not a stored artifact. Owner ruling P2-D1 forbids
    duplicating break durations into the ``ClassificationSnapshot``, which
    stays a determination artifact — so the durations live here, computed at
    evaluation time from evidence the ledger's own fingerprint already covers.

    Carries enough to explain the number without re-deriving it: which sources
    contributed, their exact authoritative boundaries, the classification
    snapshot they came from, the governing profile, and whether the cap bound.
    """

    classification: ClassificationResolution
    profile_ref: str

    #: The final contribution handed to the evaluator, in SECONDS, already
    #: capped. Rest (capped) plus meal (uncapped) where the profile qualifies
    #: each — see ``qualifying_break_seconds``.
    qualifying_break_seconds: int = 0

    #: Rest before the cap, and after. Equal when the cap did not bind.
    total_rest_seconds: int = 0
    capped_rest_seconds: int = 0
    #: Meal seconds admitted. Non-zero only where ``meal_qualifies`` is true;
    #: §13.2 defines no meal cap, so this is never capped.
    qualifying_meal_seconds: int = 0

    #: OBSERVED durations, independent of whether the profile admits them.
    #:
    #: The four fields above answer "what counted". These answer "what
    #: happened" -- §35 stores both halves for each state, and they differ
    #: exactly where a Tenant treats meal as unpaid: 45 observed, 0 qualifying.
    #: Without them a record cannot show the Worker the meal break they took
    #: (§26.1), and INV-4's distinction becomes invisible on the artifact even
    #: though the arithmetic applied it.
    observed_meal_seconds: int = 0
    observed_rest_seconds: int = 0

    #: None when the profile is uncapped (§13.2: "null = uncapped").
    cap_seconds: Optional[int] = None
    cap_applied: bool = False

    #: ``(source_ref, classification, start, end, seconds)`` per contributing
    #: break, ordered by source ref.
    contributions: Tuple[tuple, ...] = field(default_factory=tuple)

    @property
    def snapshot_key(self) -> Optional[str]:
        return self.classification.snapshot_key


def break_windows_for_day(
    db: Session, *, organization_id: int, user_id: int, work_date: date,
) -> List[Tuple[str, datetime, datetime, int]]:
    """Every break this day's evidence contains, with authoritative boundaries.

    Returns ``(source_ref, start, end, seconds)`` ordered by id. Duration is
    computed from the BOUNDARIES, never from a stored total: the boundaries are
    what ``source_evidence_fingerprint`` digests, so deriving from them is what
    keeps the number tied to the ledger version that froze it.

    A break with no end has no authoritative duration and is returned with
    ``seconds = None``, which the caller refuses rather than guessing.
    """
    import database.models as dbm

    start_of_day = datetime(work_date.year, work_date.month, work_date.day)
    end_of_day = start_of_day.replace(hour=23, minute=59, second=59,
                                      microsecond=999999)

    rows = db.execute(
        select(dbm.BreakSession.id, dbm.BreakSession.break_start,
               dbm.BreakSession.break_end).where(
            dbm.BreakSession.organization_id == organization_id,
            dbm.BreakSession.user_id == user_id,
            dbm.BreakSession.break_start >= start_of_day,
            dbm.BreakSession.break_start <= end_of_day,
        ).order_by(dbm.BreakSession.id)
    ).all()

    out: List[Tuple[str, datetime, datetime, int]] = []
    for row_id, begin, end in rows:
        seconds = (None if (begin is None or end is None)
                   else int((end - begin).total_seconds()))
        out.append((f"{BREAK_SOURCE_PREFIX}{int(row_id)}", begin, end, seconds))
    return out


def break_source_refs_for_day(
    db: Session, *, organization_id: int, user_id: int, work_date: date,
) -> List[str]:
    """Just the canonical source refs, for the classification resolver."""
    return [w[0] for w in break_windows_for_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date)]


def _refused(organization_id, user_id, on, reason, detail):
    return ClassificationResolution(
        outcome=ClassificationOutcome.REFUSED,
        organization_id=organization_id, user_id=user_id, work_date=on,
        reason=reason, detail=detail)


def classification_for_day(
    db: Session, *, organization_id: int, user_id: int,
    work_date: date | datetime, ledger,
) -> Optional[ClassificationResolution]:
    """Resolve this day's break classification, or None if it does not apply.

    None means ``NO_VERSION`` — no ``BreakTreatmentProfile`` governs the date,
    so the control is not in force and the evaluation proceeds exactly as it
    did before P-1 existed.

    ONLY ``NO_VERSION`` MEANS THAT
    ------------------------------
    ``AMBIGUOUS`` and ``INVALID`` are resolution FAILURES, and the owner ruling
    on the opt-in gate requires them to fail closed: "if the profile itself
    cannot be resolved deterministically, fail closed rather than treating the
    day as having no profile." They are refusals, NOT
    ``CLASSIFICATION_UNRESOLVED`` — that reason says the breaks were examined
    and found indeterminate; here nothing was examined, because the rule that
    would govern the examination could not be read.
    """
    resolved = break_treatment_for_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date, ledger=ledger)
    if resolved is None:
        return None
    return (resolved if isinstance(resolved, ClassificationResolution)
            else resolved.classification)


def break_treatment_for_day(
    db: Session, *, organization_id: int, user_id: int,
    work_date: date | datetime, ledger,
):
    """The full P-2 boundary resolution.

    Returns one of three things:

        None                     no profile governs the date (``NO_VERSION``)
        ClassificationResolution a REFUSAL, or a deferral the caller records
        BreakTreatmentOutcome    a resolved contribution, ready to evaluate
    """
    on = _as_date(work_date)

    # ── 1. The governing rule, resolved historically (P2-D4) ────────────────
    profile = resolve_profile_as_of(
        db, organization_id=organization_id, as_of=on)

    if profile.outcome is ProfileOutcome.NO_VERSION:
        return None

    if profile.outcome is not ProfileOutcome.RESOLVED:
        return _refused(
            organization_id, user_id, on,
            _PROFILE_REFUSAL_REASON[profile.outcome],
            f"break-treatment profile for {on.isoformat()} resolved "
            f"{profile.outcome.value}"
            + (f": {profile.invalid_reason}" if profile.invalid_reason else ""))

    # ── 2. The day's breaks, with authoritative boundaries (P2-D1) ──────────
    windows = break_windows_for_day(
        db, organization_id=organization_id, user_id=user_id, work_date=on)
    if not windows:
        # A profile governs, but the day contains no breaks. Nothing to
        # classify and nothing to defer for. No snapshot is written: an empty
        # classification input is the absence of the control, not an explicit
        # empty determination about it.
        return None

    incomplete = [w[0] for w in windows if w[3] is None]
    if incomplete:
        # A break with no authoritative end has no duration. Refused rather
        # than guessed — inferring one is the §7.2 failure in miniature.
        return _refused(
            organization_id, user_id, on, REASON_BREAK_EVIDENCE_UNAVAILABLE,
            f"{sorted(incomplete)} have no authoritative end boundary")

    # ── 3. Every break needs an authoritative determination (P2-D5) ─────────
    classification = resolve_classification_for_day(
        db, organization_id=organization_id, user_id=user_id, work_date=on,
        ledger=ledger, expected_source_refs=[w[0] for w in windows])

    if classification.is_refused or classification.defers:
        # A refusal the boundary carries through, or a deferral D-1 records as
        # CLASSIFICATION_UNRESOLVED. Either way no arithmetic happens.
        return classification

    # ── 4. Treatment, then the cumulative cap (P-4, P2-D2) ──────────────────
    rest_seconds = 0
    meal_seconds = 0
    # Observed regardless of treatment -- see ``observed_*_seconds``.
    observed_rest = 0
    observed_meal = 0
    contributions: List[tuple] = []

    for source_ref, begin, end, seconds in windows:
        verdict = classification.classifications.get(source_ref)
        if verdict == ClassificationResult.BRK_R.value:
            observed_rest += seconds
            if profile.rest_qualifies:
                rest_seconds += seconds
                contributions.append((source_ref, verdict, begin, end, seconds))
        elif verdict == ClassificationResult.BRK_M.value:
            # §13.2's ``meal_qualifies``. Typically false, never assumed: a
            # meal contributes only where the Tenant's profile says so, and
            # P-4 fixes that it never reaches the REST pool or its cap.
            observed_meal += seconds
            if profile.meal_qualifies:
                meal_seconds += seconds
                contributions.append((source_ref, verdict, begin, end, seconds))

    # THE CAP IS DAILY AND CUMULATIVE (§19.1), applied ONCE to the summed rest
    # rather than per interval: 15 + 20 against a 30 cap admits 30, not 35 and
    # not 20 + 20. Computed in SECONDS so the single ROUND_HALF_UP site below
    # converts the whole pool once (P2-D2, GV-12).
    cap_seconds = (None if profile.rest_qualifying_cap_minutes is None
                   else int(profile.rest_qualifying_cap_minutes) * 60)
    capped_rest = (rest_seconds if cap_seconds is None
                   else min(rest_seconds, cap_seconds))

    return BreakTreatmentOutcome(
        classification=classification,
        profile_ref=profile.profile_ref,
        qualifying_break_seconds=capped_rest + meal_seconds,
        total_rest_seconds=rest_seconds,
        capped_rest_seconds=capped_rest,
        qualifying_meal_seconds=meal_seconds,
        observed_meal_seconds=observed_meal,
        observed_rest_seconds=observed_rest,
        cap_seconds=cap_seconds,
        cap_applied=cap_seconds is not None and rest_seconds > cap_seconds,
        contributions=tuple(sorted(contributions, key=lambda c: c[0])),
    )


__all__ = [
    "BreakTreatmentOutcome",
    "break_treatment_for_day",
    "classification_for_day",
    "break_source_refs_for_day",
    "break_windows_for_day",
    "REASON_BREAK_EVIDENCE_UNAVAILABLE",
]
