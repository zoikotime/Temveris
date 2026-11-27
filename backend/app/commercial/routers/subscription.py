"""Self-serve subscription lifecycle endpoints (Enterprise Billing §1).

    POST /api/v1/subscription/cancel      {organization_id, mode}
    POST /api/v1/subscription/reactivate  {organization_id}

Auth: the org owner (or super_admin). Idempotent via the Idempotency-Key header
(authorization runs BEFORE the idempotency claim so a replay can't bypass it).

NOTE: no ``from __future__ import annotations`` here on purpose — real annotation
objects let FastAPI resolve the route signature without the wrapper-globals issue.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import subscription_ops, idempotency, ids
from app.commercial.models import CommercialOrganization
from app.commercial.routers._helpers import error_payload
from app.commercial.enforcement import shadow_capability as _shadow

_log = logging.getLogger("zoiko.commercial.routers.subscription")
router = APIRouter(prefix="/api/v1/subscription", tags=["Commercial v1"])


class CancelRequest(BaseModel):
    organization_id: str
    mode: str = "at_renewal"   # at_renewal | immediate


class ReactivateRequest(BaseModel):
    organization_id: str


class SeatChangeRequest(BaseModel):
    organization_id: str
    seat_count: int = Field(..., ge=1)


def _authorize(db: Session, user: User, organization_id: str) -> None:
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    if org is None:
        raise HTTPException(404, "Organization not found")
    if user.role != "super_admin" and org.owner_user_id != user.id:
        raise HTTPException(403, "You do not manage this subscription")


def _op_error(e: subscription_ops.SubscriptionOpError) -> JSONResponse:
    status = 400 if e.code == "VALIDATION_FAILED" else 409
    # over-allocation is a recoverable conflict (free up seats and retry);
    # an invalid state transition is not.
    recoverable = e.code == "SEAT_OVER_ALLOCATION"
    return JSONResponse(status_code=status, content=error_payload(
        code=e.code, message=e.message, recoverable=recoverable))


@router.post("/cancel")
def cancel(
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _authorize(db, current_user, payload.organization_id)   # auth before idempotency
    if payload.mode not in ("at_renewal", "immediate"):
        return JSONResponse(status_code=400, content=error_payload(
            code="VALIDATION_FAILED", message="mode must be 'at_renewal' or 'immediate'.",
            field="mode"))

    def _do():
        try:
            return subscription_ops.cancel(
                db, organization_id=payload.organization_id, mode=payload.mode,
                correlation_id=ids.correlation_id())
        except subscription_ops.SubscriptionOpError as e:
            return _op_error(e)

    return idempotency.run(db, scope="subscription_cancel", request=request, payload=payload, fn=_do)


@router.post("/reactivate")
def reactivate(
    payload: ReactivateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # SHADOW ONLY - observes and returns None; cannot raise, cannot block.
    # billing.portal is essential, and reactivation is the recovery action an
    # impaired tenant needs, so this hook should never report a block for a
    # tenant that is merely impaired. It lives here rather than on
    # POST /billing/customer-portal because app/billing may not import
    # app/commercial - the layering guard in test_billing_phase5_cleanup.
    _shadow(db, current_user, "billing.portal",
            route="POST /commercial/subscription/reactivate")
    _authorize(db, current_user, payload.organization_id)

    def _do():
        try:
            return subscription_ops.undo_cancel(
                db, organization_id=payload.organization_id,
                correlation_id=ids.correlation_id())
        except subscription_ops.SubscriptionOpError as e:
            return _op_error(e)

    return idempotency.run(db, scope="subscription_reactivate", request=request, payload=payload, fn=_do)


@router.post("/seats")
def change_seats(
    payload: SeatChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _authorize(db, current_user, payload.organization_id)

    def _do():
        try:
            return subscription_ops.change_seats(
                db, organization_id=payload.organization_id,
                seat_count=payload.seat_count, correlation_id=ids.correlation_id())
        except subscription_ops.SubscriptionOpError as e:
            return _op_error(e)

    return idempotency.run(db, scope="subscription_seats", request=request, payload=payload, fn=_do)
