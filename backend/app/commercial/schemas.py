"""Pydantic request/response schemas (§14)."""

from __future__ import annotations

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator, model_validator

from app.commercial import constants


# ──────────────────────────────────────────────────────────────────────────
# §14.1 — POST /api/v1/signup
# ──────────────────────────────────────────────────────────────────────────
class SignupSource(BaseModel):
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    referrer: Optional[str] = None


class SignupClientContext(BaseModel):
    locale: Optional[str] = None
    timezone: Optional[str] = None
    device_fingerprint: Optional[str] = None  # opaque client-provided hash


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: str = Field(..., min_length=1, max_length=120)
    last_name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    # Optional since the Step 1 "Account" screen of the activation flow does not
    # ask for it — the organization is named later, on the Workspace step (the
    # setup wizard's "Organization name" field, which rewrites this value).
    # When absent the server derives a provisional name; it is NEVER left to the
    # client to invent tenant identity. Callers that still send one — the older
    # single-page signup, the Postman collection, existing tests — are unchanged.
    company_name: Optional[str] = Field(None, max_length=255)
    country: Optional[str] = Field(None, min_length=2, max_length=2)

    # Server-controlled vocabularies. These were free-form strings, which let a
    # public request name any plan — including tiers whose entitlement profile
    # grants unlimited seats, SSO, SCIM and legal hold — and be provisioned onto
    # it for free. Validated against the allow-list in
    # app.commercial.constants rather than accepted verbatim.
    plan: Optional[str] = constants.DEFAULT_SELF_SERVE_PLAN
    intent: str = "trial"          # trial | paid | assisted | enterprise
    expected_users: Optional[int] = Field(1, ge=1, le=1_000_000)

    terms_version: str
    privacy_version: str
    marketing_consent: bool = False

    sso_or_scim_requested: bool = False
    custom_data_residency: bool = False

    # Proof-of-humanity token from the bot-challenge widget. Verified
    # server-side before any database write; see app.commercial.challenge.
    challenge_token: Optional[str] = Field(None, max_length=4096)

    source: Optional[SignupSource] = None
    client_context: Optional[SignupClientContext] = None

    # NOTE: there is deliberately no `role`, `organization_id` or
    # `operational_org_id` field. Role is assigned server-side by the
    # provisioning service and tenant identity is derived from the
    # authenticated user — never accepted from a client.

    @field_validator("plan")
    @classmethod
    def _validate_plan(cls, v: Optional[str]) -> str:
        if v is None or not str(v).strip():
            return constants.DEFAULT_SELF_SERVE_PLAN
        plan = str(v).strip().lower()
        if plan not in constants.SELF_SERVE_PLANS:
            raise ValueError(
                "plan must be one of: " + ", ".join(constants.SELF_SERVE_PLANS)
            )
        return plan

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, v: str) -> str:
        intent = (v or "").strip().lower() or "trial"
        if intent not in constants.SELF_SERVE_INTENTS:
            raise ValueError(
                "intent must be one of: " + ", ".join(constants.SELF_SERVE_INTENTS)
            )
        return intent

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        """Apply the platform password policy.

        The same rules the password-reset flow enforces
        (``app.auth.service._validate_password_strength``). Signup previously
        required only 8 characters, so the platform had two different password
        standards depending on how an account came into being.
        """
        from app.auth.service import _validate_password_strength

        problem = _validate_password_strength(v or "")
        if problem:
            raise ValueError(problem)
        return v

    @model_validator(mode="after")
    def _derive_company_name(self) -> "SignupRequest":
        """Fill a provisional organization name when the client sent none.

        Everything downstream (`provisioning`, `routing`, `consent`,
        `OrgSettings.company_name`, the workspace slug) treats `company_name` as
        a required string, so it is resolved here rather than threading an
        Optional through the pipeline. Derived from the first name, which the
        Account step does collect, and replaced the moment the admin names the
        organization on the Workspace step.
        """
        name = (self.company_name or "").strip()
        if not name:
            name = f"{self.first_name.strip()}'s workspace"
        self.company_name = name[:255]
        return self


class SignupSuccess(BaseModel):
    signup_id: str
    user_id: int
    organization_id: str
    # Integer-PK operational tenant the user now belongs to. Never NULL on a
    # successful self-serve signup.
    operational_org_id: Optional[int] = None
    workspace_id: str
    commercial_state: str
    verification_required: bool
    redirect_url: str
    message: str

    # Session issued at signup so the visitor lands authenticated in the setup
    # wizard instead of being bounced to the login page. Absent only if session
    # establishment failed after the tenant was already committed, in which
    # case the client should fall back to the login screen.
    access_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_in: Optional[int] = None
    refresh_token: Optional[str] = None   # desktop clients only
    role: Optional[str] = None
    setup_required: bool = True


class ErrorPayload(BaseModel):
    code: str
    message: str
    field: Optional[str] = None
    recoverable: bool
    recovery_action: Optional[str] = None
    trace_id: str
    timestamp: str


class SignupError(BaseModel):
    error: ErrorPayload


# ──────────────────────────────────────────────────────────────────────────
# §14.4 — POST /api/v1/billing/checkout-session
# ──────────────────────────────────────────────────────────────────────────
class CheckoutRequest(BaseModel):
    organization_id: str
    workspace_id: Optional[str] = None
    plan: str
    billing_cycle: str = "monthly"      # monthly | annual
    seat_count: int = 1
    currency: str = "USD"
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    # Optional server-authoritative quote. When present, plan/cycle/seats/
    # currency and the charged amount come from the quote (§D-05).
    quote_id: Optional[str] = None


class CheckoutResponse(BaseModel):
    checkout_session_id: str
    provider: str
    redirect_url: Optional[str]
    state: str


# ──────────────────────────────────────────────────────────────────────────
# POST /api/v1/billing/quote  (§D-05 server-authoritative price quote)
# ──────────────────────────────────────────────────────────────────────────
class QuoteRequest(BaseModel):
    plan: str
    billing_cycle: str = "monthly"      # monthly | annual
    seat_count: int = 1
    currency: str = "USD"
    organization_id: Optional[str] = None
    workspace_id: Optional[str] = None
    country: Optional[str] = None
    promotion_code: Optional[str] = None


class QuoteLineItem(BaseModel):
    label: str
    cadence: str
    unit_price_cents: int
    effective_monthly_unit_cents: int
    quantity: int
    amount_cents: int


class QuoteResponse(BaseModel):
    quote_id: str
    plan: str
    billing_cycle: str
    seat_count: int
    currency: str
    line_items: List[QuoteLineItem]
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    tax_state: str          # estimated | final | exempt
    total_cents: int
    renewal_amount_cents: int
    renewal_at: Optional[str]
    state: str              # active | consumed | expired
    expires_at: Optional[str]


# ──────────────────────────────────────────────────────────────────────────
# GET /api/v1/billing/quote/preview — the public trial offer
# ──────────────────────────────────────────────────────────────────────────
class SignupOfferResponse(BaseModel):
    """What a visitor is being offered, priced by the server.

    Rendered by the signup page's plan-summary panel. Every number here comes
    from the same places the paid path uses — ``quotes.compute_amounts`` for
    price and discount, ``app.billing.trial`` for the trial clock — so the
    summary a visitor reads at signup cannot drift from what they are later
    charged. Deliberately NOT a second pricing system: it is the existing quote
    math served without persisting a ``PriceQuote`` row, because an
    unauthenticated page view must not write to the database.
    """

    plan: str                       # e.g. "verified"
    plan_label: str                 # e.g. "Temveris Verified"
    currency: str

    # Per-worker list price, and the effective per-worker price once the
    # annual discount is applied. Cents, so no float rounding leaks into money.
    unit_price_monthly_cents: int
    effective_unit_price_monthly_cents: int
    billing_cycle: str              # the preference shown ("annual")
    annual_discount_pct: float

    # Trial terms. `amount_due_today_cents` is what the visitor owes on signup
    # and `card_required` whether a payment method is collected — both server
    # facts, never a frontend assumption.
    # Whether this plan may actually be started as a no-card trial. The catalog
    # has always carried the flag; surfacing it here lets the signup page stop
    # advertising a trial the router will divert to sales, without the frontend
    # having to hold its own copy of the rule.
    trial_available: bool = True
    trial_length_days: int
    trial_ends_at: str              # ISO-8601 UTC
    amount_due_today_cents: int
    card_required: bool

    # Legal document versions the consent record will be stamped with, so the
    # page cannot agree to a version the server is not recording.
    terms_version: str
    privacy_version: str


# ──────────────────────────────────────────────────────────────────────────
# §14.5 — GET /api/v1/entitlements/current
# ──────────────────────────────────────────────────────────────────────────
class EntitlementResponse(BaseModel):
    tenant_id: str
    plan: str
    commercial_state: str
    trial_overlay: bool
    features: Dict[str, bool]
    limits: Dict[str, Any]
    restrictions: Dict[str, Any]
    computed_at: str
    expires_at: str


# ──────────────────────────────────────────────────────────────────────────
# Lead capture (§6.3 / §6.4)
# ──────────────────────────────────────────────────────────────────────────
class LeadRequest(BaseModel):
    intent: str  # assisted | enterprise
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    work_email: EmailStr
    company_name: Optional[str] = None
    country: Optional[str] = None
    expected_users: Optional[int] = None
    industry: Optional[str] = None
    deployment_pref: Optional[str] = None
    integrations: Optional[str] = None
    compliance_needs: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[SignupSource] = None


class LeadResponse(BaseModel):
    lead_id: str
    routing_score: float
    handoff: Optional[str]
    message: str


# ──────────────────────────────────────────────────────────────────────────
# Admin console
# ──────────────────────────────────────────────────────────────────────────
class ManualReviewResolveRequest(BaseModel):
    outcome: str
    notes: Optional[str] = None
