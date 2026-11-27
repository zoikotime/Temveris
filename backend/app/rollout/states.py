"""
Rollout lifecycle vocabulary and transition rules — pure, no I/O, no ORM.
=========================================================================

STEP 8 of the owner-authorised Phase 2 foundation.

THE LIFECYCLE
-------------
    OFF ──▶ SHADOW ──▶ ENABLED
     ▲        │           │
     └────────┴───────────┘        (rollback, always permitted)

``OFF → ENABLED`` is deliberately NOT a transition. Reaching ENABLED requires
passing through SHADOW, which is what makes the observation period unavoidable
rather than advisory: there is no state assignment that skips it.

Rollback is permitted from anywhere, unconditionally and without a waiting
period. A control that is hard to turn off is not a safety control.

FAIL-SAFE IN ONE LINE
---------------------
Anything that is not exactly one of the three known states resolves to OFF.
Missing, empty, misspelled, wrong case, wrong type, corrupted — all OFF. There
is no input to :func:`coerce_state` that yields ENABLED except the exact string
``"ENABLED"``.

WHY THESE NAMES AND NOT THE ENTITLEMENT ONES
--------------------------------------------
``app/commercial/enforcement.py`` already has an off/shadow/**enforce** ladder
for entitlement 403s. This is a different control over a different subject, and
the owner specified OFF/SHADOW/**ENABLED** for rollout. The vocabularies are
kept separate on purpose: one word meaning two things in two subsystems is how
an operator turns on the wrong thing.
"""

from __future__ import annotations

import enum
from types import MappingProxyType
from typing import Any, FrozenSet, Mapping


class RolloutState(str, enum.Enum):
    """Durable rollout state for one control in one organization."""

    OFF = "OFF"
    """Not running. The default, and the answer to every uncertainty."""

    SHADOW = "SHADOW"
    """Running for observation only. Nothing the control governs takes effect
    for a Worker; the point is to see what it WOULD do before it does it."""

    ENABLED = "ENABLED"
    """Live for this organization. Reached only from SHADOW."""


#: The single source of truth for what may follow what. A transition not listed
#: here does not exist — there is no code path that assigns a state without
#: consulting this table.
#:
#: Wrapped in a MappingProxyType, and the values are frozensets, so the whole
#: table is read-only at runtime. A lifecycle that could be widened by assigning
#: to it at import time — by a plugin, a test helper, or a well-meant patch —
#: would be a lifecycle in name only, and OFF → ENABLED could be added by one
#: line executed anywhere in the process.
ALLOWED_TRANSITIONS: Mapping[RolloutState, FrozenSet[RolloutState]] = MappingProxyType({
    # Observation is mandatory: ENABLED is not reachable from here.
    RolloutState.OFF: frozenset({RolloutState.SHADOW}),
    # Forward to live, or straight back to off.
    RolloutState.SHADOW: frozenset({RolloutState.ENABLED, RolloutState.OFF}),
    # Rollback, either part-way or all the way. Never blocked, never delayed.
    RolloutState.ENABLED: frozenset({RolloutState.SHADOW, RolloutState.OFF}),
})

#: Transitions that reduce exposure. These bypass the observation window — a
#: rollback must never be made to wait.
ROLLBACK_TRANSITIONS: FrozenSet[tuple] = frozenset({
    (RolloutState.ENABLED, RolloutState.SHADOW),
    (RolloutState.ENABLED, RolloutState.OFF),
    (RolloutState.SHADOW, RolloutState.OFF),
})

#: The state a control is in before anybody has said otherwise, and the state
#: any unreadable value resolves to.
DEFAULT_STATE: RolloutState = RolloutState.OFF

#: Minimum days in SHADOW before ENABLED may be requested. Two workweeks read as
#: 14 calendar days.
#:
#: Deliberately a plain day count and NOT derived from an organization's
#: configured workweek: computing "two workweeks" would require reading the
#: schedule, and a rollout control that consults business configuration to
#: decide whether it may advance has started making product decisions. It is
#: also per-control configurable, so a longer observation can be required
#: without changing code.
DEFAULT_MIN_OBSERVATION_DAYS: int = 14

#: Control identifier for the Early Clock-Out v2 rollout. Declaring the name
#: creates no row and enables nothing: with no row, the control reads OFF for
#: every organization, which is the intended state at the end of Step 8.
CONTROL_EARLY_CLOCK_OUT_V2: str = "early_clock_out_v2"


def coerce_state(raw: Any) -> RolloutState:
    """Resolve a persisted value to a state, failing safe to OFF.

    Used on every read. A row whose ``state`` column holds anything unexpected —
    because a bulk update wrote it, a migration mangled it, or the value predates
    a rename — must not be interpreted optimistically. The safe direction is
    unambiguous here: reading OFF when the truth was ENABLED delays a rollout,
    while reading ENABLED when the truth was anything else turns a control on
    that nobody authorised.

    Exact match only. ``"enabled"``, ``"Enabled"`` and ``" ENABLED"`` are all
    OFF: a case-insensitive or trimming comparison would mean the vocabulary is
    not closed, and §32 warns that a value surface cannot be corrected later
    without a breaking change.
    """
    if isinstance(raw, RolloutState):
        return raw
    if not isinstance(raw, str):
        return DEFAULT_STATE
    for state in RolloutState:
        if raw == state.value:
            return state
    return DEFAULT_STATE


def is_valid_state(raw: Any) -> bool:
    """True only for one of the three exact state values."""
    if isinstance(raw, RolloutState):
        return True
    return isinstance(raw, str) and raw in {s.value for s in RolloutState}


def can_transition(current: RolloutState, target: RolloutState) -> bool:
    """Is ``current → target`` a transition that exists?

    A same-state request is not a transition; callers treat it as a no-op rather
    than asking this.
    """
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_rollback(current: RolloutState, target: RolloutState) -> bool:
    """Does this transition reduce exposure?"""
    return (current, target) in ROLLBACK_TRANSITIONS


def requires_observation_window(
    current: RolloutState, target: RolloutState
) -> bool:
    """Only the one step that increases live exposure waits.

    SHADOW → ENABLED. Everything else — entering SHADOW, and every rollback —
    proceeds immediately.
    """
    return current is RolloutState.SHADOW and target is RolloutState.ENABLED


__all__ = [
    "RolloutState",
    "ALLOWED_TRANSITIONS",
    "ROLLBACK_TRANSITIONS",
    "DEFAULT_STATE",
    "DEFAULT_MIN_OBSERVATION_DAYS",
    "CONTROL_EARLY_CLOCK_OUT_V2",
    "coerce_state",
    "is_valid_state",
    "can_transition",
    "is_rollback",
    "requires_observation_window",
]
