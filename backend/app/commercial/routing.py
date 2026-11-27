"""Commercial Routing Engine (§7).

Decides whether a signup proceeds as self_serve_trial, self_serve_paid,
assisted, enterprise, manual_review, or blocked.

Inputs:
  - Selected plan
  - Expected user count
  - Email domain (with government / regulated / disposable / webmail flags)
  - Country / jurisdiction
  - Fraud decision (already computed by fraud.evaluate)
  - SSO / SCIM / data residency requests
  - Intent (trial | paid | assisted | enterprise)

Output:
  - RoutingDecision(route, reasons, recommended_handoff)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List

from app.commercial import constants
from app.commercial.fraud import FraudDecision, ACTION_BLOCK, ACTION_REVIEW


@dataclass
class RoutingDecision:
    route: str                       # ROUTE_* constant
    reasons: List[str] = field(default_factory=list)
    recommended_handoff: Optional[str] = None   # None | "sales" | "support" | "compliance"


def _domain_of(email: str) -> str:
    return email.split("@", 1)[-1].lower().strip() if "@" in email else ""


def _is_government_email(email: str) -> bool:
    domain = _domain_of(email)
    return any(domain.endswith(tld) for tld in constants.GOVERNMENT_TLDS)


def _looks_regulated(company_name: Optional[str], domain: str, keywords: set) -> bool:
    blob = ((company_name or "") + " " + domain).lower()
    return any(k in blob for k in keywords)


def route(
    *,
    intent: str,
    plan: Optional[str],
    expected_users: Optional[int],
    email: str,
    country: Optional[str],
    company_name: Optional[str] = None,
    sso_or_scim_requested: bool = False,
    custom_data_residency: bool = False,
    fraud: Optional[FraudDecision] = None,
    trial_enabled: Optional[bool] = None,
) -> RoutingDecision:
    reasons: List[str] = []

    # 1. Hard blocks — fraud + sanctions take priority.
    if fraud and fraud.action == ACTION_BLOCK:
        return RoutingDecision(
            route=constants.ROUTE_BLOCKED,
            reasons=[fraud.reason or "Blocked by fraud rules"],
        )
    if country and country.upper() in constants.SANCTIONED_COUNTRIES:
        return RoutingDecision(
            route=constants.ROUTE_BLOCKED,
            reasons=[f"Sanctioned jurisdiction: {country}"],
        )
    if fraud and fraud.action == ACTION_REVIEW:
        return RoutingDecision(
            route=constants.ROUTE_MANUAL_REVIEW,
            reasons=[fraud.reason or "Fraud signals require manual review"],
            recommended_handoff="support",
        )

    # 2. Sales-led intents always take the enterprise/assisted lane.
    if (intent or "").lower() == "enterprise":
        return RoutingDecision(
            route=constants.ROUTE_ENTERPRISE,
            reasons=["Enterprise intent declared"],
            recommended_handoff="sales",
        )
    if (intent or "").lower() == "assisted":
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=["Assisted intent declared"],
            recommended_handoff="sales",
        )

    # 3. Government / regulated domains.
    if _is_government_email(email):
        return RoutingDecision(
            route=constants.ROUTE_MANUAL_REVIEW,
            reasons=["Government domain — manual review"],
            recommended_handoff="compliance",
        )
    domain = _domain_of(email)
    if _looks_regulated(company_name, domain, constants.FINANCIAL_KEYWORDS):
        reasons.append("Financial-services signals — assisted route preferred")
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=reasons,
            recommended_handoff="compliance",
        )
    if _looks_regulated(company_name, domain, constants.HEALTHCARE_KEYWORDS):
        reasons.append("Healthcare signals — assisted route preferred")
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=reasons,
            recommended_handoff="compliance",
        )

    # 4. Capability requests.
    if custom_data_residency:
        return RoutingDecision(
            route=constants.ROUTE_ENTERPRISE,
            reasons=["Custom data residency requested"],
            recommended_handoff="sales",
        )
    if sso_or_scim_requested and (plan or "") not in {"governed", "sovereign", "enterprise"}:
        # Push them into a tier that supports it.
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=["SSO/SCIM requested — Governed or higher required"],
            recommended_handoff="sales",
        )

    # 5. Plan-specific routing.
    if (plan or "").lower() == "enterprise":
        return RoutingDecision(
            route=constants.ROUTE_ENTERPRISE,
            reasons=["Enterprise plan selected"],
            recommended_handoff="sales",
        )

    # 6. Headcount sizing.
    users = expected_users or 0
    if users >= 250:
        return RoutingDecision(
            route=constants.ROUTE_ENTERPRISE,
            reasons=[f"Expected users={users} (≥250)"],
            recommended_handoff="sales",
        )
    if users >= 50:
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=[f"Expected users={users} (50–249)"],
            recommended_handoff="sales",
        )

    # 6b. Trial eligibility. The plan catalog has always carried `trial_enabled`
    # (Verified and Governed true, Sovereign and Enterprise false) but nothing
    # read it, so a Sovereign trial could be self-provisioned for free — the
    # headcount rule above never fired because the signup form posts
    # expected_users=1. Sovereign is a Guided Assurance Pilot, so it belongs in
    # the assisted lane that already exists rather than a new refusal path.
    #
    # Deliberately placed AFTER the enterprise and headcount lanes: a 500-seat
    # Sovereign prospect is an enterprise lead, and checking eligibility first
    # would quietly demote them to the generic assisted lane.
    #
    # `trial_enabled` stays a caller-supplied value so this module remains a
    # pure function of its arguments — the catalog lookup lives in
    # billing.service.trial_enabled_for. It is Optional and acts only on an
    # explicit False, so a caller that cannot say (lead capture, which never
    # provisions) leaves routing exactly as it was.
    if trial_enabled is False and (intent or "").lower() == "trial":
        return RoutingDecision(
            route=constants.ROUTE_ASSISTED,
            reasons=[f"{plan or 'This plan'} is not available as a self-serve trial"],
            recommended_handoff="sales",
        )

    # 7. Default self-serve route.
    if (intent or "").lower() == "paid":
        return RoutingDecision(
            route=constants.ROUTE_SELF_SERVE_PAID,
            reasons=reasons + ["Default self-serve paid"],
        )
    return RoutingDecision(
        route=constants.ROUTE_SELF_SERVE_TRIAL,
        reasons=reasons + ["Default self-serve trial"],
    )
