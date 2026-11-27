"""
The governed vocabularies a PolicyVersion may carry.
=====================================================

Closed on purpose. An unrecognised value is REFUSED, never normalised onto a
neighbour: a policy that silently became ``NONE`` because a typo did not match
would compute a different weekly total from the one the Tenant configured, and
nothing downstream could tell.
"""

from __future__ import annotations

import enum
from typing import Any, FrozenSet


class NettingMode(str, enum.Enum):
    """§15's netting modes, verbatim.

    The BEHAVIOUR of each is deliberately not implemented here — MODEL-NETTING
    is a separate increment. This artifact records WHICH mode was in force on a
    given date, which is the part §17 needs frozen.
    """

    NONE = "NONE"
    """Each day is evaluated independently. §15's platform default, and what
    §45's OD-02 recommends at onboarding."""

    WEEKLY_BANK = "WEEKLY_BANK"
    """Surplus accrued earlier in the SAME workweek offsets later shortfall, up
    to a configured cap."""

    PERIOD_BANK = "PERIOD_BANK"
    """The same idea over a longer configured period."""


class NettingDecisionProvenance(str, enum.Enum):
    """HOW the netting mode came to be selected (owner rulings D5, OD-02: A).

    §15 puts the obligation on the onboarding surface: "Tenant onboarding MUST
    surface this choice explicitly as a required decision, never as a
    silently-accepted default." §44.3 then makes "Netting decision made
    explicitly — Not accepted as a default" a rollout gate, and the gate needs
    something in the data to read.

    ONE MEMBER, DELIBERATELY
    ------------------------
    OD-02 was ruled A: the onboarding choice must begin unselected and require
    an explicit selection. Under that ruling a NEW capture can only ever be an
    explicit Tenant choice, so a DEFAULT_DERIVED member would be unreachable —
    and an unreachable enum member is an invitation to start writing it.

    ABSENT IS NOT A MEMBER EITHER. Historical rows carry NULL, because
    ``policy_version`` is append-only with database triggers and cannot be
    back-filled. Owner ruling F: D grandfathers them, and NULL is how the gate
    recognises one.
    """

    EXPLICIT_TENANT_CHOICE = "EXPLICIT_TENANT_CHOICE"
    """An authorized Tenant actor selected the mode at onboarding."""


#: Every provenance value a NEW capture may declare. NULL is not in this set:
#: it is the grandfathered historical state, not something a capture may claim.
NETTING_DECISION_PROVENANCES: FrozenSet[str] = frozenset(
    m.value for m in NettingDecisionProvenance)


#: Every netting mode a policy may declare. Exactly three — §15 names no
#: others, and a fourth would be a specification change, not a code change.
NETTING_MODES: FrozenSet[str] = frozenset(m.value for m in NettingMode)


def is_valid_netting_mode(value: Any) -> bool:
    """True only for one of the three governed values. Exact match.

    No case folding, no trimming, no alias table. ``weekly_bank`` is not
    ``WEEKLY_BANK``: accepting it would be the first step towards accepting
    something nobody authorized.
    """
    if isinstance(value, NettingMode):
        return True
    return isinstance(value, str) and value in NETTING_MODES


def parse_netting_mode(value: Any) -> NettingMode:
    """The enum member, or ``ValueError``. Never a default."""
    if isinstance(value, NettingMode):
        return value
    if isinstance(value, str) and value in NETTING_MODES:
        return NettingMode(value)
    raise ValueError(
        f"{value!r} is not a governed netting mode; expected one of "
        f"{sorted(NETTING_MODES)}"
    )


__all__ = [
    "NettingMode",
    "NETTING_MODES",
    "is_valid_netting_mode",
    "parse_netting_mode",
]


# ═════════════════════════════════════════════════════════════════════════════
# §21.5 — the retroactive notification horizon
# ═════════════════════════════════════════════════════════════════════════════

#: §21.5, verbatim: "A Tenant-configurable retroactive horizon (default 90
#: days) bounds how far back a retroactive threshold event may be raised."
#:
#: Named here rather than written as a literal at the capture site so the
#: specification's own number has exactly one home. It is the value a capture
#: RECORDS when the Tenant states no other; it is NEVER substituted for a
#: missing value at resolution time. A row that carries no horizon carries
#: none, and Stage 4 decides what that means — inventing 90 for it here would
#: assert a policy nobody configured, which is the failure D-1.1 and D-2.1
#: were ruled to prevent for the scheduled end.
DEFAULT_RETROACTIVE_HORIZON_DAYS: int = 90
