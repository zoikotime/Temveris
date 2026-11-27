"""
MODEL-NETTING — §15's bank, as a fold over day records.
========================================================

Owner rulings D-6.3, D-6.4, D-6.8, D-6.9, D-6.10, D-6.11, D-6.12.

WHAT THIS MODULE IS
-------------------
The answer to one question, asked once per day evaluation:

    "How many minutes of banked surplus are available to close THIS day's
     requirement gap?"

It answers by FOLDING canonical day records. It reads; it never writes a
balance, and there is nothing here that accumulates between calls.

§15.1 / INV-3, verbatim:

    "The bank balance is itself a fold over day records, never a stored
     counter."

So there is no `bank_balance`, no `bank_counter`, no period row and no running
total anywhere in this package or in the schema. Every number below is
recomputed from immutable `EcoDayRecord` rows each time it is needed. That is
slower than a counter and it is the point: a counter can drift from the records
it summarises, and then no one can say which is true.

SURPLUS (D-6.3)
---------------
    surplus_minutes = MAX(0, qualifying_minutes - required_minutes)

Ledger-derived only. §15.1: "Banked surplus MUST derive from the same ledger as
shortfall. Manually entered surplus MUST NOT exist."

It is computed from the two measurements on the day record and from nothing
else. In particular NOT from gross shortfall (which is already net of bank),
NOT from attributable shortfall (already net of exclusions and grace), and NOT
from schedule variance (§10: recorded, never drives the threshold). Deriving it
from any of those would let leave, grace or an early departure manufacture
credit, which is exactly what D-6.3 rules out.

A day that recorded no measurements — unresolved requirement, unresolved
policy, deferred classification — contributes NO surplus. Not zero surplus
because nothing was measured: `qualifying_minutes` is NULL on those rows, and
NULL is not zero.

THE ORDERING (D-6.4, §18)
-------------------------
Bank is consumed at step 10-11, BEFORE gross exists::

     9  q     := roundMinutes(Q, rounding_profile)
    10  bank  := bankCredit(worker, work_date, netting_mode)
    11  gross := MAX(0, req.minutes - q - bank)
    12  exclusions apply to gross
    13  attributable := remaining; IF <= grace: 0

So exclusions and grace are applied to a gap the bank has ALREADY reduced.
Leave never becomes surplus, and grace never returns credit to the bank —
the credit was spent at step 11 and is recorded on the day that spent it.

WHY A DAY TAKES ONLY WHAT IT NEEDS
-----------------------------------
`bank_credit_minutes = MIN(available, MAX(0, required - qualifying))`.

A day with no gap takes nothing, however large the pool. Taking more would
consume a capped resource to no effect and would make the recorded credit
unexplainable against the day's own arithmetic.

EARLIER DAYS ONLY, NEVER LATER ONES
------------------------------------
The pool for a day is folded from days STRICTLY BEFORE it in the canonical
workweek. §15's WEEKLY_BANK is explicit — "surplus accrued EARLIER in the same
workweek offsets LATER shortfall" — and the one-directional read is also what
makes D-6.11 hold: evaluating Wednesday can never rewrite Tuesday's recorded
credit, because Wednesday is not in Tuesday's fold.

WHEN A CORRECTION LANDS (D-6.11)
---------------------------------
Nothing is mutated, because there is nothing to mutate. Correcting Monday
writes a new authoritative Monday record; the next time Tuesday is evaluated
its fold reads the corrected Monday and produces a different credit, and that
Tuesday evaluation supersedes the previous one through the existing
`evaluation_generation` / `supersedes` chain. A day already recorded against a
superseded predecessor is STALE, not wrong — and re-evaluating it is the
documented way to refresh it.

FAIL CLOSED ON A MISSING CAP (D-6.8)
-------------------------------------
§15.1: "Surplus MUST be capped per day and per period, per Tenant
configuration." A bank mode whose policy records no cap is a policy gap, and
this module REFUSES rather than treating the absent cap as unlimited or as
zero. A NULL cap is not zero — Stage 2 kept those distinguishable precisely so
this refusal could be made.

PERIOD_BANK IS NOT IMPLEMENTED, AND IS NOT GUESSED
---------------------------------------------------
§15 says PERIOD_BANK nets "across a longer configured reference period (month,
quarter, annualised)" and that under it "weekly threshold evaluation is
SUSPENDED; a period-level review replaces it ... the weekly threshold model
does not apply."

Three things are therefore missing, and none may be invented here:

* WHICH period — month, quarter or annualised — is not recorded anywhere.
  `PolicyVersion` carries `surplus_cap_per_period_minutes` but no period
  definition, and no other artifact supplies one.
* WHERE the period starts. Without an anchor the fold has no membership rule,
  and a fold with no membership is not deterministic (D-6.12).
* The period-level REVIEW that §15 says replaces weekly evaluation does not
  exist as a control.

§43 lists PERIOD_BANK under phase P4. So a `PERIOD_BANK` policy REFUSES here.
Silently folding it as a workweek would suspend nothing, review nothing, and
report a weekly threshold the specification says does not apply.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.policy_version import NettingMode

from .models import EcoDayRecord

#: §15's modes that consume a bank. `NONE` evaluates each day independently and
#: never creates or consumes credit, so it is absent by construction rather
#: than by an `!= NONE` test somewhere.
BANKING_MODES = frozenset({NettingMode.WEEKLY_BANK.value,
                           NettingMode.PERIOD_BANK.value})

#: The one banking mode this increment implements. See the module docstring for
#: why PERIOD_BANK is refused rather than approximated.
IMPLEMENTED_BANKING_MODES = frozenset({NettingMode.WEEKLY_BANK.value})


class BankOutcome(str, enum.Enum):
    """What the fold established. Exactly one per call."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The governing policy does not bank (`NONE`). No credit exists, and none
    is a legitimate answer rather than an absence."""

    RESOLVED = "RESOLVED"
    """A credit was folded — possibly zero, which is a measured zero."""

    REFUSED = "REFUSED"
    """The policy banks but the fold cannot be performed. No credit is
    invented and no evaluation may proceed on a guess."""


@dataclass(frozen=True)
class BankResolution:
    """The answer, in a form a caller cannot misread as a number."""

    outcome: BankOutcome
    netting_mode: Optional[str] = None
    credit_minutes: Optional[int] = None

    #: Only on RESOLVED. The fold's working, so an explanation surface can say
    #: WHY the credit was what it was without recomputing it.
    eligible_surplus_minutes: Optional[int] = None
    already_consumed_minutes: Optional[int] = None
    available_minutes: Optional[int] = None
    gap_minutes: Optional[int] = None
    cap_per_day_minutes: Optional[int] = None
    cap_per_period_minutes: Optional[int] = None

    #: Content-addressed digest of everything the fold READ, or None where no
    #: fold ran. Joins the EcoDayRecord snapshot identity so a correction
    #: upstream produces a NEW record rather than colliding with the old one —
    #: see `compute_bank_input_key`.
    input_key: Optional[str] = None

    #: Only on REFUSED.
    reason: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is BankOutcome.REFUSED:
            if not self.reason:
                raise ValueError("a REFUSED bank resolution must carry a reason")
            if self.credit_minutes is not None:
                raise ValueError(
                    "REFUSED must not carry a credit: a fold that could not be "
                    "performed produced no number")
            return
        if self.reason is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason")
        if self.outcome is BankOutcome.NOT_APPLICABLE:
            if self.credit_minutes not in (None, 0):
                raise ValueError(
                    "a non-banking policy cannot produce a credit")
            return
        if self.credit_minutes is None:
            raise ValueError("RESOLVED must state a credit")
        if self.credit_minutes < 0:
            raise ValueError(
                f"credit_minutes must be >= 0, got {self.credit_minutes}")

    @property
    def is_refused(self) -> bool:
        return self.outcome is BankOutcome.REFUSED

    @property
    def minutes(self) -> int:
        """The credit to apply at §18 step 11. Zero unless RESOLVED."""
        return int(self.credit_minutes or 0)


def compute_bank_input_key(
    *,
    netting_mode: str,
    cap_per_day_minutes: int,
    cap_per_period_minutes: int,
    earlier_records: Sequence,
) -> str:
    """A digest of every input the fold read. Content-addressed.

    WHY THIS EXISTS
    ---------------
    `EcoDayRecord`'s snapshot identity names only facts about ITS OWN day.
    A banked day's result also depends on OTHER days, so without this the
    architecture has no way to express "same day, different fold": a
    corrected Monday would make Tuesday re-evaluate to a different answer
    under an unchanged identity, which `record_evaluation` correctly
    refuses as a RESULT_CONFLICT.

    So the fold's inputs get a key, exactly as leave inputs and the
    classification already do. A change upstream changes the key, the key
    changes the identity, and the re-evaluation SUPERSEDES instead of
    colliding (D-6.11).

    The earlier days are identified by their own `content_hash` — already
    a digest of their whole result — so any change to a folded day, in any
    field, moves this key. Sorted by date, and the digest is canonical
    JSON, so the value cannot depend on row order or platform encoding.
    """
    payload = {
        "netting_mode": netting_mode,
        "cap_per_day_minutes": int(cap_per_day_minutes),
        "cap_per_period_minutes": int(cap_per_period_minutes),
        "folded": sorted(
            [_as_date(r.work_date).isoformat(),
             str(getattr(r, "content_hash", "") or "")]
            for r in earlier_records
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def eligible_surplus_minutes(record, *, cap_per_day: int) -> int:
    """One day's bankable surplus, after the per-day cap. PURE.

    D-6.3::

        surplus = MAX(0, qualifying_minutes - required_minutes)

    then bounded by §15.1's per-day cap, which exists "to prevent one long day
    erasing a week".

    A record that measured nothing contributes nothing: `qualifying_minutes` or
    `required_minutes` being NULL means no measurement was taken, and NULL is
    not zero. An unresolved requirement, an unresolved policy and a deferred
    classification all reach here as NULLs, and all correctly bank nothing.
    """
    qualifying = getattr(record, "qualifying_minutes", None)
    required = getattr(record, "required_minutes", None)
    if qualifying is None or required is None:
        return 0
    surplus = max(int(qualifying) - int(required), 0)
    return min(surplus, int(cap_per_day))


def _consumed_minutes(record) -> int:
    """What a day already took from the bank. NULL reads as nothing taken."""
    return int(getattr(record, "bank_credit_minutes", None) or 0)


def fold_available_minutes(
    earlier_records: Sequence,
    *,
    cap_per_day: int,
    cap_per_period: int,
) -> tuple:
    """The pool available to the next day. PURE, and the whole of the fold.

    Returns ``(available, eligible, consumed)`` in minutes.

    Deterministic for a fixed set of records (D-6.12): the two sums are
    order-independent integer arithmetic, so the result does not depend on how
    the database returned the rows or on how many times this is called.

    The per-period cap bounds what the WEEK may supply in total, not what
    remains after consumption — otherwise a week could supply its cap over and
    over as earlier days were spent. It is therefore applied to the eligible
    pool before consumption is subtracted.
    """
    eligible = sum(eligible_surplus_minutes(r, cap_per_day=cap_per_day)
                   for r in earlier_records)
    consumed = sum(_consumed_minutes(r) for r in earlier_records)
    available = min(eligible, int(cap_per_period)) - consumed
    return max(available, 0), eligible, consumed


def _earlier_authoritative_records(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    workweek_key: str,
    before: date,
) -> list:
    """Authoritative day records earlier than ``before`` in this workweek.

    CANONICAL MEMBERSHIP, NOT ISO WEEKS
    ------------------------------------
    The dates come from `eval_week.membership.canonical_dates`, which reads the
    workweek key frozen on the ledger at seal. A Sunday-start tenant's week is
    its own seven dates; ISO week numbering would silently be the wrong week
    for them, which is the drift W4-3's start-date format exists to remove.

    ``superseded_by IS NULL`` — the same authoritative-row filter EVAL-WEEK
    applies. A corrected day's OLD record is retained for history and must
    never be folded: the fold reads what is true now, and the supersession
    chain is what makes "now" well-defined.
    """
    # Deferred import to keep the module graph acyclic — the same idiom the
    # ledger, producer and classifier packages use. EVAL-WEEK depends on
    # EVAL-DAY, so importing it at module level here would close the loop.
    # Membership is still EVAL-WEEK's answer; this only defers asking for it.
    from app.eval_week.membership import canonical_dates

    dates = canonical_dates(workweek_key)
    if dates is None:
        return []
    earlier = [d for d in dates if d < before]
    if not earlier:
        return []
    return list(db.execute(
        select(EcoDayRecord).where(
            EcoDayRecord.organization_id == organization_id,
            EcoDayRecord.user_id == user_id,
            EcoDayRecord.work_date.in_(sorted(earlier)),
            EcoDayRecord.superseded_by.is_(None),
        ).order_by(EcoDayRecord.work_date, EcoDayRecord.id)
    ).scalars().all())


def resolve_bank_credit(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date | datetime,
    workweek_key: str,
    netting_mode: Optional[str],
    cap_per_day_minutes: Optional[int],
    cap_per_period_minutes: Optional[int],
    evaluation,
) -> BankResolution:
    """§18 step 10: the bank credit for one day.

    Called at the recording boundary, where the session lives, so the evaluator
    stays pure — the same seam `leave_overlap_seconds` and
    `qualifying_break_seconds` already use.

    ``evaluation`` is THIS day's first-pass `DayEvaluation`, evaluated with no
    credit. Its ``required_minutes`` and ``qualifying_minutes`` are already
    resolved and already rounded once (§16 / GV-12), and the gap between them
    is what bounds the credit — a day takes only what closes its own gap.

    The whole object is taken rather than the two numbers so the recording
    boundary never has to name a measurement. That boundary is orchestration
    only; every quantity here is read from an evaluation the evaluator
    produced, and nothing recomputes one.
    """
    from .errors import (
        REASON_NETTING_CAP_UNAVAILABLE,
        REASON_NETTING_PERIOD_UNSUPPORTED,
    )

    on = _as_date(work_date)

    # ── NONE, and anything a policy did not state ───────────────────────────
    # D-6.9: banking applies to the bank modes only. A policy with no netting
    # mode is not a banking policy; it is a policy that never said, and the
    # PolicyVersion resolver has already refused a mode outside the vocabulary.
    if netting_mode not in BANKING_MODES:
        return BankResolution(outcome=BankOutcome.NOT_APPLICABLE,
                              netting_mode=netting_mode, credit_minutes=0)

    # ── PERIOD_BANK: refused, never approximated ────────────────────────────
    if netting_mode not in IMPLEMENTED_BANKING_MODES:
        return BankResolution(
            outcome=BankOutcome.REFUSED,
            netting_mode=netting_mode,
            reason=REASON_NETTING_PERIOD_UNSUPPORTED,
            detail=(
                f"{netting_mode} nets across a configured reference period "
                f"(month, quarter, annualised) that no artifact records, and "
                f"§15 suspends weekly threshold evaluation under it in favour "
                f"of a period-level review that does not exist. Folding it as "
                f"a workweek would report a weekly threshold the specification "
                f"says does not apply"),
        )

    # ── The caps are mandatory under a bank mode (D-6.8, §15.1) ─────────────
    # NULL is not zero and not unlimited: it is a policy that never stated the
    # bound §15.1 says MUST exist. Refused so a Tenant sees the gap rather than
    # silently banking without a limit.
    missing = [name for name, value in (
        ("surplus_cap_per_day_minutes", cap_per_day_minutes),
        ("surplus_cap_per_period_minutes", cap_per_period_minutes),
    ) if value is None]
    if missing:
        return BankResolution(
            outcome=BankOutcome.REFUSED,
            netting_mode=netting_mode,
            reason=REASON_NETTING_CAP_UNAVAILABLE,
            detail=(
                f"policy governing {on.isoformat()} declares "
                f"netting_mode={netting_mode} but records no {', '.join(missing)}; "
                f"§15.1 requires surplus to be capped per day and per period, "
                f"and an absent cap is neither zero nor unlimited"),
        )

    earlier = _earlier_authoritative_records(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key, before=on)

    available, eligible, consumed = fold_available_minutes(
        earlier,
        cap_per_day=int(cap_per_day_minutes),
        cap_per_period=int(cap_per_period_minutes),
    )

    # A day takes only what closes its own gap. `gap` is the PRE-bank shortfall
    # — §18 step 11 subtracts the credit from it, so asking for more than the
    # gap would spend a capped resource to no effect.
    #
    # Read off the first-pass evaluation; a day that measured nothing has no
    # gap to close and takes nothing.
    required = getattr(evaluation, "required_minutes", None)
    qualifying = getattr(evaluation, "qualifying_minutes", None)
    gap = (0 if required is None or qualifying is None
           else max(int(required) - int(qualifying), 0))
    credit = min(available, gap)

    return BankResolution(
        outcome=BankOutcome.RESOLVED,
        netting_mode=netting_mode,
        credit_minutes=credit,
        input_key=compute_bank_input_key(
            netting_mode=netting_mode,
            cap_per_day_minutes=int(cap_per_day_minutes),
            cap_per_period_minutes=int(cap_per_period_minutes),
            earlier_records=earlier,
        ),
        eligible_surplus_minutes=eligible,
        already_consumed_minutes=consumed,
        available_minutes=available,
        gap_minutes=gap,
        cap_per_day_minutes=int(cap_per_day_minutes),
        cap_per_period_minutes=int(cap_per_period_minutes),
    )


__all__ = [
    "BANKING_MODES",
    "IMPLEMENTED_BANKING_MODES",
    "BankOutcome",
    "BankResolution",
    "compute_bank_input_key",
    "eligible_surplus_minutes",
    "fold_available_minutes",
    "resolve_bank_credit",
]
