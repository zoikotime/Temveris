"""State machines (§8, §9, §10, §11) — pure transition tables.

Each machine is a set of allowed (from, to) tuples. ``transition`` raises
``IllegalTransitionError`` if a caller tries to move between two states
that are not explicitly allowed. This is invariant I-2 + I-3 enforcement
material: we don't trust the application to keep state internally
consistent, we check on every mutation.

These functions are pure — they do not touch the DB. Callers that want a
transition recorded should:

    new_state = state_machines.signup.transition(old, "abuse_check_pending")
    signup.state = new_state
    audit.emit_event(...state_from=old, state_to=new_state...)
    db.flush()

so transition + event + persistence happen in one transaction.
"""

from __future__ import annotations

from typing import FrozenSet, Tuple, Set


class IllegalTransitionError(ValueError):
    """Raised when a caller attempts a transition not in the spec."""

    def __init__(self, machine: str, from_state: str, to_state: str):
        self.machine = machine
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Illegal {machine} transition: {from_state} -> {to_state}"
        )


class _Machine:
    def __init__(self, name: str, transitions: Set[Tuple[str, str]],
                 terminal: Set[str] = frozenset(),
                 recoverable_failure_targets: dict | None = None):
        self.name = name
        self.transitions: FrozenSet[Tuple[str, str]] = frozenset(transitions)
        self.terminal = frozenset(terminal)
        # Map of failure_state → safe state to roll back to.
        self.recoverable: dict = recoverable_failure_targets or {}

    def can(self, from_state: str, to_state: str) -> bool:
        return (from_state, to_state) in self.transitions

    def transition(self, from_state: str, to_state: str) -> str:
        if from_state == to_state:
            return to_state
        if not self.can(from_state, to_state):
            raise IllegalTransitionError(self.name, from_state, to_state)
        return to_state

    def all_states(self) -> Set[str]:
        return {s for pair in self.transitions for s in pair}


# ──────────────────────────────────────────────────────────────────────────
# §8 — Signup state machine
# ──────────────────────────────────────────────────────────────────────────
SIGNUP_TRANSITIONS: Set[Tuple[str, str]] = {
    ("signup_started", "signup_submitted"),
    ("signup_submitted", "validation_failed"),
    ("signup_submitted", "abuse_check_pending"),
    ("abuse_check_pending", "abuse_blocked"),
    ("abuse_check_pending", "manual_review_required"),
    ("abuse_check_pending", "identity_created"),
    ("identity_created", "organization_created"),
    ("organization_created", "workspace_pending"),
    ("workspace_pending", "workspace_ready"),
    ("workspace_ready", "trial_created"),
    ("workspace_ready", "paid_checkout_pending"),
    ("paid_checkout_pending", "paid_checkout_failed"),
    ("paid_checkout_pending", "subscription_active"),
    ("paid_checkout_failed", "paid_checkout_pending"),  # retry
    ("trial_created", "signup_complete"),
    ("subscription_active", "signup_complete"),
    # Recovery: from any non-terminal state, manual_review_required can be cleared.
    ("manual_review_required", "identity_created"),
    ("manual_review_required", "abuse_blocked"),
    # Hard-fail any state to signup_failed (we whitelist a few common paths)
    ("validation_failed", "signup_failed"),
    ("abuse_blocked", "signup_failed"),
    ("paid_checkout_failed", "signup_failed"),
    ("workspace_pending", "signup_failed"),
}

signup = _Machine(
    "signup",
    SIGNUP_TRANSITIONS,
    terminal={"signup_complete", "signup_failed"},
    recoverable_failure_targets={
        "validation_failed": "signup_started",
        "paid_checkout_failed": "workspace_ready",
    },
)


# ──────────────────────────────────────────────────────────────────────────
# §9 — Tenant provisioning state machine
# ──────────────────────────────────────────────────────────────────────────
PROVISIONING_TRANSITIONS: Set[Tuple[str, str]] = {
    ("not_started", "queued"),
    ("queued", "identity_bound"),
    ("identity_bound", "organization_created"),
    ("organization_created", "workspace_created"),
    ("workspace_created", "owner_assigned"),
    ("owner_assigned", "defaults_applied"),
    ("defaults_applied", "audit_initialized"),
    ("audit_initialized", "entitlements_applied"),
    ("entitlements_applied", "ready"),

    # Retry path
    ("failed_retryable", "queued"),
    ("failed_retryable", "rolled_back"),

    # Any step → failure
    ("queued", "failed_retryable"),
    ("identity_bound", "failed_retryable"),
    ("organization_created", "failed_retryable"),
    ("workspace_created", "failed_retryable"),
    ("owner_assigned", "failed_retryable"),
    ("defaults_applied", "failed_retryable"),
    ("audit_initialized", "failed_retryable"),
    ("entitlements_applied", "failed_retryable"),

    ("failed_retryable", "failed_terminal"),
    ("failed_terminal", "rolled_back"),
}

provisioning = _Machine(
    "provisioning",
    PROVISIONING_TRANSITIONS,
    terminal={"ready", "failed_terminal", "rolled_back"},
)


# ──────────────────────────────────────────────────────────────────────────
# §10 — Subscription state machine
# ──────────────────────────────────────────────────────────────────────────
SUBSCRIPTION_TRANSITIONS: Set[Tuple[str, str]] = {
    # Trial lifecycle
    ("trial_active", "trial_ending"),
    ("trial_ending", "active"),
    ("trial_ending", "trial_expired"),
    ("trial_expired", "active"),
    ("trial_expired", "trial_closed"),

    # Paid lifecycle
    ("active", "past_due"),
    ("past_due", "grace_period"),
    ("grace_period", "active"),
    ("grace_period", "suspended"),
    ("suspended", "active"),
    ("suspended", "cancelled"),

    # Customer-initiated cancellation
    ("active", "cancelled_pending_end"),
    ("cancelled_pending_end", "cancelled"),
    ("cancelled_pending_end", "active"),   # undo a scheduled cancellation before period end
    ("cancelled", "closed"),

    # Demo / sandbox / enterprise
    ("demo", "sandbox"),
    ("sandbox", "active"),
    ("enterprise_pending", "sandbox"),
    ("enterprise_pending", "active"),

    # Manual ops admin paths
    ("manual_review", "trial_active"),
    ("manual_review", "closed"),
    ("trial_active", "manual_review"),
    ("active", "manual_review"),
}

subscription = _Machine(
    "subscription",
    SUBSCRIPTION_TRANSITIONS,
    terminal={"closed"},
)


# ──────────────────────────────────────────────────────────────────────────
# §11 — Billing state machine
# ──────────────────────────────────────────────────────────────────────────
BILLING_TRANSITIONS: Set[Tuple[str, str]] = {
    ("billing_not_required", "billing_profile_created"),
    ("billing_profile_created", "checkout_pending"),
    ("checkout_pending", "payment_pending"),
    ("payment_pending", "payment_succeeded"),
    ("payment_pending", "payment_failed"),
    ("payment_succeeded", "invoice_created"),
    ("invoice_created", "invoice_paid"),
    ("invoice_paid", "billing_reconciled"),
    ("payment_failed", "dunning_active"),
    ("dunning_active", "payment_succeeded"),
    ("dunning_active", "billing_reconciled"),
    ("checkout_pending", "checkout_pending"),  # idempotent re-checkout

    # Refund flow (allowed from any post-payment state)
    ("payment_succeeded", "refund_pending"),
    ("invoice_paid", "refund_pending"),
    ("billing_reconciled", "refund_pending"),
    ("refund_pending", "refund_completed"),

    # Void / reissue
    ("invoice_created", "invoice_voided"),
    ("payment_failed", "checkout_pending"),  # retry checkout after dunning failure
}

billing = _Machine(
    "billing",
    BILLING_TRANSITIONS,
    terminal={"refund_completed", "billing_reconciled", "invoice_voided"},
)


__all__ = [
    "IllegalTransitionError",
    "signup", "provisioning", "subscription", "billing",
]
