"""Tier-0 v3.0 canonical constants.

These are binding (spec section 1). Changing them changes the contract
between commercial, finance, billing, support, and engineering.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────
# §1 — Canonical commercial assumptions
# ──────────────────────────────────────────────────────────────────────────
TRIAL_LENGTH_DAYS = 30
CREDIT_CARD_REQUIRED_FOR_TRIAL = False

PLAN_VERIFIED = "verified"
PLAN_GOVERNED = "governed"
PLAN_SOVEREIGN = "sovereign"
PLAN_ENTERPRISE = "enterprise"


# ──────────────────────────────────────────────────────────────────────────
# Pricing — DERIVED, never restated
#
# `app.billing.service.SEED_PLANS` is the canonical catalog (§J1): it is what
# `ensure_plans` writes into `billing_plans` and actively corrects drift
# against, and it is what `POST /billing/upgrade` reads when it picks a Stripe
# Price. This module used to carry its own copy of the same numbers, so the
# quote a customer was shown and the amount Stripe was asked to charge came
# from two literals that nothing kept in step. They agreed — until someone
# edited one of them.
#
# Deriving here makes that divergence impossible rather than merely unlikely.
# The import is safe: `app.billing` never imports `app.commercial`, so there is
# no cycle, and main.py loads the billing service at startup regardless.
# ──────────────────────────────────────────────────────────────────────────
def _monthly_prices() -> dict:
    """Monthly list price per plan code, `None` for the sales-led tiers.

    `None` is load-bearing, not cosmetic: `quotes.compute_amounts` uses it to
    refuse a self-serve quote. The catalog stores 0.0 for Enterprise (a Float
    column cannot hold "no price"), so mapping the enterprise flag back to
    `None` is what keeps Enterprise from being quoted — and sold — at zero.
    """
    from app.billing.service import SEED_PLANS   # noqa: PLC0415 — see note above

    return {
        plan["code"]: (None if plan.get("is_enterprise") else plan["monthly_price_per_user"])
        for plan in SEED_PLANS
    }


def _annual_discount_pct() -> float:
    """The annual discount every self-serve plan shares.

    The catalog stores it per plan. They are equal today and the published
    "Save 20%" depends on that, so disagreement is a catalog bug rather than
    something to average over — it fails loudly here instead of silently
    picking one plan's rate for all of them.
    """
    from app.billing.service import SEED_PLANS   # noqa: PLC0415

    rates = {
        plan["annual_discount_percentage"]
        for plan in SEED_PLANS
        if not plan.get("is_enterprise")
    }
    if len(rates) != 1:
        raise RuntimeError(
            "billing catalog defines more than one annual discount for the "
            f"self-serve plans: {sorted(rates)}. The commercial layer quotes a "
            "single rate; reconcile SEED_PLANS before deploying."
        )
    return float(next(iter(rates)))


CANONICAL_PRICES_MONTHLY = _monthly_prices()
ANNUAL_DISCOUNT_PCT = _annual_discount_pct()

# Plans a PUBLIC, unauthenticated signup request may select.
#
# This is an allow-list, not a denylist: an unrecognised plan string is
# rejected rather than silently coerced. `plan` used to be an unvalidated
# free-form string, so a request could name any tier — including one whose
# entitlement profile grants unlimited seats, SSO, SCIM and legal hold — and
# receive it on a free trial with no payment method. `enterprise` is
# deliberately absent: it is sales-led, and the routing engine diverts it to
# the assisted lane, so it must never be provisionable from a public request.
SELF_SERVE_PLANS = (
    PLAN_VERIFIED,
    PLAN_GOVERNED,
    PLAN_SOVEREIGN,
)

DEFAULT_SELF_SERVE_PLAN = PLAN_VERIFIED

# Intents a public signup request may declare. `trial` and `paid` provision a
# tenant; `assisted` and `enterprise` are routed to sales without provisioning
# (see app.commercial.routing).
SELF_SERVE_INTENTS = ("trial", "paid", "assisted", "enterprise")

# ──────────────────────────────────────────────────────────────────────────
# §3 — Valid commercial states (exhaustive)
# ──────────────────────────────────────────────────────────────────────────
COMMERCIAL_STATES = {
    "trial_active",
    "trial_ending",
    "trial_expired",
    "trial_closed",
    "active",
    "past_due",
    "grace_period",
    "suspended",
    "cancelled_pending_end",
    "cancelled",
    "closed",
    "demo",
    "sandbox",
    "enterprise_pending",
    "manual_review",
}

# ──────────────────────────────────────────────────────────────────────────
# §8 — Signup state machine
# ──────────────────────────────────────────────────────────────────────────
SIGNUP_STATES = {
    "signup_started",
    "signup_submitted",
    "validation_failed",
    "abuse_check_pending",
    "abuse_blocked",
    "manual_review_required",
    "identity_created",
    "organization_created",
    "workspace_pending",
    "workspace_ready",
    "trial_created",
    "paid_checkout_pending",
    "paid_checkout_failed",
    "subscription_active",
    "signup_complete",
    "signup_failed",
}

# ──────────────────────────────────────────────────────────────────────────
# §9 — Tenant provisioning state machine
# ──────────────────────────────────────────────────────────────────────────
PROVISIONING_STATES = {
    "not_started",
    "queued",
    "identity_bound",
    "organization_created",
    "workspace_created",
    "owner_assigned",
    "defaults_applied",
    "audit_initialized",
    "entitlements_applied",
    "ready",
    "failed_retryable",
    "failed_terminal",
    "rolled_back",
}

# ──────────────────────────────────────────────────────────────────────────
# §11 — Billing state machine
# ──────────────────────────────────────────────────────────────────────────
BILLING_STATES = {
    "billing_not_required",
    "billing_profile_created",
    "checkout_pending",
    "payment_pending",
    "payment_succeeded",
    "payment_failed",
    "invoice_created",
    "invoice_paid",
    "invoice_voided",
    "refund_pending",
    "refund_completed",
    "dunning_active",
    "billing_reconciled",
}

# ──────────────────────────────────────────────────────────────────────────
# §12 — Ledger entry types
# ──────────────────────────────────────────────────────────────────────────
LEDGER_ENTRY_TYPES = {
    "subscription_charge",
    "seat_proration_charge",
    "seat_proration_credit",
    "plan_upgrade_charge",
    "plan_downgrade_credit",
    "tax_charge",
    "payment_received",
    "payment_failed",
    "refund_issued",
    "credit_applied",
    "manual_adjustment",
    "write_off",
}

# ──────────────────────────────────────────────────────────────────────────
# §7 — Commercial routing engine outputs
# ──────────────────────────────────────────────────────────────────────────
ROUTE_SELF_SERVE_TRIAL = "self_serve_trial"
ROUTE_SELF_SERVE_PAID = "self_serve_paid"
ROUTE_ASSISTED = "assisted"
ROUTE_ENTERPRISE = "enterprise"
ROUTE_MANUAL_REVIEW = "manual_review"
ROUTE_BLOCKED = "blocked"

ROUTING_OUTPUTS = {
    ROUTE_SELF_SERVE_TRIAL,
    ROUTE_SELF_SERVE_PAID,
    ROUTE_ASSISTED,
    ROUTE_ENTERPRISE,
    ROUTE_MANUAL_REVIEW,
    ROUTE_BLOCKED,
}

# ──────────────────────────────────────────────────────────────────────────
# §15 — Error codes
# ──────────────────────────────────────────────────────────────────────────
ERR_VALIDATION_FAILED = "VALIDATION_FAILED"
ERR_EMAIL_ALREADY_EXISTS = "EMAIL_ALREADY_EXISTS"
ERR_DISPOSABLE_EMAIL_BLOCKED = "DISPOSABLE_EMAIL_BLOCKED"
ERR_RATE_LIMITED = "RATE_LIMITED"
ERR_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
ERR_COUNTRY_RESTRICTED = "COUNTRY_RESTRICTED"
ERR_PAYMENT_DECLINED = "PAYMENT_DECLINED"
ERR_FRAUD_REVIEW_REQUIRED = "FRAUD_REVIEW_REQUIRED"
ERR_PROVISIONING_FAILED = "PROVISIONING_FAILED"
ERR_ENTITLEMENT_FAILED = "ENTITLEMENT_FAILED"
ERR_WEBHOOK_FAILED = "WEBHOOK_FAILED"
ERR_SUBSCRIPTION_STATE_INVALID = "SUBSCRIPTION_STATE_INVALID"
ERR_IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
ERR_REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
ERR_INTERNAL = "INTERNAL_ERROR"
ERR_SEAT_OVER_ALLOCATION = "SEAT_OVER_ALLOCATION"
# Public-signup hardening
ERR_CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
ERR_CHALLENGE_FAILED = "CHALLENGE_FAILED"
ERR_WEAK_PASSWORD = "WEAK_PASSWORD"
ERR_INVALID_PLAN = "INVALID_PLAN"
ERR_TEAM_LIMIT_REACHED = "TEAM_LIMIT_REACHED"

ERROR_CODES = {
    ERR_VALIDATION_FAILED,
    ERR_EMAIL_ALREADY_EXISTS,
    ERR_DISPOSABLE_EMAIL_BLOCKED,
    ERR_RATE_LIMITED,
    ERR_MANUAL_REVIEW_REQUIRED,
    ERR_COUNTRY_RESTRICTED,
    ERR_PAYMENT_DECLINED,
    ERR_FRAUD_REVIEW_REQUIRED,
    ERR_PROVISIONING_FAILED,
    ERR_ENTITLEMENT_FAILED,
    ERR_WEBHOOK_FAILED,
    ERR_SUBSCRIPTION_STATE_INVALID,
    ERR_IDEMPOTENCY_KEY_REUSED,
    ERR_REQUEST_IN_PROGRESS,
    ERR_INTERNAL,
    ERR_SEAT_OVER_ALLOCATION,
    ERR_CHALLENGE_REQUIRED,
    ERR_CHALLENGE_FAILED,
    ERR_WEAK_PASSWORD,
    ERR_INVALID_PLAN,
    ERR_TEAM_LIMIT_REACHED,
}

# ──────────────────────────────────────────────────────────────────────────
# Public signup abuse controls
# ──────────────────────────────────────────────────────────────────────────
# Per-IP ceiling on POST /api/v1/signup, applied by the shared slowapi
# limiter. Account creation is a rare action even behind a shared office NAT
# — a handful of admins per hour from one address is generous — so this can be
# far tighter than the auth routes' 200/minute. The DB-backed velocity buckets
# in app.commercial.fraud remain the second layer.
SIGNUP_RATE_LIMIT = "10/hour"

# GET /api/v1/billing/quote/preview — the plan-summary panel on the public
# signup page. Read-only, no DB writes and no tenant data, so the ceiling is
# generous: it exists to stop a scraper hammering the endpoint, not to throttle
# visitors. One page load is one call, and a whole office shares one NAT'd IP.
PUBLIC_QUOTE_PREVIEW_RATE_LIMIT = "120/hour"
# Unauthenticated verification-resend ceiling. The per-user 60s cooldown in
# app.commercial.identity is a fairness control, not an abuse control: it does
# nothing against an attacker walking user ids.
VERIFICATION_RESEND_RATE_LIMIT = "5/hour"

# ──────────────────────────────────────────────────────────────────────────
# §16 — Event taxonomy
# ──────────────────────────────────────────────────────────────────────────
EVENT_SUCCESS = [
    "signup.started", "signup.submitted", "signup.completed",
    "user.created", "email_verification.sent", "email_verification.completed",
    "organization.created", "workspace.created", "tenant.provisioned",
    "trial.started", "subscription.created", "billing.customer_created",
    "checkout.session_created", "payment.succeeded", "invoice.created",
    "entitlements.computed", "entitlements.snapshot.created", "entitlements.changed",
    "workspace.first_login",
    "workspace.first_policy_configured", "workspace.first_invite_sent",
    "workspace.first_meaningful_action", "trial.converted",
    "subscription.activated",
    "subscription.cancellation_scheduled", "subscription.cancelled",
    "subscription.reactivated", "subscription.seats_changed",
    "subscription.past_due",
    "notification.sent", "crm.lead.created",
    # Activation flow, Step 2 — the organisation form was saved. Registered so
    # emit_event does not log it as unknown. (NOTE: the neighbouring
    # "onboarding.setup_completed", emitted by routers/onboarding.py, is absent
    # from this list and has always warned — a pre-existing gap, left alone
    # here because it is not this change's business.)
    "onboarding.organization_configured",
]

EVENT_FAILURE = [
    "signup.validation_failed", "signup.rate_limited", "signup.abuse_flagged",
    "email_verification.failed", "email_verification.expired",
    "email_delivery.failed", "payment.declined", "payment.fraud_flagged",
    "billing.customer_creation_failed", "subscription.creation_failed",
    "tenant.provisioning_failed", "workspace.creation_failed",
    "entitlements.computation_failed", "webhook.processing_failed",
]

EVENT_RECOVERY = [
    "tenant.provisioning_retried", "tenant.provisioning_recovered",
    "billing.reconciled", "subscription.recovered", "payment.retried",
    "payment.recovered", "entitlements.recomputed", "manual_review.resolved",
]

EVENT_CONSENT = [
    "terms.accepted", "privacy.acknowledged", "trial_terms.accepted",
    "marketing_consent.granted", "marketing_consent.withdrawn",
    "data_processing_acknowledged",
]

VALID_EVENT_TYPES = set(EVENT_SUCCESS + EVENT_FAILURE + EVENT_RECOVERY + EVENT_CONSENT)

# ──────────────────────────────────────────────────────────────────────────
# §17 — Fraud/abuse thresholds
# ──────────────────────────────────────────────────────────────────────────
FRAUD_SAME_IP_HOURLY_CHALLENGE = 5
FRAUD_SAME_DEVICE_HOURLY_REVIEW = 3
FRAUD_SAME_DOMAIN_HOURLY_REVIEW = 10
FRAUD_FAILED_PAYMENT_REVIEW = 3
VERIFICATION_RESEND_COOLDOWN_SECONDS = 60

# ──────────────────────────────────────────────────────────────────────────
# Email-verification CODE (activation flow, Step 2 — Verify)
#
# The emailed link stays exactly as it was; this is the second credential on
# the same verification row, for people who type the code instead of clicking.
# The numbers mirror the platform's existing OTP posture in
# app.auth.service (5-minute life, 5 wrong guesses) rather than inventing a
# second standard — but the code lives on the commercial verification row, not
# in `password_reset_otps`, so it can never satisfy a password reset.
# ──────────────────────────────────────────────────────────────────────────
VERIFICATION_CODE_LENGTH = 6
# Deliberately far shorter than the link's 48 hours. A 6-digit code is a weak
# secret; its defence is that it is short-lived and attempt-limited.
VERIFICATION_CODE_TTL_SECONDS = 15 * 60
VERIFICATION_CODE_MAX_ATTEMPTS = 5
# Per-IP ceiling on the verify endpoint. There was none: unnecessary for a
# 43-character token, essential once a 6-digit code is accepted.
VERIFICATION_VERIFY_RATE_LIMIT = "30/hour"

ERR_VERIFICATION_CODE_INVALID = "VERIFICATION_CODE_INVALID"
ERR_VERIFICATION_CODE_EXPIRED = "VERIFICATION_CODE_EXPIRED"
ERR_VERIFICATION_CODE_LOCKED = "VERIFICATION_CODE_LOCKED"

# ──────────────────────────────────────────────────────────────────────────
# Worker-count bands offered on the activation flow's Workspace details.
#
# Stored as the band's upper bound in `CommercialOrganization.expected_users`,
# which already exists for exactly this purpose. Defined here rather than in
# the UI so the label a visitor picks and the number the server keeps cannot
# disagree; the list is served to the client, never hardcoded there.
#
# INFORMATIONAL ONLY. This is a segmentation signal. It must never reach
# `CommercialSubscription.seat_count` — billable seats are counted from real
# users by app.billing.seats, and letting a self-declared band set the billed
# quantity would charge customers for people who do not exist.
# ──────────────────────────────────────────────────────────────────────────
WORKER_COUNT_BANDS = (
    {"value": "1-10",    "label": "1\u201310 active workers",    "expected_users": 10},
    {"value": "11-50",   "label": "11\u201350 active workers",   "expected_users": 50},
    {"value": "51-200",  "label": "51\u2013200 active workers",  "expected_users": 200},
    {"value": "201-500", "label": "201\u2013500 active workers", "expected_users": 500},
    {"value": "501+",    "label": "501+ active workers",          "expected_users": 1000},
)
WORKER_COUNT_BAND_VALUES = {b["value"]: b["expected_users"] for b in WORKER_COUNT_BANDS}

MANUAL_REVIEW_OUTCOMES = {
    "approve",
    "approve_with_restrictions",
    "request_more_information",
    "route_to_sales",
    "block",
    "close_account",
    "escalate_to_security",
}

# ──────────────────────────────────────────────────────────────────────────
# §13 — Entitlement features & limits (canonical names)
# ──────────────────────────────────────────────────────────────────────────
ENTITLEMENT_FEATURES = [
    "time_tracking", "dashboards", "audit_logs", "advanced_compliance",
    "legal_hold", "sso", "scim", "api_access", "data_export",
]

ENTITLEMENT_LIMITS = [
    "seat_limit", "retention_days", "api_requests_per_day", "workspace_count",
    "team_limit",
]

ENTITLEMENTS_CACHE_TTL_SECONDS = 3600  # 1 hour per spec

# ──────────────────────────────────────────────────────────────────────────
# §17 — Disposable email domain blocklist (small, conservative starter set)
# Operations can extend via app_settings.disposable_email_domains_extra.
# ──────────────────────────────────────────────────────────────────────────
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "tempmail.com", "throwaway.email", "yopmail.com", "trashmail.com",
    "fakeinbox.com", "getnada.com", "maildrop.cc", "sharklasers.com",
    "dispostable.com", "tempinbox.com", "discard.email",
}

FREE_WEBMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com",
}

# Sanctioned/restricted jurisdictions (ISO-3166 alpha-2). Conservative starter set;
# operations should pull from the official OFAC/UK/EU lists in production.
SANCTIONED_COUNTRIES = {"IR", "KP", "SY", "CU"}

# Government / regulated TLD hints
GOVERNMENT_TLDS = {".gov", ".mil", ".gov.uk", ".gov.au", ".gov.in"}
FINANCIAL_KEYWORDS = {"bank", "capital", "wealth", "asset", "finance", "fund"}
HEALTHCARE_KEYWORDS = {"health", "hospital", "clinic", "medical", "pharma", "bio"}

# ──────────────────────────────────────────────────────────────────────────
# §20 — i18n
# ──────────────────────────────────────────────────────────────────────────
LAUNCH_LANGUAGES = ["en-US", "en-GB", "es", "fr", "de", "pt-BR"]
DEFAULT_LANGUAGE = "en-US"

# ──────────────────────────────────────────────────────────────────────────
# §21 — Alert thresholds (used by Analytics router/metrics)
# ──────────────────────────────────────────────────────────────────────────
ALERT_PROVISIONING_FAILURE_PCT = 1.0
ALERT_API_ERROR_PCT = 1.0
ALERT_EMAIL_DELIVERY_FAILURE_PCT = 5.0
ALERT_PAYMENT_FAILURE_SPIKE_PCT = 20.0
ALERT_SIGNUP_DROP_PCT = 20.0

# Legal versions (current). Bump these when terms/privacy change.
TERMS_VERSION_CURRENT = "2026-05-01"
PRIVACY_VERSION_CURRENT = "2026-05-01"
TRIAL_TERMS_VERSION_CURRENT = "2026-05-01"
