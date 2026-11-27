"""
Offboarding case state machine.

Single authority for "may this case move from A → B?". Every mutating service
calls `assert_transition(...)` BEFORE writing the new state, so an invalid hop
is rejected server-side regardless of what the frontend believes. The frontend
can grey out buttons for UX, but it can never *cause* an illegal transition —
all gating is here.

Design notes
  • The graph is intentionally explicit (no "any → any") so a new state can't
    silently become reachable. Adding a transition is a deliberate edit here.
  • BLOCKED is reachable from any active (non-terminal) state and can return to
    the state it came from once the blocker clears — we store the pre-block
    state on the case row (`blocked_from_state`).
  • REVERSED is a controlled un-wind reachable from any post-approval state
    except COMPLETED-after-retention-lock (reversal of a fully completed case
    is a separate, audited rehire flow, not a state hop).
"""
from __future__ import annotations

from typing import Dict, FrozenSet

from .constants import CaseState, TERMINAL_STATES


# Allowed forward/again transitions. Key = from-state, value = set of legal
# to-states. BLOCKED, REVERSED, CANCELLED handling is layered on top.
_TRANSITIONS: Dict[CaseState, FrozenSet[CaseState]] = {
    CaseState.DRAFT: frozenset({
        CaseState.PENDING_APPROVAL,
        CaseState.CANCELLED,
    }),
    CaseState.PENDING_APPROVAL: frozenset({
        CaseState.APPROVED,
        CaseState.DRAFT,          # sent back for edits
        CaseState.CANCELLED,
    }),
    CaseState.APPROVED: frozenset({
        CaseState.SCHEDULED,
        CaseState.IN_PROGRESS,    # immediate revocation mode
        CaseState.CANCELLED,
    }),
    CaseState.SCHEDULED: frozenset({
        CaseState.IN_PROGRESS,
        CaseState.CANCELLED,
    }),
    CaseState.IN_PROGRESS: frozenset({
        CaseState.ACCESS_CLOSURE_PENDING,
    }),
    CaseState.ACCESS_CLOSURE_PENDING: frozenset({
        CaseState.FINAL_REVIEW_PENDING,        # all components revoked
        CaseState.ACCESS_REVOCATION_FAILED,    # one or more failed
    }),
    CaseState.ACCESS_REVOCATION_FAILED: frozenset({
        CaseState.ACCESS_CLOSURE_PENDING,      # retry
        CaseState.FINAL_REVIEW_PENDING,        # operator force-resolved + audited
    }),
    CaseState.FINAL_REVIEW_PENDING: frozenset({
        CaseState.PAYROLL_CLOSURE_PENDING,
    }),
    CaseState.PAYROLL_CLOSURE_PENDING: frozenset({
        CaseState.BILLING_CLOSURE_PENDING,
    }),
    CaseState.BILLING_CLOSURE_PENDING: frozenset({
        CaseState.LEGAL_HOLD_REVIEW_PENDING,
    }),
    CaseState.LEGAL_HOLD_REVIEW_PENDING: frozenset({
        CaseState.FORMER_ACCESS_PENDING,
    }),
    CaseState.FORMER_ACCESS_PENDING: frozenset({
        CaseState.COMPLETED,
    }),
    # Terminal / control states have no ordinary forward transitions.
    CaseState.COMPLETED: frozenset({CaseState.REVERSED}),  # rehire/unwind, audited
    CaseState.REVERSED: frozenset(),
    CaseState.CANCELLED: frozenset(),
    CaseState.BLOCKED: frozenset(),   # handled specially via unblock()
}

# States from which an operator may CANCEL (before any irreversible closure).
_CANCELLABLE = frozenset({
    CaseState.DRAFT,
    CaseState.PENDING_APPROVAL,
    CaseState.APPROVED,
    CaseState.SCHEDULED,
})

# States from which an approved case may be REVERSED (controlled unwind). Once
# retention is locked + certificate issued (COMPLETED) reversal is a rehire
# flow, represented as COMPLETED → REVERSED above.
_REVERSIBLE = frozenset({
    CaseState.APPROVED,
    CaseState.SCHEDULED,
    CaseState.IN_PROGRESS,
    CaseState.ACCESS_CLOSURE_PENDING,
    CaseState.ACCESS_REVOCATION_FAILED,
    CaseState.FINAL_REVIEW_PENDING,
    CaseState.PAYROLL_CLOSURE_PENDING,
    CaseState.BILLING_CLOSURE_PENDING,
    CaseState.LEGAL_HOLD_REVIEW_PENDING,
    CaseState.FORMER_ACCESS_PENDING,
    CaseState.BLOCKED,
    CaseState.COMPLETED,
})


class InvalidTransition(Exception):
    """Raised when a requested state hop is not permitted. Carries machine and
    human-readable detail so the router can return a clean 409."""

    def __init__(self, frm: CaseState, to: CaseState, reason: str = ""):
        self.frm = frm
        self.to = to
        self.reason = reason or f"{frm.value} → {to.value} is not a permitted transition"
        super().__init__(self.reason)


def _coerce(state) -> CaseState:
    return state if isinstance(state, CaseState) else CaseState(state)


def can_transition(frm, to) -> bool:
    """Pure predicate — does the graph permit frm → to?"""
    frm, to = _coerce(frm), _coerce(to)
    if frm == to:
        return False
    if to == CaseState.BLOCKED:
        return frm not in TERMINAL_STATES and frm != CaseState.BLOCKED
    if to == CaseState.CANCELLED:
        return frm in _CANCELLABLE
    if to == CaseState.REVERSED:
        return frm in _REVERSIBLE
    return to in _TRANSITIONS.get(frm, frozenset())


def assert_transition(frm, to) -> None:
    """Raise InvalidTransition unless frm → to is permitted. Call this BEFORE
    persisting the new state."""
    frm, to = _coerce(frm), _coerce(to)
    if not can_transition(frm, to):
        raise InvalidTransition(frm, to)


def can_unblock(blocked_from) -> bool:
    """A BLOCKED case may return only to the (non-terminal) state it left."""
    blocked_from = _coerce(blocked_from)
    return blocked_from not in TERMINAL_STATES and blocked_from != CaseState.BLOCKED


def allowed_next(frm) -> FrozenSet[CaseState]:
    """Every state reachable from `frm` (for surfacing valid actions to the UI;
    NOT used for enforcement — enforcement is assert_transition)."""
    frm = _coerce(frm)
    nxt = set(_TRANSITIONS.get(frm, frozenset()))
    if frm not in TERMINAL_STATES and frm != CaseState.BLOCKED:
        nxt.add(CaseState.BLOCKED)
    if frm in _CANCELLABLE:
        nxt.add(CaseState.CANCELLED)
    if frm in _REVERSIBLE:
        nxt.add(CaseState.REVERSED)
    return frozenset(nxt)
