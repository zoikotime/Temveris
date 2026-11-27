"""CRM Integration Service (§5 — required service #10).

Provider interface for assisted / enterprise lead capture. The default
provider is a database-only stub so the lead-capture endpoints still
work without external connectivity. Real providers (HubSpot, Salesforce,
Pipedrive) plug in by setting CRM_PROVIDER in the environment and
implementing the CRMProvider interface.

Lead scoring uses the same Commercial Routing Engine so a single signal
graph drives both signup routing and CRM lead routing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.commercial import ids, routing, constants
from app.commercial.audit import emit_event
from app.commercial.models import CrmLead

_log = logging.getLogger("zoiko.commercial.crm")


# ──────────────────────────────────────────────────────────────────────────
# DOMAIN
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class LeadPayload:
    intent: str  # assisted | enterprise
    first_name: Optional[str]
    last_name: Optional[str]
    work_email: str
    company_name: Optional[str]
    country: Optional[str]
    expected_users: Optional[int]
    industry: Optional[str] = None
    deployment_pref: Optional[str] = None
    integrations: Optional[str] = None
    compliance_needs: Optional[str] = None
    notes: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    referrer: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────
# PROVIDERS
# ──────────────────────────────────────────────────────────────────────────
class CRMProvider:
    name = "base"

    def push_lead(self, lead: CrmLead) -> Optional[str]:
        """Send the lead to the external CRM. Return external_id or None."""
        raise NotImplementedError


class StubCRMProvider(CRMProvider):
    """DB-only provider. Records the lead, returns no external id.
    Production replaces this with HubSpot/Salesforce/Pipedrive."""
    name = "stub"

    def push_lead(self, lead: CrmLead) -> Optional[str]:
        _log.info("StubCRM: captured lead %s (%s, %s)", lead.id, lead.work_email, lead.intent)
        return None


def _provider() -> CRMProvider:
    name = (os.getenv("CRM_PROVIDER") or "stub").lower()
    if name == "stub":
        return StubCRMProvider()
    # Future: hubspot / salesforce / pipedrive.
    _log.warning("Unknown CRM_PROVIDER=%r — falling back to stub", name)
    return StubCRMProvider()


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────
def capture_lead(
    db: Session,
    *,
    payload: LeadPayload,
    correlation_id: Optional[str] = None,
) -> CrmLead:
    decision = routing.route(
        intent=payload.intent,
        plan="enterprise" if payload.intent == "enterprise" else None,
        expected_users=payload.expected_users,
        email=payload.work_email,
        country=payload.country,
        company_name=payload.company_name,
    )

    lead = CrmLead(
        id=ids.lead_id(),
        intent=payload.intent,
        first_name=payload.first_name,
        last_name=payload.last_name,
        work_email=payload.work_email.strip().lower(),
        company_name=payload.company_name,
        country=payload.country,
        expected_users=payload.expected_users,
        industry=payload.industry,
        deployment_pref=payload.deployment_pref,
        integrations=payload.integrations,
        compliance_needs=payload.compliance_needs,
        notes=payload.notes,
        routing_score=_score_from_routing(decision),
        status="new",
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        utm_campaign=payload.utm_campaign,
        referrer=payload.referrer,
    )
    db.add(lead)
    db.flush()

    # Optional external push.
    ext = _provider().push_lead(lead)
    if ext:
        lead.crm_external_id = ext
        db.flush()

    emit_event(
        db,
        event_type="crm.lead.created",
        source_service="crm-integration",
        payload={
            "lead_id": lead.id, "intent": payload.intent,
            "route": decision.route, "handoff": decision.recommended_handoff,
            "reasons": decision.reasons,
        },
        correlation_id=correlation_id,
    )
    return lead


def _score_from_routing(decision) -> float:
    """Convert a RoutingDecision into a 0–100 lead score that ops can sort by."""
    if decision.route == constants.ROUTE_ENTERPRISE:
        return 90.0
    if decision.route == constants.ROUTE_ASSISTED:
        return 60.0
    if decision.route == constants.ROUTE_MANUAL_REVIEW:
        return 50.0
    if decision.route == constants.ROUTE_BLOCKED:
        return 0.0
    return 30.0
