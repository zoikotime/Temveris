"""Commercial Activation System — SQLAlchemy models (Tier-0 spec v3.0).

These tables are additive: they coexist with the legacy single-tenant
tables (which use organization_id="default"). The commercial layer mints
real organization_ids on self-serve signup.

Every commercial-layer model is also recorded in the create_all sweep via
the shared `database.database.Base` metadata, so SQLAlchemy will pick
them up alongside the existing models.
"""

from __future__ import annotations

from datetime import datetime, UTC
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Text, Float, JSON, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Used as the column default/onupdate callable.

    Replaces the deprecated ``datetime.utcnow`` (naive). All commercial
    DateTime columns are ``timezone=True``, and commercial services compare
    against ``datetime.now(timezone.utc)`` (aware), so defaults must be aware
    too to avoid naive/aware comparison errors.
    """
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────────
# ORGANIZATION  (one per customer tenant)
# ──────────────────────────────────────────────────────────────────────────
class CommercialOrganization(Base):
    __tablename__ = "commercial_organizations"

    id              = Column(String(40), primary_key=True)   # "org_<uuid7-ish>"
    name            = Column(String(255), nullable=False)
    legal_name      = Column(String(255), nullable=True)
    country         = Column(String(2), nullable=True)        # ISO 3166-1 alpha-2
    jurisdiction    = Column(String(40), nullable=True)
    industry        = Column(String(80), nullable=True)
    expected_users  = Column(Integer, nullable=True)
    domain          = Column(String(255), nullable=True, index=True)

    # Link back to the operational organizations table (integer PK anchor).
    # Set when an org is provisioned via super-admin; NULL for self-signup orgs
    # that haven't been provisioned yet.
    operational_org_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)

    # Ownership: the user that first created this org (account owner).
    owner_user_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Source of truth for which commercial state the tenant is in.
    # MUST be one of constants.COMMERCIAL_STATES.
    commercial_state = Column(String(40), nullable=False, default="trial_active", index=True)

    created_at      = Column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at      = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    workspaces      = relationship("CommercialWorkspace", back_populates="organization", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_commercial_org_state_created", "commercial_state", "created_at"),
    )


# ──────────────────────────────────────────────────────────────────────────
# WORKSPACE  (operational container within an organization)
# ──────────────────────────────────────────────────────────────────────────
class CommercialWorkspace(Base):
    __tablename__ = "commercial_workspaces"

    id              = Column(String(40), primary_key=True)   # "wsp_<uuid>"
    organization_id = Column(String(40), ForeignKey("commercial_organizations.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    slug            = Column(String(120), nullable=False, unique=True, index=True)
    name            = Column(String(255), nullable=False)
    region          = Column(String(40), nullable=True)
    timezone        = Column(String(64), nullable=True)
    currency        = Column(String(3), nullable=True)
    locale          = Column(String(10), nullable=True, default="en-US")

    # Provisioning lifecycle (§9). MUST be one of PROVISIONING_STATES.
    provisioning_state = Column(String(40), nullable=False, default="not_started", index=True)

    # Time-to-value milestones (§19.3). Set by the orchestrator and the app
    # when each milestone is first reached. NULL until then.
    first_login_at              = Column(DateTime(timezone=True), nullable=True)
    first_email_verified_at     = Column(DateTime(timezone=True), nullable=True)
    first_policy_configured_at  = Column(DateTime(timezone=True), nullable=True)
    first_invite_sent_at        = Column(DateTime(timezone=True), nullable=True)
    first_teammate_accepted_at  = Column(DateTime(timezone=True), nullable=True)
    first_work_session_at       = Column(DateTime(timezone=True), nullable=True)
    first_report_viewed_at      = Column(DateTime(timezone=True), nullable=True)

    created_at      = Column(DateTime(timezone=True), default=_utcnow)
    updated_at      = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    organization    = relationship("CommercialOrganization", back_populates="workspaces")


# ──────────────────────────────────────────────────────────────────────────
# SIGNUP STATE  (§8) — one row per signup attempt
# ──────────────────────────────────────────────────────────────────────────
class CommercialSignup(Base):
    __tablename__ = "commercial_signups"

    id                = Column(String(40), primary_key=True)   # "sgn_<uuid>"
    email             = Column(String(255), nullable=False, index=True)
    state             = Column(String(40), nullable=False, default="signup_started", index=True)
    route             = Column(String(40), nullable=True)      # commercial routing engine output
    intent            = Column(String(20), nullable=False, default="trial")  # trial | paid | assisted | enterprise
    plan              = Column(String(40), nullable=True)
    expected_users    = Column(Integer, nullable=True)
    country           = Column(String(2), nullable=True)
    locale            = Column(String(10), nullable=True)
    timezone          = Column(String(64), nullable=True)

    # Resolved IDs once provisioning lands.
    user_id           = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    organization_id   = Column(String(40), nullable=True, index=True)
    workspace_id      = Column(String(40), nullable=True)

    # Source attribution
    utm_source        = Column(String(120), nullable=True)
    utm_medium        = Column(String(120), nullable=True)
    utm_campaign      = Column(String(120), nullable=True)
    referrer          = Column(Text, nullable=True)

    # Client capture
    ip_address        = Column(String(45), nullable=True, index=True)
    user_agent_hash   = Column(String(64), nullable=True, index=True)
    device_fingerprint = Column(String(64), nullable=True, index=True)

    # Failure metadata
    failure_code      = Column(String(60), nullable=True)
    failure_message   = Column(Text, nullable=True)

    # Correlation
    correlation_id    = Column(String(40), nullable=False, index=True)

    created_at        = Column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at        = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# COMMERCIAL EVENT  (§16) — append-only governed event log
# Separate from the legacy AuditLog so the new event taxonomy stays clean.
# ──────────────────────────────────────────────────────────────────────────
class CommercialEvent(Base):
    __tablename__ = "commercial_events"

    id                = Column(Integer, primary_key=True, index=True)
    event_id          = Column(String(40), nullable=False, unique=True, index=True)
    event_type        = Column(String(80), nullable=False, index=True)
    occurred_at       = Column(DateTime(timezone=True), nullable=False, index=True)
    source_service    = Column(String(60), nullable=False)
    tenant_id         = Column(String(40), nullable=True, index=True)
    organization_id   = Column(String(40), nullable=True, index=True)
    workspace_id      = Column(String(40), nullable=True, index=True)
    user_id           = Column(Integer, nullable=True, index=True)
    correlation_id    = Column(String(40), nullable=True, index=True)
    payload           = Column(JSON, nullable=False, default=dict)

    # When this event is itself a state transition, capture from→to.
    state_from        = Column(String(60), nullable=True)
    state_to          = Column(String(60), nullable=True)

    # When the event represents a failure, capture canonical error code.
    error_code        = Column(String(60), nullable=True, index=True)

    created_at        = Column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        Index("ix_commercial_events_corr_type", "correlation_id", "event_type"),
        Index("ix_commercial_events_org_type_at", "organization_id", "event_type", "occurred_at"),
    )


# ──────────────────────────────────────────────────────────────────────────
# CONSENT LOG  (§18) — immutable
# ──────────────────────────────────────────────────────────────────────────
class CommercialConsent(Base):
    __tablename__ = "commercial_consents"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_id = Column(String(40), nullable=True, index=True)
    signup_id       = Column(String(40), nullable=True, index=True)

    consent_type    = Column(String(60), nullable=False, index=True)  # terms.accepted | privacy.acknowledged | ...
    version         = Column(String(40), nullable=True)               # legal document version
    granted         = Column(Boolean, nullable=False, default=True)

    ip_address      = Column(String(45), nullable=True)
    user_agent_hash = Column(String(64), nullable=True)
    country         = Column(String(2), nullable=True)
    locale          = Column(String(10), nullable=True)

    occurred_at     = Column(DateTime(timezone=True), default=_utcnow, index=True)


# ──────────────────────────────────────────────────────────────────────────
# COMMERCIAL SUBSCRIPTION (§10) — per-tenant subscription state machine.
#
# This is the v1 multi-tenant subscription record. It coexists with the
# legacy `billing_subscriptions` row (organization_id="default") which the
# admin billing UI still reads. Once the legacy code migrates to tenant IDs
# we can collapse the two.
# ──────────────────────────────────────────────────────────────────────────
class CommercialSubscription(Base):
    __tablename__ = "commercial_subscriptions"

    id                  = Column(String(40), primary_key=True)   # "sub_<uuid>"
    organization_id     = Column(String(40), ForeignKey("commercial_organizations.id",
                                                        ondelete="CASCADE"),
                                  nullable=False, index=True, unique=True)
    plan_code           = Column(String(40), nullable=False, default="verified")  # verified|governed|sovereign|enterprise
    billing_cycle       = Column(String(20), nullable=False, default="monthly")    # monthly|annual
    seat_count          = Column(Integer, nullable=False, default=1)
    currency            = Column(String(3), nullable=False, default="USD")

    # MUST be one of COMMERCIAL_STATES.
    state               = Column(String(40), nullable=False, default="trial_active", index=True)

    trial_started_at    = Column(DateTime(timezone=True), nullable=True)
    trial_ends_at       = Column(DateTime(timezone=True), nullable=True)
    trial_ending_notified_at = Column(DateTime(timezone=True), nullable=True)
    trial_expired_at    = Column(DateTime(timezone=True), nullable=True)

    active_since        = Column(DateTime(timezone=True), nullable=True)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at        = Column(DateTime(timezone=True), nullable=True)
    closed_at           = Column(DateTime(timezone=True), nullable=True)
    suspended_at        = Column(DateTime(timezone=True), nullable=True)
    past_due_since      = Column(DateTime(timezone=True), nullable=True)

    provider            = Column(String(40), nullable=True, default="stub")  # stripe|stub|manual
    provider_subscription_id = Column(String(120), nullable=True, index=True)

    enterprise_override = Column(JSON, nullable=True, default=dict)  # custom limits/features

    created_at          = Column(DateTime(timezone=True), default=_utcnow)
    updated_at          = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# ENTITLEMENTS CACHE  (§13) — computed entitlement objects, 1h TTL
# ──────────────────────────────────────────────────────────────────────────
class EntitlementsCache(Base):
    __tablename__ = "commercial_entitlements_cache"

    organization_id = Column(String(40), primary_key=True)
    computed_at     = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at      = Column(DateTime(timezone=True), nullable=False)
    profile_json    = Column(JSON, nullable=False, default=dict)
    plan_code       = Column(String(40), nullable=False)
    commercial_state = Column(String(40), nullable=False)


# ──────────────────────────────────────────────────────────────────────────
# BILLING LEDGER  (§12) — every billing mutation creates one row
# ──────────────────────────────────────────────────────────────────────────
class BillingLedgerEntry(Base):
    __tablename__ = "commercial_billing_ledger"

    id                = Column(Integer, primary_key=True, index=True)
    entry_id          = Column(String(40), nullable=False, unique=True, index=True)
    organization_id   = Column(String(40), nullable=False, index=True)
    subscription_id   = Column(String(40), nullable=True, index=True)
    invoice_id        = Column(String(40), nullable=True, index=True)
    payment_id        = Column(String(40), nullable=True, index=True)
    parent_entry_id   = Column(String(40), nullable=True, index=True)  # for refund→payment linkage

    entry_type        = Column(String(40), nullable=False, index=True)  # LEDGER_ENTRY_TYPES
    amount_cents      = Column(Integer, nullable=False, default=0)
    currency          = Column(String(3), nullable=False, default="USD")
    description       = Column(Text, nullable=True)
    metadata_json     = Column(JSON, nullable=True, default=dict)

    occurred_at       = Column(DateTime(timezone=True), default=_utcnow, index=True)
    created_at        = Column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_ledger_org_type_at", "organization_id", "entry_type", "occurred_at"),
    )


# ──────────────────────────────────────────────────────────────────────────
# BILLING INVOICE  (multi-tenant v1 — distinct from legacy BillingInvoice)
# ──────────────────────────────────────────────────────────────────────────
class CommercialInvoice(Base):
    __tablename__ = "commercial_invoices"

    id                = Column(String(40), primary_key=True)  # "inv_<uuid>"
    organization_id   = Column(String(40), nullable=False, index=True)
    subscription_id   = Column(String(40), nullable=True, index=True)
    number            = Column(String(40), nullable=False, unique=True, index=True)
    period_start      = Column(DateTime(timezone=True), nullable=True)
    period_end        = Column(DateTime(timezone=True), nullable=True)
    subtotal_cents    = Column(Integer, nullable=False, default=0)
    tax_cents         = Column(Integer, nullable=False, default=0)
    discount_cents    = Column(Integer, nullable=False, default=0)
    total_cents       = Column(Integer, nullable=False, default=0)
    currency          = Column(String(3), nullable=False, default="USD")
    status            = Column(String(30), nullable=False, default="draft", index=True)  # draft|issued|paid|void|refunded
    issued_at         = Column(DateTime(timezone=True), nullable=True)
    paid_at           = Column(DateTime(timezone=True), nullable=True)
    voided_at         = Column(DateTime(timezone=True), nullable=True)
    provider          = Column(String(40), nullable=True)
    provider_invoice_id = Column(String(120), nullable=True, index=True)

    created_at        = Column(DateTime(timezone=True), default=_utcnow)
    updated_at        = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# CHECKOUT SESSION  (§11) — payment provider checkout handle
# ──────────────────────────────────────────────────────────────────────────
class CheckoutSession(Base):
    __tablename__ = "commercial_checkout_sessions"

    id              = Column(String(40), primary_key=True)  # "chk_<uuid>"
    organization_id = Column(String(40), nullable=False, index=True)
    workspace_id    = Column(String(40), nullable=True)
    plan_code       = Column(String(40), nullable=False)
    billing_cycle   = Column(String(20), nullable=False, default="monthly")
    seat_count      = Column(Integer, nullable=False, default=1)
    currency        = Column(String(3), nullable=False, default="USD")
    success_url     = Column(Text, nullable=True)
    cancel_url      = Column(Text, nullable=True)
    state           = Column(String(30), nullable=False, default="checkout_pending", index=True)
    provider        = Column(String(40), nullable=False, default="stub")
    provider_session_id = Column(String(120), nullable=True)
    provider_redirect_url = Column(Text, nullable=True)
    correlation_id  = Column(String(40), nullable=True, index=True)
    # Server-authoritative quote this checkout was created from (§D-05). When
    # set, the charged amount is read from the quote, never recomputed client
    # or browser side. Nullable for the legacy plan/cycle/seats path.
    quote_id        = Column(String(40), nullable=True, index=True)
    completed_at    = Column(DateTime(timezone=True), nullable=True)
    failed_at       = Column(DateTime(timezone=True), nullable=True)
    # §6.2 critical-failure recovery: when payment succeeds but a
    # downstream provisioning step (entitlements/workspace state) fails,
    # we flag the session here so the admin console + reconciliation
    # job can retry or queue a refund without losing the payment record.
    recovery_required = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    recovery_reason   = Column(Text, nullable=True)
    recovery_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    created_at      = Column(DateTime(timezone=True), default=_utcnow)
    updated_at      = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# SERVER-AUTHORITATIVE PRICE QUOTE  (§D-05, §6.3, §12.1)
# The frontend renders this by id and never recomputes money.
# ──────────────────────────────────────────────────────────────────────────
class PriceQuote(Base):
    __tablename__ = "commercial_price_quotes"

    id              = Column(String(40), primary_key=True)  # "qot_<uuid>"
    organization_id = Column(String(40), nullable=True, index=True)
    workspace_id    = Column(String(40), nullable=True)
    plan_code       = Column(String(40), nullable=False)
    billing_cycle   = Column(String(20), nullable=False, default="monthly")
    seat_count      = Column(Integer, nullable=False, default=1)
    currency        = Column(String(3), nullable=False, default="USD")
    country         = Column(String(2), nullable=True)
    promotion_code  = Column(String(60), nullable=True)

    subtotal_cents        = Column(Integer, nullable=False, default=0)
    discount_cents        = Column(Integer, nullable=False, default=0)
    tax_cents             = Column(Integer, nullable=False, default=0)
    tax_state             = Column(String(20), nullable=False, default="estimated")  # estimated|final|exempt
    total_cents           = Column(Integer, nullable=False, default=0)
    renewal_amount_cents  = Column(Integer, nullable=False, default=0)
    renewal_at            = Column(DateTime(timezone=True), nullable=True)
    line_items            = Column(JSON, nullable=False, default=list)

    state           = Column(String(20), nullable=False, default="active", index=True)  # active|consumed|expired
    expires_at      = Column(DateTime(timezone=True), nullable=True, index=True)
    correlation_id  = Column(String(40), nullable=True, index=True)
    created_at      = Column(DateTime(timezone=True), default=_utcnow, index=True)


# ──────────────────────────────────────────────────────────────────────────
# IDEMPOTENT WEBHOOK PROCESSING  (§11 — webhooks idempotent)
# ──────────────────────────────────────────────────────────────────────────
class ProviderWebhookReceipt(Base):
    __tablename__ = "commercial_webhook_receipts"

    id              = Column(Integer, primary_key=True)
    provider        = Column(String(40), nullable=False, index=True)
    provider_event_id = Column(String(160), nullable=False, index=True)
    event_type      = Column(String(80), nullable=False)
    payload         = Column(JSON, nullable=False, default=dict)
    processed_at    = Column(DateTime(timezone=True), nullable=True)
    status          = Column(String(20), nullable=False, default="received", index=True)  # received|processed|failed|dead_letter
    attempts        = Column(Integer, nullable=False, default=0)
    last_error      = Column(Text, nullable=True)
    received_at     = Column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_provider_event"),
    )


# ──────────────────────────────────────────────────────────────────────────
# FRAUD SIGNALS  (§17) — every fraud check writes a signal row
# ──────────────────────────────────────────────────────────────────────────
class FraudSignal(Base):
    __tablename__ = "commercial_fraud_signals"

    id              = Column(Integer, primary_key=True, index=True)
    signup_id       = Column(String(40), nullable=True, index=True)
    organization_id = Column(String(40), nullable=True, index=True)
    signal_type     = Column(String(60), nullable=False, index=True)  # disposable_email | high_velocity_ip | ...
    severity        = Column(String(20), nullable=False, default="low")  # low|medium|high|critical
    score_delta     = Column(Float, nullable=False, default=0.0)
    details         = Column(JSON, nullable=True, default=dict)

    created_at      = Column(DateTime(timezone=True), default=_utcnow, index=True)


# ──────────────────────────────────────────────────────────────────────────
# MANUAL REVIEW QUEUE  (§17.3)
# ──────────────────────────────────────────────────────────────────────────
class ManualReviewCase(Base):
    __tablename__ = "commercial_manual_review"

    id              = Column(Integer, primary_key=True, index=True)
    case_id         = Column(String(40), nullable=False, unique=True, index=True)
    signup_id       = Column(String(40), nullable=True, index=True)
    organization_id = Column(String(40), nullable=True, index=True)
    reason          = Column(String(120), nullable=False)
    severity        = Column(String(20), nullable=False, default="medium")
    fraud_score     = Column(Float, nullable=False, default=0.0)
    details         = Column(JSON, nullable=True, default=dict)

    status          = Column(String(30), nullable=False, default="pending", index=True)
    # outcomes: approve | approve_with_restrictions | request_more_information
    #           route_to_sales | block | close_account | escalate_to_security
    outcome         = Column(String(40), nullable=True)
    reviewer_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    review_notes    = Column(Text, nullable=True)
    reviewed_at     = Column(DateTime(timezone=True), nullable=True)

    created_at      = Column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at      = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# CRM LEAD  (§6.3 / §6.4) — assisted + enterprise lead capture
# ──────────────────────────────────────────────────────────────────────────
class CrmLead(Base):
    __tablename__ = "commercial_leads"

    id                = Column(String(40), primary_key=True)  # "lead_<uuid>"
    intent            = Column(String(40), nullable=False)     # assisted | enterprise
    first_name        = Column(String(120), nullable=True)
    last_name         = Column(String(120), nullable=True)
    work_email        = Column(String(255), nullable=False, index=True)
    company_name      = Column(String(255), nullable=True)
    country           = Column(String(2), nullable=True)
    expected_users    = Column(Integer, nullable=True)
    industry          = Column(String(80), nullable=True)
    deployment_pref   = Column(String(40), nullable=True)
    integrations      = Column(Text, nullable=True)
    compliance_needs  = Column(Text, nullable=True)
    notes             = Column(Text, nullable=True)

    routing_score     = Column(Float, nullable=False, default=0.0)
    assigned_to_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status            = Column(String(30), nullable=False, default="new", index=True)
    # statuses: new | contacted | qualified | demo_scheduled | sandbox_created
    #           contract_sent | contract_signed | provisioned | lost

    utm_source        = Column(String(120), nullable=True)
    utm_medium        = Column(String(120), nullable=True)
    utm_campaign      = Column(String(120), nullable=True)
    referrer          = Column(Text, nullable=True)

    crm_external_id   = Column(String(160), nullable=True, index=True)

    created_at        = Column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at        = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# EMAIL VERIFICATION TOKENS  (§19 — confirm email)
# ──────────────────────────────────────────────────────────────────────────
class EmailVerificationToken(Base):
    __tablename__ = "commercial_email_verification_tokens"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(String(40), nullable=True, index=True)
    token_hash      = Column(String(64), nullable=False, unique=True, index=True)

    # Second credential for the SAME verification event: the 6-digit code the
    # activation flow's Verify step asks the visitor to type. One row carries
    # both, so clicking the emailed link and typing the code converge on one
    # `consumed_at` — whichever lands first wins and the other reports
    # ALREADY_CONSUMED, an outcome the flow already handles.
    #
    # Nullable because rows minted before this shipped are link-only and must
    # keep working until they expire.
    #
    # Deliberately NOT `password_reset_otps`: that table's rows are what
    # `POST /auth/reset-password` checks for eligibility, so a signup code
    # living there would double as a password-reset code.
    code_hash       = Column(String(64), nullable=True)
    # Wrong-guess counter. A 43-character token needs none — a 6-digit code has
    # a 10^6 space and would be trivially brute-forced without it.
    code_attempts   = Column(Integer, nullable=False, default=0, server_default="0")

    expires_at      = Column(DateTime(timezone=True), nullable=False)
    consumed_at     = Column(DateTime(timezone=True), nullable=True)
    last_sent_at    = Column(DateTime(timezone=True), default=_utcnow)
    resend_count    = Column(Integer, nullable=False, default=0)
    created_at      = Column(DateTime(timezone=True), default=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# TRIAL EMAIL SCHEDULE  (§6.1 — Day 0/1/7/14/18/25/30/32/35)
# Tracks which lifecycle emails have already been sent so we never duplicate.
# ──────────────────────────────────────────────────────────────────────────
class TrialEmailSent(Base):
    __tablename__ = "commercial_trial_emails_sent"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(String(40), nullable=False, index=True)
    subscription_id = Column(String(40), nullable=True, index=True)
    day_marker      = Column(String(20), nullable=False)  # "day_0" | "day_1" | "day_7" | ... | "day_35"
    sent_at         = Column(DateTime(timezone=True), default=_utcnow, index=True)
    status          = Column(String(20), nullable=False, default="sent")  # sent|skipped|failed
    last_error      = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "day_marker", name="uq_trial_email_org_day"),
    )


# ──────────────────────────────────────────────────────────────────────────
# RATE-LIMIT COUNTERS  (§17 — same-IP, same-device, same-domain throttling)
# Coarse hourly buckets; replaced by Redis in a real deployment but the
# DB-backed version keeps single-process correctness without new infra.
# ──────────────────────────────────────────────────────────────────────────
class SignupRateBucket(Base):
    __tablename__ = "commercial_signup_rate_buckets"

    id              = Column(Integer, primary_key=True, index=True)
    bucket_key      = Column(String(160), nullable=False, index=True)   # "ip:1.2.3.4" / "domain:acme.com" / "device:<hash>"
    window_start    = Column(DateTime(timezone=True), nullable=False, index=True)
    count           = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("bucket_key", "window_start", name="uq_signup_rate_bucket"),
    )


# ──────────────────────────────────────────────────────────────────────────
# ONBOARDING CHECKLIST  (§19.2)
# One row per workspace tracking which steps the customer has completed.
# ──────────────────────────────────────────────────────────────────────────
class OnboardingChecklist(Base):
    __tablename__ = "commercial_onboarding_checklist"

    organization_id            = Column(String(40), primary_key=True)
    workspace_id               = Column(String(40), nullable=False, index=True)

    email_confirmed            = Column(Boolean, nullable=False, default=False)
    first_policy_configured    = Column(Boolean, nullable=False, default=False)
    first_invite_sent          = Column(Boolean, nullable=False, default=False)
    dashboard_reviewed         = Column(Boolean, nullable=False, default=False)
    tools_connected            = Column(Boolean, nullable=False, default=False)
    billing_reviewed           = Column(Boolean, nullable=False, default=False)

    email_confirmed_at            = Column(DateTime(timezone=True), nullable=True)
    first_policy_configured_at    = Column(DateTime(timezone=True), nullable=True)
    first_invite_sent_at          = Column(DateTime(timezone=True), nullable=True)
    dashboard_reviewed_at         = Column(DateTime(timezone=True), nullable=True)
    tools_connected_at            = Column(DateTime(timezone=True), nullable=True)
    billing_reviewed_at           = Column(DateTime(timezone=True), nullable=True)
    created_at                    = Column(DateTime(timezone=True), default=_utcnow)
    updated_at                    = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ──────────────────────────────────────────────────────────────────────────
# IDEMPOTENCY KEYS  (Build Contracts §7.1)
# One row per (scope, idempotency_key). Guards mutating endpoints (signup,
# checkout, …) so a client retry / double-submit replays the original
# response instead of executing the operation twice.
# ──────────────────────────────────────────────────────────────────────────
class CommercialIdempotencyKey(Base):
    __tablename__ = "commercial_idempotency_keys"

    id              = Column(Integer, primary_key=True, index=True)
    scope           = Column(String(40), nullable=False)   # "signup" | "checkout" | ...
    idempotency_key = Column(String(200), nullable=False)
    request_hash    = Column(String(64), nullable=False)   # sha256 of the canonical request body
    status          = Column(String(20), nullable=False, default="in_progress")  # in_progress|completed
    http_status     = Column(Integer, nullable=True)
    response_json   = Column(JSON, nullable=True)
    created_at      = Column(DateTime(timezone=True), default=_utcnow, index=True)
    expires_at      = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )
