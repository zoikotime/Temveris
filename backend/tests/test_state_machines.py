"""State machine unit tests (§8, §9, §10, §11)."""

import pytest

from app.commercial.state_machines import (
    signup, provisioning, subscription, billing, IllegalTransitionError,
)


# ──────────────────────────────────────────────────────────────────────────
# §8 Signup
# ──────────────────────────────────────────────────────────────────────────
class TestSignupSM:
    def test_happy_trial_path(self):
        assert signup.transition("signup_started", "signup_submitted") == "signup_submitted"
        assert signup.transition("signup_submitted", "abuse_check_pending") == "abuse_check_pending"
        assert signup.transition("abuse_check_pending", "identity_created") == "identity_created"
        assert signup.transition("identity_created", "organization_created") == "organization_created"
        assert signup.transition("organization_created", "workspace_pending") == "workspace_pending"
        assert signup.transition("workspace_pending", "workspace_ready") == "workspace_ready"
        assert signup.transition("workspace_ready", "trial_created") == "trial_created"
        assert signup.transition("trial_created", "signup_complete") == "signup_complete"

    def test_paid_path(self):
        s = "workspace_ready"
        s = signup.transition(s, "paid_checkout_pending")
        s = signup.transition(s, "subscription_active")
        s = signup.transition(s, "signup_complete")
        assert s == "signup_complete"

    def test_abuse_blocked(self):
        s = signup.transition("abuse_check_pending", "abuse_blocked")
        assert s == "abuse_blocked"

    def test_illegal_jump_raises(self):
        with pytest.raises(IllegalTransitionError):
            signup.transition("signup_started", "signup_complete")

    def test_idempotent_same_state(self):
        assert signup.transition("workspace_ready", "workspace_ready") == "workspace_ready"


# ──────────────────────────────────────────────────────────────────────────
# §9 Provisioning
# ──────────────────────────────────────────────────────────────────────────
class TestProvisioningSM:
    def test_happy_path(self):
        chain = ["not_started", "queued", "identity_bound", "organization_created",
                 "workspace_created", "owner_assigned", "defaults_applied",
                 "audit_initialized", "entitlements_applied", "ready"]
        for a, b in zip(chain, chain[1:]):
            assert provisioning.transition(a, b) == b

    def test_failure_then_retry(self):
        s = provisioning.transition("queued", "failed_retryable")
        # Retry path
        s = provisioning.transition(s, "queued")
        assert s == "queued"

    def test_terminal_failure(self):
        s = provisioning.transition("failed_retryable", "failed_terminal")
        assert s == "failed_terminal"


# ──────────────────────────────────────────────────────────────────────────
# §10 Subscription
# ──────────────────────────────────────────────────────────────────────────
class TestSubscriptionSM:
    def test_trial_to_active(self):
        s = subscription.transition("trial_active", "trial_ending")
        s = subscription.transition(s, "active")
        assert s == "active"

    def test_trial_expires(self):
        s = subscription.transition("trial_active", "trial_ending")
        s = subscription.transition(s, "trial_expired")
        assert s == "trial_expired"

    def test_past_due_grace_recover(self):
        s = subscription.transition("active", "past_due")
        s = subscription.transition(s, "grace_period")
        s = subscription.transition(s, "active")
        assert s == "active"

    def test_past_due_grace_suspended_recover(self):
        s = subscription.transition("active", "past_due")
        s = subscription.transition(s, "grace_period")
        s = subscription.transition(s, "suspended")
        s = subscription.transition(s, "active")
        assert s == "active"

    def test_customer_cancellation_path(self):
        s = subscription.transition("active", "cancelled_pending_end")
        s = subscription.transition(s, "cancelled")
        s = subscription.transition(s, "closed")
        assert s == "closed"

    def test_enterprise_path(self):
        s = subscription.transition("enterprise_pending", "sandbox")
        s = subscription.transition(s, "active")
        assert s == "active"

    def test_illegal_active_to_trial(self):
        with pytest.raises(IllegalTransitionError):
            subscription.transition("active", "trial_active")


# ──────────────────────────────────────────────────────────────────────────
# §11 Billing
# ──────────────────────────────────────────────────────────────────────────
class TestBillingSM:
    def test_happy_path(self):
        s = "billing_not_required"
        s = billing.transition(s, "billing_profile_created")
        s = billing.transition(s, "checkout_pending")
        s = billing.transition(s, "payment_pending")
        s = billing.transition(s, "payment_succeeded")
        s = billing.transition(s, "invoice_created")
        s = billing.transition(s, "invoice_paid")
        s = billing.transition(s, "billing_reconciled")
        assert s == "billing_reconciled"

    def test_payment_failed_into_dunning(self):
        s = "checkout_pending"
        s = billing.transition(s, "payment_pending")
        s = billing.transition(s, "payment_failed")
        s = billing.transition(s, "dunning_active")
        assert s == "dunning_active"

    def test_refund(self):
        s = billing.transition("invoice_paid", "refund_pending")
        s = billing.transition(s, "refund_completed")
        assert s == "refund_completed"
