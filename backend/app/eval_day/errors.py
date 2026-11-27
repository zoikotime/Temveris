"""
EVAL-DAY refusals.
==================

Every one of these means **no DayRecord is written**. That is the point: R4
forbids converting a ledger failure into a fabricated ECO result, and R6/R8
require failing closed rather than inventing semantics.

A refusal is not an error in the evidence. It is the evaluator declining to
produce a number it has no authority to produce.
"""

from __future__ import annotations


class EvalDayError(Exception):
    """Base for EVAL-DAY failures."""


class EvaluationRefused(EvalDayError):
    """The day cannot be evaluated, and no result may be invented.

    Carries a machine-readable ``reason`` so a caller can distinguish the
    refusals without parsing prose, and so an operator is told which authority
    is missing rather than simply that "it failed".
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class DayRecordConflict(EvalDayError):
    """The same snapshot re-evaluated to a different result.

    The snapshot identity (R2) fixes every authoritative input, so a differing
    result means the evaluator itself changed. That is a real event worth
    surfacing — silently overwriting would destroy the earlier derived result
    and make the change invisible. R1: historical DayRecords are not mutated in
    place.
    """


class DayRecordImmutable(EvalDayError):
    """A persisted DayRecord would be altered or removed.

    Recomputation produces a new derived version under a new snapshot identity;
    it never rewrites an existing one (R1).
    """


class LeaveInputSnapshotRequired(EvalDayError):
    """A record was offered for persistence without its leave input identity.

    Owner Ruling 2: the leave input snapshot key is part of the snapshot
    identity, and a day with no contributing leave references an explicit EMPTY
    snapshot. So there is no such thing as "no leave input" — only a snapshot
    saying none contributed. Refused rather than defaulted: silently recording
    an empty input for a day that HAD leave would fabricate provenance, which
    is worse than failing.
    """


class DayRecordAuthorityContention(EvalDayError):
    """Repeated writers kept claiming the authoritative generation first.

    The generation unique constraint serializes authority, and a loser
    re-reads and retries. Exhausting the bounded retries means sustained
    contention rather than a defect; the caller re-runs. Fails closed — no
    record is written and no row is left ambiguously authoritative.
    """


#: Refusal reasons. Stable strings so tests and operators can match on them
#: without depending on message wording.
REASON_LEDGER_INTEGRITY_FAULT = "LEDGER_INTEGRITY_FAULT"
REASON_LEDGER_MISSING_CLOCK_OUT = "LEDGER_MISSING_CLOCK_OUT"
REASON_LEDGER_NOT_SEALED = "LEDGER_NOT_SEALED"
REASON_NON_WRK_STATE_PRESENT = "NON_WRK_STATE_PRESENT"
REASON_EXCLUSION_REQUIRED = "EXCLUSION_REQUIRED"
REASON_ORGANIZATION_REQUIRED = "ORGANIZATION_REQUIRED"
#: MODEL-NETTING (D-6.8). The governing policy banks, but records no
#: per-day or per-period surplus cap. §15.1 requires both, and an absent
#: cap is neither zero nor unlimited, so the day is refused rather than
#: evaluated against a bound nobody configured.
REASON_NETTING_CAP_UNAVAILABLE = "NETTING_CAP_UNAVAILABLE"
#: MODEL-NETTING. PERIOD_BANK nets across a reference period no artifact
#: records, and §15 suspends weekly threshold evaluation under it. Refused
#: rather than folded as a workweek â see app/eval_day/netting.py.
REASON_NETTING_PERIOD_UNSUPPORTED = "NETTING_PERIOD_UNSUPPORTED"


__all__ = [
    "EvalDayError",
    "EvaluationRefused",
    "DayRecordConflict",
    "DayRecordImmutable",
    "LeaveInputSnapshotRequired",
    "DayRecordAuthorityContention",
    "REASON_LEDGER_INTEGRITY_FAULT",
    "REASON_LEDGER_MISSING_CLOCK_OUT",
    "REASON_LEDGER_NOT_SEALED",
    "REASON_NON_WRK_STATE_PRESENT",
    "REASON_EXCLUSION_REQUIRED",
    "REASON_ORGANIZATION_REQUIRED",
    "REASON_NETTING_CAP_UNAVAILABLE",
    "REASON_NETTING_PERIOD_UNSUPPORTED",
]
