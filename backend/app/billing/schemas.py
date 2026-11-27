"""Temveris — Billing Schemas (Pydantic v2)."""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Any, Dict
from datetime import datetime


# ── Plan ──────────────────────────────────────────────────────────────────────
class PlanFeature(BaseModel):
    label: str
    highlighted: Optional[bool] = False


class PlanOut(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    badge: Optional[str] = None
    short_description: Optional[str] = None
    tier_order: int = 0
    monthly_price_per_user: float = 0.0
    annual_price_per_user: float = 0.0
    annual_discount_percentage: float = 20.0
    user_limit: Optional[int] = None
    feature_list_json: List[Dict[str, Any]] = []
    feature_flags_json: Dict[str, Any] = {}
    is_most_popular: bool = False
    is_enterprise: bool = False
    trial_enabled: bool = False
    active_status: bool = True
    currency: str = "USD"

    class Config:
        from_attributes = True


# ── Upgrade / downgrade / enterprise ──────────────────────────────────────────
class UpgradeRequest(BaseModel):
    plan_code: str
    billing_cycle: Optional[str] = "monthly"   # monthly | annual

    # How many seats the customer chose to buy.
    #
    # Optional, and absent means "bill for the people who are actually here" —
    # the original behaviour, which every existing caller keeps. When supplied
    # it is a REQUEST, not an instruction: the server clamps it to at least the
    # billable headcount (you cannot buy fewer seats than you are using) and
    # refuses anything above the plan's ceiling. A subscription configured for
    # a team that has not been invited yet is the normal case for a workspace
    # bought on day one, which is why the field exists at all.
    seats: Optional[int] = Field(None, ge=1, le=100_000)


class DowngradeRequestIn(BaseModel):
    requested_plan_code: str
    reason: str = Field(..., min_length=20)
    preferred_effective_date: Optional[str] = None
    acknowledged_risk: bool = False


class EnterpriseQuoteIn(BaseModel):
    company_name: str
    work_email: EmailStr
    user_count: str
    deployment_preference: str
    required_integrations: Optional[str] = None
    compliance_requirements: Optional[str] = None
    notes: Optional[str] = None


# ── Contacts ──────────────────────────────────────────────────────────────────
class BillingContactIn(BaseModel):
    name: str
    email: EmailStr
    role: Optional[str] = "Billing Admin"
    receives_invoices: bool = True
    receives_payment_alerts: bool = True
    receives_renewal_notices: bool = True
    receives_tax_documents: bool = False
    is_primary: bool = False


class BillingContactPatch(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    receives_invoices: Optional[bool] = None
    receives_payment_alerts: Optional[bool] = None
    receives_renewal_notices: Optional[bool] = None
    receives_tax_documents: Optional[bool] = None
    is_primary: Optional[bool] = None


# ── Tax profile ───────────────────────────────────────────────────────────────
class TaxProfileIn(BaseModel):
    legal_entity_name: Optional[str] = None
    trading_name: Optional[str] = None
    billing_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    tax_id_label: Optional[str] = None
    tax_id_value: Optional[str] = None
    tax_exempt: Optional[bool] = None
    exempt_certificate_url: Optional[str] = None
    invoice_currency: Optional[str] = None


# ── Payment method ────────────────────────────────────────────────────────────
class PaymentMethodIn(BaseModel):
    """RETIRED — no endpoint accepts this any more.

    It described a hand-typed card: a brand chosen from a dropdown, four digits, and an
    expiry date. Nothing Stripe had ever seen, so nothing that could be charged.
    ``POST /payment-method`` now returns 410 Gone and cards are collected by Stripe's
    hosted Setup Session, then mirrored from Stripe.

    Kept as an empty marker rather than deleted so any client still posting this body
    gets the endpoint's explanatory 410 rather than a validation error that looks like
    a field problem.
    """
