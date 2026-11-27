"""
The weekly evaluation result vocabulary.
========================================

EVAL-WEEK, under owner rulings 1-7 of the membership gate.

A weekly state enum exists here where the first fold deliberately refused to
invent one: ruling 5 now *requires* the evaluator to distinguish at least
``COMPLETE``, ``DEFERRED_DAY_PRESENT``, ``REQUIREMENT_UNRESOLVED`` and
``DAY_RECORD_MISSING``, and forbids collapsing them into one generic error.

The four additional members are structural conditions that arise from deriving
membership from the database rather than from a caller's list. Each fails
closed, and each says something different about *why* the week cannot be
trusted — which is the point of not collapsing them.

WHAT A WEEKLY STATE IS NOT
--------------------------
It is not an ECO event, a crossing, or a comparison against a threshold. The
evaluator stops at ``W``; ``W >= T`` belongs to EVAL-EVENT by a closed ruling.
Nothing here names a tier, a sequence or a notification.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Tuple


class WeeklyState(str, enum.Enum):
    """Why the weekly evaluator returned what it returned."""

    COMPLETE = "COMPLETE"
    """Every day of the canonical workweek has an authoritative evaluation, and
    ``weekly_attributable_shortfall_minutes`` is set."""

    DEFERRED_DAY_PRESENT = "DEFERRED_DAY_PRESENT"
    """At least one day is ``DEFERRED_PENDING_LEDGER``.

    W1: a deferred day contributes 0, and that zero means "contributes nothing
    YET" — never "contributed nothing". The week is unresolved, not partial.
    The most actionable blocker, because re-sealing the day's ledger clears
    it."""

    REQUIREMENT_UNRESOLVED = "REQUIREMENT_UNRESOLVED"
    """At least one day is ``REQUIREMENT_UNRESOLVED``.

    §10.2: with no resolved requirement a shortfall must not be computed,
    assumed zero, or assumed to be a default — so neither may a week
    containing one."""

    DAY_RECORD_MISSING = "DAY_RECORD_MISSING"
    """The canonical week contains an expected day whose authoritative
    ``EcoDayRecord`` is absent (ruling 2).

    Not zero, not ignored, not ``NOT_A_WORKING_DAY``, not ``NO_SHORTFALL``. An
    unsealed (``OPEN``) ledger day and a ``LEDGER_INTEGRITY_FAULT`` day both
    land here, because EVAL-DAY writes no record for either — which is exactly
    the case a caller-supplied day list could have hidden."""

    DAY_RECORD_AMBIGUOUS = "DAY_RECORD_AMBIGUOUS"
    """More than one ``EcoDayRecord`` matches one expected day.

    The snapshot identity includes ``config_version`` and ``policy_version``, so
    re-evaluating a day under superseded configuration legitimately produces a
    second row for the same ledger version. Which of them governs the week is
    not ruled, so the evaluator refuses rather than picking — the same choice
    the threshold reader makes for two candidate rows."""

    LEDGER_MEMBERSHIP_INCONSISTENT = "LEDGER_MEMBERSHIP_INCONSISTENT"
    """The persisted membership contradicts the workweek key it carries.

    Either a current ledger row's ``work_date`` lies outside the seven calendar
    dates the key denotes, or two current rows claim the same date. Both mean
    the stored grouping cannot be trusted, and neither can be repaired here:
    §24.15's principle is to record and refuse, never to correct evidence."""

    NO_LEDGER_DAYS = "NO_LEDGER_DAYS"
    """No current ledger day carries this workweek key for this Worker.

    Returned instead of a zero total because the two readings — "a week in
    which nothing was ever recorded" and "a week whose days did not load" — are
    indistinguishable from an empty result, and one of them is a defect."""

    MALFORMED_WORKWEEK_KEY = "MALFORMED_WORKWEEK_KEY"
    """The supplied key is not the canonical ``YYYY-MM-DD`` workweek start date.

    Refused rather than repaired or guessed: the key IS the week's identity
    (W4-a), so a key nobody can parse identifies no week."""

    UNEXPECTED_DAY_STATE = "UNEXPECTED_DAY_STATE"
    """A record carries a state the weekly contract does not place, or a
    participating record carries no contribution value.

    ``EVALUABLE`` lands here. Ruling 5 lists it among the states a complete
    week may contain, but EVAL-DAY's S1 contract forbids persisting it at all
    and a test pins that no stored record carries it — so the case cannot arise
    from EVAL-DAY, and if it ever did the record would carry NULL minutes
    (``UNCOMPUTED_STATES``) which must never be summed. Failing closed is the
    only reading that satisfies both rulings; the contradiction is reported to
    the owner rather than silently resolved."""


#: Which weekly state is reported when a week trips more than one condition.
#:
#: Structural problems first: a week whose membership or identity cannot be
#: trusted has no meaningful day-level diagnosis, so reporting a deferred day
#: inside it would point an operator at the wrong thing. Then the missing
#: record, then the two day-level conditions — deferred ahead of unresolved,
#: because re-sealing a ledger is something an operator can actually do.
BLOCKING_PRECEDENCE: Tuple[WeeklyState, ...] = (
    WeeklyState.MALFORMED_WORKWEEK_KEY,
    WeeklyState.NO_LEDGER_DAYS,
    WeeklyState.LEDGER_MEMBERSHIP_INCONSISTENT,
    WeeklyState.DAY_RECORD_AMBIGUOUS,
    WeeklyState.DAY_RECORD_MISSING,
    WeeklyState.UNEXPECTED_DAY_STATE,
    WeeklyState.DEFERRED_DAY_PRESENT,
    WeeklyState.REQUIREMENT_UNRESOLVED,
)


@dataclass(frozen=True)
class WeekEvaluation:
    """One weekly evaluation for one Worker's canonical workweek.

    Identity is ``(organization_id, user_id, workweek_key)`` (ruling 6). Nothing
    is persisted: this is a pure evaluator, there is no weekly table and no
    migration.

    ``weekly_attributable_shortfall_minutes`` is ``None`` unless the state is
    ``COMPLETE`` — the same guarantee EVAL-DAY, Step 5 and the threshold reader
    make, so no caller can read an unresolved week as a zero-shortfall week.
    """

    state: WeeklyState
    organization_id: int
    user_id: int
    workweek_key: str

    weekly_attributable_shortfall_minutes: Optional[int] = None

    #: The seven calendar dates the workweek key denotes, in order. Empty only
    #: when the key could not be parsed.
    canonical_dates: Tuple[date, ...] = ()

    #: How many current ledger days carry this key.
    day_count: int = 0

    #: The distinct ``EcoDayState`` values observed, sorted. Ruling 1 requires
    #: ``NOT_A_WORKING_DAY`` to stay visible in the weekly state set.
    observed_states: Tuple[str, ...] = ()

    #: The dates responsible for a blocking state, sorted.
    blocking_dates: Tuple[date, ...] = ()

    # ── Supporting provenance (ruling 6) ────────────────────────────────────
    day_record_ids: Tuple[int, ...] = ()
    ledger_versions: Tuple[int, ...] = ()
    #: Preserved exactly as persisted, ``None`` included (ruling 7). No
    #: policy-version subsystem is introduced and no value is invented.
    policy_versions: Tuple[Optional[str], ...] = ()
    config_versions: Tuple[Optional[str], ...] = ()

    #: Prose for an operator. Never parsed; ``state`` is the classification.
    detail: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        return self.state is WeeklyState.COMPLETE

    def __post_init__(self) -> None:
        if self.state is WeeklyState.COMPLETE:
            if self.weekly_attributable_shortfall_minutes is None:
                raise ValueError(
                    "a COMPLETE week must carry its weekly total"
                )
            if self.blocking_dates:
                raise ValueError(
                    f"a COMPLETE week has no blocking dates "
                    f"(got {self.blocking_dates!r})"
                )
        elif self.weekly_attributable_shortfall_minutes is not None:
            raise ValueError(
                f"{self.state.value} must not carry a weekly total "
                f"(got {self.weekly_attributable_shortfall_minutes!r}); an "
                f"unresolved week is not a zero-shortfall week"
            )


__all__ = [
    "WeeklyState",
    "BLOCKING_PRECEDENCE",
    "WeekEvaluation",
]
