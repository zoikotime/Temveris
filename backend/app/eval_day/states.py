"""
ECO day-evaluation state vocabulary — distinct from LedgerState.
================================================================

EVAL-DAY, under owner ruling R4.

TWO VOCABULARIES, DELIBERATELY SEPARATE
---------------------------------------
``LedgerState`` (Step 1) describes the SEALED EVIDENCE:

    OPEN · SEALED · MISSING_CLOCK_OUT · NOT_A_WORKING_DAY · LEDGER_INTEGRITY_FAULT

``EcoDayState`` (here) describes the DERIVED EVALUATION. R4 fixed it at six
values and forbids adding any of them to ``LedgerState``. The ledger vocabulary
is untouched by this module, and a test asserts that.

``POLICY_UNRESOLVED`` is the SEVENTH, added by owner ruling after the
PolicyVersion consumption audit established that none of R4's six could carry
it: ``REQUIREMENT_UNRESOLVED`` is scoped by its own contract to the requirement,
``DEFERRED_PENDING_LEDGER`` to a deferral condition, and the specification's
``evaluateDay()`` resolves a policy at step 1 without ever guarding it. The
owner declined to stretch an existing state, so the vocabulary grew by exactly
one rather than quietly changing what an existing value means.

WHY POLICY_UNRESOLVED IS STILL ITS OWN STATE, AND CLASSIFICATION IS NOT
-----------------------------------------------------------------------
Owner ruling D-1 later admitted a SECOND deferral condition — an unresolved
break classification — onto ``DEFERRED_PENDING_LEDGER`` rather than minting an
eighth state. That is not a reversal of the reasoning above, and the asymmetry
is deliberate rather than accidental:

* A deferred day is one that CANNOT YET be evaluated and is expected to become
  evaluable. Both a missing clock-out and an unestablished classification are
  that: the same S2 shape (every measurement NULL, contribution exactly 0,
  ``shortfall_computed`` False) and the same §28.1 label, differing only in
  which input is outstanding. A reason distinguishes them losslessly.
* ``POLICY_UNRESOLVED`` is NOT that shape. It is an UNCOMPUTED state: it
  carries no contribution at all, not even a truthful zero, because a Tenant
  with no applicable policy has no evaluated week to contribute to. Folding it
  onto the deferred carrier would have had to invent a contribution of 0 that
  §10.2 forbids, and the renderer records separately that "Deferred" would be
  the wrong §28.1 label for a CONFIGURATION failure.

So the rule is shape, not convenience: a condition joins the deferred carrier
only when it already satisfies ``DEFERRED_STATES``' invariants exactly. Neither
``POLICY_UNRESOLVED`` nor ``REQUIREMENT_UNRESOLVED`` does, and neither moves.

``NOT_A_WORKING_DAY`` appears in both. That is not a merge: the ledger records
that no work was expected, and the evaluation records that it therefore produced
a zeroed result (R5). The two are different statements about the same day.

WHAT IS NOT HERE
----------------
``MISSING_CLOCK_OUT`` and ``LEDGER_INTEGRITY_FAULT`` are ledger states and are
NOT evaluation states. A day in either condition receives no DayRecord at all —
the evaluator refuses (R10). Converting a ledger fault into an evaluation result
would be exactly the fabrication §24.15 forbids.
"""

from __future__ import annotations

import enum
from typing import FrozenSet


class EcoDayState(str, enum.Enum):
    """The approved ECO day-evaluation states — R4's six, plus
    ``POLICY_UNRESOLVED`` by later owner ruling."""

    NOT_A_WORKING_DAY = "NOT_A_WORKING_DAY"
    """No work was expected. Everything zeroes (R5), and the record is written
    so the week fold stays complete and explainable (§24.2)."""

    REQUIREMENT_UNRESOLVED = "REQUIREMENT_UNRESOLVED"
    """No requirement could be resolved for the date. §10.2: shortfall MUST NOT
    be computed, assumed zero, or assumed to be the default.

    Scoped to the REQUIREMENT, deliberately. A missing policy is
    ``POLICY_UNRESOLVED``; the two failures are reported separately because a
    Tenant fixes them in different places."""

    POLICY_UNRESOLVED = "POLICY_UNRESOLVED"
    """No applicable historical PolicyVersion could be resolved for the work
    date.

    A policy-resolution failure ONLY. §15 forbids assuming ``NONE`` netting and
    the owner forbade a neutral default, so evaluation stops here rather than
    proceeding under a policy nobody configured. Like every other
    non-computable state it carries no minutes — see ``UNCOMPUTED_STATES``."""

    DEFERRED_PENDING_LEDGER = "DEFERRED_PENDING_LEDGER"
    """The day cannot yet be evaluated because a required authoritative
    condition — a LEDGER condition or a CLASSIFICATION condition — is
    unavailable, but is expected to become resolvable (S2).

    ``LedgerState.MISSING_CLOCK_OUT`` maps here (S3): the ledger keeps its own
    condition, and this is the ECO-domain counterpart. A persisted result, not
    a refusal — but one that fabricates nothing.

    TWO DEFERRAL CONDITIONS, ONE CARRIER (owner ruling D-1)
    -------------------------------------------------------
    The state is the CARRIER; ``EcoDayRecord.evaluation_deferral_reason`` says
    WHICH condition deferred the day, and it is part of the authoritative
    meaning rather than metadata:

        reason NULL                      the ledger/departure deferral this
                                         state has always meant — missing
                                         clock-out (S3) or the §24.7
                                         unverified exit
        reason CLASSIFICATION_UNRESOLVED a sealed day whose break
                                         classification could not be
                                         established post-seal (P-1, P-2)

    Both carry the identical S2 shape — every measurement NULL, contribution
    exactly 0, ``shortfall_computed`` False — which is why one carrier serves
    both without either condition borrowing the other's meaning. See
    ``EvaluationDeferralReason``."""

    EVALUABLE = "EVALUABLE"
    """Pre-computation readiness (S1): the authoritative inputs are present and
    the day is eligible, but the shortfall classification has not been produced.

    Represented internally during evaluation and **never persisted** as the
    final state of a successful evaluation — that must be SHORTFALL or
    NO_SHORTFALL. A test pins that no stored record carries it."""

    SHORTFALL = "SHORTFALL"
    """Evaluated, and the Worker fell short of the resolved requirement."""

    NO_SHORTFALL = "NO_SHORTFALL"
    """Evaluated, and the requirement was met."""


class EvaluationDeferralReason(str, enum.Enum):
    """WHICH condition deferred a ``DEFERRED_PENDING_LEDGER`` day (D-1).

    A CLOSED vocabulary, and deliberately not part of ``EcoDayState``: the
    state says the day is deferred, this says why. Owner ruling D-1 is explicit
    that the reason is "part of the authoritative evaluation meaning", not
    cosmetic metadata, so it joins the record's identity and its content hash.

    ABSENCE IS A VALUE, NOT A GAP
    -----------------------------
    NULL means the ledger/departure deferral this state has always carried —
    the reading every record written before D-1 has, which is why the column is
    nullable and why the migration performs no backfill. Writing a reason onto
    those rows would assert a determination nobody made.
    """

    CLASSIFICATION_UNRESOLVED = "CLASSIFICATION_UNRESOLVED"
    """A sealed day whose break classification could not be established.

    P-1 forbids inferring the classification from the worker-selected break
    label, and an unclassifiable break must never be silently read as BRK_M,
    BRK_R or WRK. So the day defers, contributing a truthful 0, until an
    authoritative assertion exists."""


#: Every reason a persisted record may carry. Exactly one today; a second would
#: be an owner ruling, not a code change.
DEFERRAL_REASONS: FrozenSet[EvaluationDeferralReason] = frozenset(
    EvaluationDeferralReason)

#: The states a deferral reason may accompany. Only the deferred carrier: a
#: reason on any other state is a contradiction the evaluation refuses to
#: construct, which is D-1's fail-closed rule stated as data.
REASON_BEARING_STATES: FrozenSet[EcoDayState] = frozenset({
    EcoDayState.DEFERRED_PENDING_LEDGER,
})


def is_valid_deferral_reason(value) -> bool:
    """True only for an approved reason. Exact match, no case folding."""
    if isinstance(value, EvaluationDeferralReason):
        return True
    return isinstance(value, str) and value in {
        r.value for r in EvaluationDeferralReason}


#: States that carry a complete set of computed minutes.
COMPUTED_STATES: FrozenSet[EcoDayState] = frozenset({
    EcoDayState.NOT_A_WORKING_DAY,
    EcoDayState.SHORTFALL,
    EcoDayState.NO_SHORTFALL,
})

#: States that carry NO minute values at all. §10.2: a state that could not be
#: evaluated must not carry a number a caller might read as one.
UNCOMPUTED_STATES: FrozenSet[EcoDayState] = frozenset({
    EcoDayState.REQUIREMENT_UNRESOLVED,
    EcoDayState.POLICY_UNRESOLVED,
    EcoDayState.EVALUABLE,
})

#: The one state that carries a single truthful zero and nothing else.
#:
#: The S2 invariant is that "minute result fields that CANNOT TRUTHFULLY be
#: computed must remain NULL". For a deferred day the accumulator contribution
#: *can* be computed truthfully — a day awaiting a ledger condition contributes
#: nothing — while every measurement (required, qualifying, gross, exclusion,
#: attributable, variance) genuinely cannot. So contribution is 0 and the rest
#: are NULL. GV-16 and GV-17 independently state "contributes 0", which is the
#: same reading.
DEFERRED_STATES: FrozenSet[EcoDayState] = frozenset({
    EcoDayState.DEFERRED_PENDING_LEDGER,
})

#: Declared by R4 but emitted by no code path. ``EVALUABLE`` is internal-only
#: by S1; nothing else remains unemitted after this increment.
UNEMITTED_IN_THIS_INCREMENT: FrozenSet[EcoDayState] = frozenset({
    EcoDayState.EVALUABLE,
})

#: States a persisted DayRecord may carry. ``EVALUABLE`` is excluded by S1.
PERSISTABLE_STATES: FrozenSet[EcoDayState] = frozenset(
    set(EcoDayState) - {EcoDayState.EVALUABLE}
)


def is_valid_state(value) -> bool:
    """True only for an approved value. Exact match."""
    if isinstance(value, EcoDayState):
        return True
    return isinstance(value, str) and value in {s.value for s in EcoDayState}


__all__ = [
    "EcoDayState",
    "EvaluationDeferralReason",
    "DEFERRAL_REASONS",
    "REASON_BEARING_STATES",
    "is_valid_deferral_reason",
    "COMPUTED_STATES",
    "UNCOMPUTED_STATES",
    "DEFERRED_STATES",
    "UNEMITTED_IN_THIS_INCREMENT",
    "PERSISTABLE_STATES",
    "is_valid_state",
]
