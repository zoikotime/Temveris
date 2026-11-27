"""
The minimal weekly fold.
========================

EVAL-WEEK first increment, under owner rulings W1 and W3. Authorised to make
exactly one golden vector executable: GV-12.

WHAT IT IS
----------
A pure sum of already-rounded daily contributions:

    weekly_total = SUM(day.contributes_to_accumulator_minutes)

Nothing else. No threshold, no event, no notification, no persistence, no
workweek resolution, no netting, grace or horizon.

THE ONE ROUNDING BOUNDARY (W3, R-16.5)
--------------------------------------
Rounding happens once, in EVAL-DAY. This module sums integers that are already
minutes and **never touches seconds**. That is the whole point of GV-12: five
days each a little short round to one minute apiece and total 5 — where rounding
once at week end would give 3. A second rounding site here would silently
reintroduce the defect the vector exists to catch.

So this module imports no rounding function, performs no division, and reads no
duration. It reads one field per day.

WHAT IT REFUSES (W1)
--------------------
A week is not final unless every day supplied is a successfully evaluated day.
A required day that is deferred or unresolved makes the week **unresolved**, not
partial — because a deferred day's zero contribution means "contributes nothing
yet", never "contributed nothing". Producing a weekly total from an incomplete
week would hand a threshold evaluation a number that looks authoritative and is
not.

THIS IS THE INNER FOLD, NOT THE PUBLIC ENTRY POINT
--------------------------------------------------
When this module was written, W4 was open and no authoritative workweek
identity existed, so the caller supplied the days. W4 is now closed — the
persisted ``workweek_key`` is the local workweek start date and is
authoritative — and owner ruling 3 requires the PUBLIC evaluator to derive its
own membership, because a caller-supplied list lets the most dangerous week (one
with a day missing) look exactly like a complete one.

That entry point is :func:`app.eval_week.evaluate_workweek`. This function
remains as the arithmetic over a set of days already established to be the
week, and as the executable home of GV-12, which supplies five days directly
and needs no calendar rule.

WHAT IS STILL DELIBERATELY ABSENT
---------------------------------
* **No workweek resolution here.** Membership is ``membership.py``'s job, from
  the persisted key. This module resolves nothing.
* **No persistence.** There is no WeeklyRecord, no migration, no table; ruling
  6 keeps the evaluator pure.
* **No threshold.** ``W >= T`` belongs to EVAL-EVENT by a closed ruling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from app.eval_day import EcoDayState

#: The EVAL-DAY states that may contribute to a final weekly total.
#:
#: ``NOT_A_WORKING_DAY`` is included by owner ruling 1: it is a MEMBER of the
#: week whose contribution is exactly 0. It was previously excluded because
#: GV-12 established nothing about non-working days and assuming participation
#: would have invented semantics — but the exclusion meant every real calendar
#: week returned ``PARTICIPATION_NOT_ESTABLISHED``, since every real week
#: contains one. Membership and participation-as-a-working-day are distinct;
#: only the second is denied to it.
#:
#: Held as the string VALUES, not the enum members. A persisted
#: ``EcoDayRecord.state`` is a ``String``, and ``value is EcoDayState.X`` is
#: False for a matching string — so an identity comparison silently
#: misclassified every persisted row.
PARTICIPATING_STATES = frozenset({
    EcoDayState.NOT_A_WORKING_DAY.value,
    EcoDayState.SHORTFALL.value,
    EcoDayState.NO_SHORTFALL.value,
})

#: Reasons a week cannot be finalised. Stable strings so a caller can branch
#: without parsing prose.
REASON_NO_DAYS = "NO_DAYS_SUPPLIED"
REASON_DEFERRED_DAY = "DEFERRED_DAY_PRESENT"
REASON_UNRESOLVED_DAY = "UNRESOLVED_DAY_PRESENT"
REASON_PARTICIPATION_NOT_ESTABLISHED = "PARTICIPATION_NOT_ESTABLISHED"


@dataclass(frozen=True)
class WeekFold:
    """The outcome of folding a supplied set of day evaluations.

    Deliberately a boolean plus a reason rather than a state enum: a weekly
    state vocabulary is a normative decision (W3/W6) that has not been made, and
    inventing one here would be harder to retract than to avoid.

    ``weekly_total_minutes`` is ``None`` unless the fold is complete — the same
    guarantee EVAL-DAY and Step 5 make, so a caller cannot read an incomplete
    week as a zero-shortfall week.
    """

    is_complete: bool
    weekly_total_minutes: Optional[int] = None
    day_count: int = 0
    reason: Optional[str] = None
    blocking_states: Tuple[str, ...] = ()
    #: The day-level deferral reasons behind a blocked week (owner ruling D-1,
    #: ruling 4). The weekly OUTCOME is deliberately identical for both deferral
    #: conditions — ``DEFERRED_DAY_PRESENT``, with no new weekly state — but a
    #: consumer must still be able to tell an unresolved EXIT from an unresolved
    #: CLASSIFICATION, because they are acted on in different places. Carried as
    #: provenance only: nothing in the fold branches on it.
    #:
    #: ``None`` appears for a departure/ledger deferral, which is what a NULL
    #: reason has always meant.
    deferral_reasons: Tuple[Optional[str], ...] = ()

    def __post_init__(self) -> None:
        if self.is_complete:
            if self.weekly_total_minutes is None:
                raise ValueError("a complete fold must carry a weekly total")
            if self.reason is not None:
                raise ValueError("a complete fold has no blocking reason")
        else:
            if self.weekly_total_minutes is not None:
                raise ValueError(
                    f"an incomplete fold must not carry a weekly total "
                    f"(got {self.weekly_total_minutes!r}); an unresolved week is "
                    f"not a zero-shortfall week"
                )
            if self.reason is None:
                raise ValueError("an incomplete fold must state its reason")


def fold_week(day_evaluations: Sequence) -> WeekFold:
    """Sum already-rounded daily contributions over the days supplied.

    The caller decides which days constitute the week; this function does not
    resolve a workweek (W4 unresolved). It reads exactly one field per day —
    ``contributes_to_accumulator_minutes`` — and performs no arithmetic on
    seconds, durations or intervals.

    Deterministic and side-effect free: integer addition is order-independent,
    nothing is persisted, and no clock is read.
    """
    days = list(day_evaluations)
    if not days:
        # An empty collection is ambiguous — it could mean "a week with no days"
        # or "the days could not be loaded". Returning 0 would make the second
        # indistinguishable from the first.
        return WeekFold(
            is_complete=False, day_count=0, reason=REASON_NO_DAYS,
        )

    blocking = []
    deferral_reasons = []
    for day in days:
        # Normalised to the string value: a persisted state is a str and an
        # in-memory one is a str enum, and only ``.value`` is common to both.
        state = (day.state.value if isinstance(day.state, EcoDayState)
                 else str(day.state))
        if state in PARTICIPATING_STATES:
            continue
        if state == EcoDayState.DEFERRED_PENDING_LEDGER.value:
            blocking.append((REASON_DEFERRED_DAY, state))
            # Provenance, not a branch: both deferral conditions produce the
            # same weekly outcome, and only the reason distinguishes them.
            deferral_reasons.append(
                getattr(day, "evaluation_deferral_reason", None))
        elif state == EcoDayState.REQUIREMENT_UNRESOLVED.value:
            blocking.append((REASON_UNRESOLVED_DAY, state))
        else:
            # EVALUABLE, or any state the weekly contract does not place.
            # Refused rather than assumed: an unplaced state carries no
            # contribution value that could truthfully be summed.
            blocking.append((REASON_PARTICIPATION_NOT_ESTABLISHED, state))

    if blocking:
        # A deferred day is reported ahead of the others: it is the condition
        # W1 names, and the one an operator can actually act on by re-sealing
        # the day's ledger.
        order = (REASON_DEFERRED_DAY, REASON_UNRESOLVED_DAY,
                 REASON_PARTICIPATION_NOT_ESTABLISHED)
        reason = min((r for r, _ in blocking), key=order.index)
        return WeekFold(
            is_complete=False,
            day_count=len(days),
            reason=reason,
            blocking_states=tuple(sorted({s for _, s in blocking})),
            deferral_reasons=tuple(deferral_reasons),
        )

    # Already-rounded minutes in, integer sum out. No second rounding (R-16.5).
    # Every participating state carries a value; a None here would be a
    # contributing day with nothing to contribute, which is not summable.
    if any(d.contributes_to_accumulator_minutes is None for d in days):
        return WeekFold(
            is_complete=False, day_count=len(days),
            reason=REASON_PARTICIPATION_NOT_ESTABLISHED,
            blocking_states=("MISSING_CONTRIBUTION",),
        )
    total = sum(int(d.contributes_to_accumulator_minutes) for d in days)
    return WeekFold(
        is_complete=True, weekly_total_minutes=total, day_count=len(days),
    )


__all__ = [
    "WeekFold",
    "fold_week",
    "PARTICIPATING_STATES",
    "REASON_NO_DAYS",
    "REASON_DEFERRED_DAY",
    "REASON_UNRESOLVED_DAY",
    "REASON_PARTICIPATION_NOT_ESTABLISHED",
]
