"""Assisted / Enterprise lead capture (§6.3 + §6.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database.database import get_db
from app.commercial import ids, crm
from app.commercial.schemas import LeadRequest, LeadResponse
from app.commercial.routers._helpers import client_meta


router = APIRouter(prefix="/api/v1", tags=["Commercial v1"])


@router.post("/leads", response_model=LeadResponse)
def capture_lead(payload: LeadRequest, request: Request, db: Session = Depends(get_db)):
    correlation_id = ids.correlation_id()
    lead = crm.capture_lead(
        db,
        payload=crm.LeadPayload(
            intent=payload.intent,
            first_name=payload.first_name,
            last_name=payload.last_name,
            work_email=payload.work_email,
            company_name=payload.company_name,
            country=payload.country,
            expected_users=payload.expected_users,
            industry=payload.industry,
            deployment_pref=payload.deployment_pref,
            integrations=payload.integrations,
            compliance_needs=payload.compliance_needs,
            notes=payload.notes,
            utm_source=(payload.source.utm_source if payload.source else None),
            utm_medium=(payload.source.utm_medium if payload.source else None),
            utm_campaign=(payload.source.utm_campaign if payload.source else None),
            referrer=(payload.source.referrer if payload.source else None),
        ),
        correlation_id=correlation_id,
    )
    db.commit()

    handoff = "sales" if payload.intent in {"enterprise", "assisted"} else None
    return LeadResponse(
        lead_id=lead.id,
        routing_score=lead.routing_score,
        handoff=handoff,
        message="Thanks — our team will reach out within one business day.",
    )
