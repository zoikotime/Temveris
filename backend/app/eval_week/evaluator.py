"""
The weekly evaluator.
=====================

    evaluate_workweek(db, organization_id=..., user_id=..., workweek_key=...)

EVAL-WEEK, under owner rulings 1-7 of the membership gate.

WHAT IT PRODUCES
----------------
``W`` — the weekly attributable shortfall in minutes — and stops there. The
comparison ``W >= T`` belongs to EVAL-EVENT by a closed ruling, so no threshold
is read, no crossing is decided, no event is emitted and no notification is
sent. Nothing here names a tier or a sequence.

WHY IT DERIVES ITS OWN DAY SET (ruling 3)
-----------------------------------------
The earlier fold took the days from its caller. That made the most dangerous
week — one with a day missing — indistinguishable from a complete one, because
a caller could omit the inconvenient day and receive a confident total. So this
evaluator takes an identity, not a list, and derives membership from the
persisted ledger. A caller cannot hide a day it does not get to choose.

MEMBERSHIP VS PARTICIPATION (ruling 1)
--------------------------------------
``NOT_A_WORKING_DAY`` is a MEMBER of the week and contributes exactly 0. It is
not a working day, creates no shortfall, and does not make the week unresolved.
The two ideas are distinct, and conflating them is what previously made no real
calendar week foldable at all.

WHAT IT NEVER DOES
------------------
It does not manufacture ``EcoDayRecord`` rows, evaluate raw ledger intervals,
call ``app._worked``, or recompute any daily value. It reads one field per day —
``contributes_to_accumulator_minutes`` — already rounded once by EVAL-DAY
(R-16.5). No second rounding, no seconds, no mutable accumulator, and integer
addition so no order dependence.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.eval_day import EcoDayState

from .membership import (
    canonical_dates,
    current_ledger_days,
    day_records_for,
)
from .outcomes import BLOCKING_PRECEDENCE, WeeklyState, WeekEvaluation

#: Day states that are members of the week AND carry a usable contribution.
#:
#: ``NOT_A_WORKING_DAY`` is included by owner ruling 1. Its contribution is a
#: truthful 0 (R5 zeroes every measurement), so including it is arithmetically
#: inert — but excluding it made every real week unresolved, because every real
#: week contains one.
CONTRIBUTING_STATES = frozenset({
    EcoDayState.NOT_A_WORKING_DAY.value,
    EcoDayState.SHORTFALL.value,
    EcoDayState.NO_SHORTFALL.value,
})

#: Day states that are members of the week and block a weekly total.
BLOCKING_STATES: Dict[str, WeeklyState] = {
    EcoDayState.DEFERRED_PENDING_LEDGER.value: WeeklyState.DEFERRED_DAY_PRESENT,
    EcoDayState.REQUIREMENT_UNRESOLVED.value: WeeklyState.REQUIREMENT_UNRESOLVED,
}


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _state_value(state) -> str:
    """A day state as its canonical string.

    Persisted ``EcoDayRecord.state`` is a ``String`` column, so a comparison by
    identity against an enum member is False even when the values match — the
    defect this normalisation removes. ``EcoDayState`` is a ``str`` enum, so
    ``.value`` is the one representation both sides share.
    """
    return state.value if isinstance(state, EcoDayState) else str(state)


def evaluate_workweek(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    workweek_key: str,
) -> WeekEvaluation:
    """Evaluate one Worker's canonical workweek. Pure: reads, never writes.

    Returns, never raises, for every ordinary outcome — a caller checks
    ``is_complete`` rather than catching. Nothing is persisted and no clock is
    read, so the same inputs always produce the same answer (INV-8).
    """
    def _refuse(
        state: WeeklyState,
        *,
        detail: str,
        dates: Sequence[date] = (),
        blocking: Sequence[date] = (),
        days: int = 0,
        observed: Sequence[str] = (),
        provenance: Optional[Tuple] = None,
    ) -> WeekEvaluation:
        ids, versions, policies, configs = provenance or ((), (), (), ())
        return WeekEvaluation(
            state=state,
            organization_id=organization_id,
            user_id=user_id,
            workweek_key=workweek_key,
            canonical_dates=tuple(dates),
            day_count=days,
            observed_states=tuple(sorted(set(observed))),
            blocking_dates=tuple(sorted(set(blocking))),
            day_record_ids=tuple(ids),
            ledger_versions=tuple(versions),
            policy_versions=tuple(policies),
            config_versions=tuple(configs),
            detail=detail,
        )

    # ── 1. The seven calendar dates the key denotes ──────────────────────────
    week = canonical_dates(workweek_key)
    if week is None:
        return _refuse(
            WeeklyState.MALFORMED_WORKWEEK_KEY,
            detail=(
                f"workweek_key {workweek_key!r} is not a canonical YYYY-MM-DD "
                f"workweek start date"
            ),
        )

    # ── 2. Membership comes from the ledger, never from a caller ─────────────
    ledger_days = current_ledger_days(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key,
    )
    if not ledger_days:
        return _refuse(
            WeeklyState.NO_LEDGER_DAYS, dates=week,
            detail=(
                f"no current ledger day carries workweek_key {workweek_key!r} "
                f"for user {user_id} in organization {organization_id}"
            ),
        )

    # ── 3. The stored grouping must agree with the key it carries ────────────
    in_week = set(week)
    seen_dates: Dict[date, int] = {}
    inconsistent: List[date] = []
    for day in ledger_days:
        on = _as_date(day.work_date)
        if on not in in_week:
            inconsistent.append(on)
        seen_dates[on] = seen_dates.get(on, 0) + 1
    duplicated = sorted(d for d, n in seen_dates.items() if n > 1)

    if inconsistent or duplicated:
        parts = []
        if inconsistent:
            parts.append(
                f"{len(inconsistent)} ledger day(s) fall outside the week "
                f"{week[0].isoformat()}..{week[-1].isoformat()}"
            )
        if duplicated:
            parts.append(
                f"{len(duplicated)} date(s) carry more than one current "
                f"ledger row"
            )
        return _refuse(
            WeeklyState.LEDGER_MEMBERSHIP_INCONSISTENT, dates=week,
            days=len(ledger_days),
            blocking=sorted(set(inconsistent) | set(duplicated)),
            detail="; ".join(parts),
        )

    # ── 4. One authoritative day record per ledger day ──────────────────────
    records = day_records_for(
        db, organization_id=organization_id, user_id=user_id,
        ledger_days=ledger_days,
    )

    missing: List[date] = []
    ambiguous: List[date] = []
    resolved: List[Tuple[date, object]] = []
    for day in ledger_days:
        key = (_as_date(day.work_date), int(day.ledger_version))
        found = records.get(key, [])
        if not found:
            missing.append(key[0])
        elif len(found) > 1:
            ambiguous.append(key[0])
        else:
            resolved.append((key[0], found[0]))

    observed = [_state_value(record.state) for _, record in resolved]
    provenance = (
        tuple(int(r.id) for _, r in resolved),
        tuple(int(r.ledger_version) for _, r in resolved),
        # Ruling 7: preserved exactly as persisted, NULL included. No
        # policy-version is resolved, defaulted or invented.
        tuple(r.policy_version for _, r in resolved),
        tuple(r.config_version for _, r in resolved),
    )

    # ── 5. Day-level conditions ─────────────────────────────────────────────
    blocked: Dict[WeeklyState, List[date]] = {}
    total = 0
    for on, record in resolved:
        state = _state_value(record.state)
        if state in CONTRIBUTING_STATES:
            contribution = record.contributes_to_accumulator_minutes
            if contribution is None:
                # A contributing state with no contribution is not summable,
                # and a missing value must never be read as a zero.
                blocked.setdefault(
                    WeeklyState.UNEXPECTED_DAY_STATE, []).append(on)
                continue
            total += int(contribution)
        elif state in BLOCKING_STATES:
            blocked.setdefault(BLOCKING_STATES[state], []).append(on)
        else:
            # EVALUABLE, or any state the weekly contract does not place. See
            # WeeklyState.UNEXPECTED_DAY_STATE for why this fails closed.
            blocked.setdefault(
                WeeklyState.UNEXPECTED_DAY_STATE, []).append(on)

    if missing:
        blocked[WeeklyState.DAY_RECORD_MISSING] = missing
    if ambiguous:
        blocked[WeeklyState.DAY_RECORD_AMBIGUOUS] = ambiguous

    if blocked:
        state = min(blocked, key=BLOCKING_PRECEDENCE.index)
        dates_at_fault = blocked[state]
        return _refuse(
            state, dates=week, days=len(ledger_days),
            observed=observed, blocking=dates_at_fault,
            provenance=provenance,
            detail=(
                f"{state.value} on "
                f"{', '.join(d.isoformat() for d in sorted(set(dates_at_fault)))}"
            ),
        )

    # ── 6. W — already-rounded minutes in, integer sum out (W3, R-16.5) ─────
    return WeekEvaluation(
        state=WeeklyState.COMPLETE,
        organization_id=organization_id,
        user_id=user_id,
        workweek_key=workweek_key,
        weekly_attributable_shortfall_minutes=total,
        canonical_dates=week,
        day_count=len(ledger_days),
        observed_states=tuple(sorted(set(observed))),
        day_record_ids=provenance[0],
        ledger_versions=provenance[1],
        policy_versions=provenance[2],
        config_versions=provenance[3],
    )


__all__ = [
    "evaluate_workweek",
    "CONTRIBUTING_STATES",
    "BLOCKING_STATES",
]
