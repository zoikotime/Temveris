"""Consent endpoints — view, grant, withdraw marketing consent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import consent
from app.commercial.routers._helpers import client_meta


router = APIRouter(prefix="/api/v1/consent", tags=["Commercial v1"])


@router.get("/me")
def my_consents(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return consent.list_for_user(db, current_user.id)


@router.post("/marketing/grant")
def grant_marketing(request: Request, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    ip, _ = client_meta(request)
    consent.record(db, consent_type="marketing_consent.granted",
                   granted=True, user_id=current_user.id,
                   ip_address=ip,
                   user_agent=request.headers.get("user-agent", "") if request else None)
    db.commit()
    return {"granted": True}


@router.post("/marketing/withdraw")
def withdraw_marketing(request: Request, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    ip, _ = client_meta(request)
    consent.record(db, consent_type="marketing_consent.withdrawn",
                   granted=False, user_id=current_user.id,
                   ip_address=ip,
                   user_agent=request.headers.get("user-agent", "") if request else None)
    db.commit()
    return {"granted": False}
