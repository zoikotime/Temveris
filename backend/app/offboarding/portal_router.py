"""Former-worker portal API (§1 — governance spec).

Mounted under ``/api/offboarding/former-worker``. EVERY endpoint here uses the
isolated ``get_portal_principal`` dependency — never ``get_current_user``. The
two auth universes never cross: a portal bearer cannot reach an admin route
(it is not a JWT), and an admin JWT cannot satisfy this gate (it is not a
portal session). That is the §1 "no inheritance from active-worker
permissions" rule, enforced at the dependency layer rather than by UI hiding.

Endpoints (no operational/admin/analytics surface — by design):

  POST /auth/login                — exchange portal token for a pending-MFA session
  POST /auth/mfa/verify           — present OTP, flip session to ACTIVE
  POST /auth/logout
  POST /auth/mfa/enroll           — first-run: register alternate email + send OTP
  POST /auth/mfa/enroll/verify    — first-run: confirm OTP, return one-time recovery code

  GET  /me                        — info + scopes + expiry visibility (the §1 "expiry visibility")
  GET  /documents                 — list approved documents WITHIN granted scopes
  POST /documents/download        — download with throttle + audit
  POST /support-requests          — open a ticket (allowed §1 surface)
  POST /data-rights-requests      — file a GDPR-style DSAR / erasure / portability request

A former worker can never reach ``/dashboards``, ``/analytics``, ``/crm``,
``/billing``, ``/admin/...``, ``/tracking/...``, or
``/audit/...`` from this router — those routes simply do not exist on it, and
no other router in the app mounts ``get_portal_principal``.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db

from . import ids
from .constants import (
    ALLOWED_FORMER_WORKER_SCOPES, DATA_RIGHTS_SLA_DAYS,
    FORBIDDEN_FORMER_WORKER_SCOPES, DataRightsRequestStatus,
    DataRightsRequestType, PortalAction, SupportTicketStatus,
)
from .models import (
    DataRightsRequest, FormerWorkerAccess, FormerWorkerMFA,
    FormerWorkerPortalSession, FormerWorkerSupportTicket, PortalAccessLog,
)
from .portal_auth import (
    PortalAuthError, PortalPrincipal, _client_ip, begin_login,
    begin_mfa_enrollment, check_download_throttle, get_portal_principal,
    log_action, logout as auth_logout, verify_mfa, verify_mfa_enrollment,
)

_log = logging.getLogger("zoiko.offboarding.portal")

router = APIRouter(prefix="/api/offboarding/former-worker", tags=["Former Worker Portal"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ════════════════════════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    portal_token: str = Field(min_length=10, max_length=120)


class MFAVerifyRequest(BaseModel):
    session_token: str = Field(min_length=10, max_length=120)
    otp: str = Field(min_length=4, max_length=12)


class MFAEnrollRequest(BaseModel):
    portal_token: str = Field(min_length=10, max_length=120)
    alternate_email: str = Field(min_length=3, max_length=255)


class MFAEnrollVerifyRequest(BaseModel):
    portal_token: str = Field(min_length=10, max_length=120)
    otp: str = Field(min_length=4, max_length=12)


class DownloadRequest(BaseModel):
    scope: str = Field(min_length=1, max_length=60)
    document_ref: str = Field(min_length=1, max_length=120)


class SupportRequestCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=255)
    body: str = Field(min_length=3, max_length=4000)
    category: Optional[str] = Field(default=None, max_length=40)


class DataRightsCreate(BaseModel):
    request_type: str
    details: Optional[str] = Field(default=None, max_length=4000)


# ════════════════════════════════════════════════════════════════════════════
# AUTH
# ════════════════════════════════════════════════════════════════════════════
@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    raw, session = begin_login(db, portal_token=payload.portal_token, request=request)
    db.commit()
    return {
        "session_token": raw,
        "session_id": session.id,
        "expires_at": session.expires_at,
        "status": session.status,
        "mfa_required": True,
        "otp_expires_at": session.otp_expires_at,
    }


@router.post("/auth/mfa/verify")
def mfa_verify(payload: MFAVerifyRequest, request: Request, db: Session = Depends(get_db)):
    session = verify_mfa(db, session_token=payload.session_token, otp=payload.otp,
                        request=request)
    db.commit()
    return {
        "session_id": session.id, "status": session.status,
        "expires_at": session.expires_at, "mfa_passed": True,
    }


@router.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Best-effort logout. We accept the bearer token from the Authorization
    header (same place as the active session) but never raise: an already-
    invalid token still gets a 204 so the client is free to discard state."""
    auth = (request.headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        raw = auth.split(" ", 1)[1].strip()
        try:
            auth_logout(db, session_token=raw, request=request)
            db.commit()
        except Exception:
            db.rollback()
    return {"ok": True}


@router.post("/auth/mfa/enroll")
def mfa_enroll(payload: MFAEnrollRequest, request: Request, db: Session = Depends(get_db)):
    """First-run MFA enrollment. We accept the portal token here too (the
    worker has no session yet) so we can authenticate the enrollment without
    requiring the half-baked PENDING_MFA bearer."""
    token_hash = hashlib.sha256(payload.portal_token.encode("utf-8")).hexdigest()
    fwa = (db.query(FormerWorkerAccess)
           .filter(FormerWorkerAccess.portal_token_hash == token_hash)
           .first())
    if fwa is None:
        raise PortalAuthError(status_code=401, detail="invalid_token")
    begin_mfa_enrollment(db, fwa=fwa, alternate_email=payload.alternate_email)
    db.commit()
    return {"ok": True, "status": "pending_verify"}


@router.post("/auth/mfa/enroll/verify")
def mfa_enroll_verify(payload: MFAEnrollVerifyRequest, request: Request,
                      db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(payload.portal_token.encode("utf-8")).hexdigest()
    fwa = (db.query(FormerWorkerAccess)
           .filter(FormerWorkerAccess.portal_token_hash == token_hash)
           .first())
    if fwa is None:
        raise PortalAuthError(status_code=401, detail="invalid_token")
    _row, recovery_code = verify_mfa_enrollment(db, fwa=fwa, otp=payload.otp)
    db.commit()
    # Surfaced ONCE. The portal UI shows it; we never persist plaintext.
    return {"ok": True, "status": "enrolled", "recovery_code": recovery_code}


# ════════════════════════════════════════════════════════════════════════════
# PROFILE  ("expiry visibility" + scopes)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/me")
def me(principal: PortalPrincipal = Depends(get_portal_principal),
       db: Session = Depends(get_db)):
    fwa = principal.fwa
    mfa = (db.query(FormerWorkerMFA)
           .filter(FormerWorkerMFA.fwa_id == fwa.id).first())
    return {
        "fwa_id": fwa.id, "case_id": fwa.case_id, "worker_id": fwa.worker_id,
        "tenant_id": fwa.tenant_id, "status": fwa.status,
        "scopes": fwa.scopes, "granted_at": fwa.granted_at,
        "expires_at": fwa.expires_at,
        "days_remaining": _days_remaining(fwa),
        "session": {
            "id": principal.session.id, "status": principal.session.status,
            "expires_at": principal.session.expires_at,
            "issued_at": principal.session.issued_at,
        },
        "mfa": {
            "method": mfa.method if mfa else None,
            "status": mfa.status if mfa else "not_enrolled",
            "alternate_email_verified": (mfa.alternate_email_verified if mfa else False),
            "last_verified_at": mfa.last_verified_at if mfa else None,
        },
    }


def _days_remaining(fwa: FormerWorkerAccess) -> Optional[int]:
    if fwa.expires_at is None:
        return None
    exp = fwa.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return max(0, int((exp - _now()).total_seconds() // 86400))


# ════════════════════════════════════════════════════════════════════════════
# DOCUMENTS  (scope-restricted)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/documents")
def list_documents(principal: PortalPrincipal = Depends(get_portal_principal),
                   db: Session = Depends(get_db),
                   request: Request = None):
    fwa = principal.fwa
    ip = _client_ip(request)
    log_action(db, fwa=fwa, action=PortalAction.LIST_DOCUMENTS.value, outcome="ok",
               session=principal.session, ip=ip,
               detail={"scopes": list(fwa.scopes or [])})
    db.commit()
    return {
        "scopes": list(fwa.scopes or []),
        "documents": [
            {"scope": s, "documents": _stub_documents_for_scope(s)}
            for s in (fwa.scopes or [])
            if s in ALLOWED_FORMER_WORKER_SCOPES
        ],
    }


def _stub_documents_for_scope(scope: str) -> list:
    """Document storage is outside the offboarding module — this returns the
    resolved scope contract the portal renders against. A real integration
    swaps this for a query into the existing payslip / timesheet store."""
    return [{"ref": f"{scope}:latest", "available": True}]


@router.post("/documents/download")
def download_document(payload: DownloadRequest, request: Request,
                      principal: PortalPrincipal = Depends(get_portal_principal),
                      db: Session = Depends(get_db)):
    fwa = principal.fwa
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")

    # Document scope engine (§3) — blocklist + allowlist, both enforced.
    if payload.scope in FORBIDDEN_FORMER_WORKER_SCOPES:
        log_action(db, fwa=fwa, action=PortalAction.FORBIDDEN_SCOPE_ATTEMPT.value,
                   outcome="denied", session=principal.session,
                   scope=payload.scope, document_ref=payload.document_ref,
                   detail={"reason": "blocklisted_scope"}, ip=ip, user_agent=ua)
        db.commit()
        raise HTTPException(status_code=403, detail="forbidden_scope")
    if payload.scope not in ALLOWED_FORMER_WORKER_SCOPES:
        log_action(db, fwa=fwa, action=PortalAction.FORBIDDEN_SCOPE_ATTEMPT.value,
                   outcome="denied", session=principal.session,
                   scope=payload.scope, document_ref=payload.document_ref,
                   detail={"reason": "not_in_allowlist"}, ip=ip, user_agent=ua)
        db.commit()
        raise HTTPException(status_code=403, detail="scope_not_allowed")
    if payload.scope not in (fwa.scopes or []):
        log_action(db, fwa=fwa, action=PortalAction.FORBIDDEN_SCOPE_ATTEMPT.value,
                   outcome="denied", session=principal.session,
                   scope=payload.scope, document_ref=payload.document_ref,
                   detail={"reason": "scope_not_granted"}, ip=ip, user_agent=ua)
        db.commit()
        raise HTTPException(status_code=403, detail="scope_not_granted_to_worker")

    check_download_throttle(db, fwa, session=principal.session, ip=ip)

    log_action(db, fwa=fwa, action=PortalAction.DOWNLOAD_DOCUMENT.value, outcome="ok",
               session=principal.session, scope=payload.scope,
               document_ref=payload.document_ref, ip=ip, user_agent=ua)
    db.commit()
    # The actual file-serving integration is out of scope for this module;
    # we return a signed-URL stub the storage backend would mint.
    return {
        "scope": payload.scope, "document_ref": payload.document_ref,
        "download_url": f"/static/former-worker/{fwa.id}/{payload.scope}/{payload.document_ref}",
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=5)).isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# SUPPORT REQUESTS  (allowed §1 surface)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/support-requests")
def open_support_request(payload: SupportRequestCreate, request: Request,
                         principal: PortalPrincipal = Depends(get_portal_principal),
                         db: Session = Depends(get_db)):
    fwa = principal.fwa
    ticket = FormerWorkerSupportTicket(
        id=ids.support_ticket_id(), tenant_id=fwa.tenant_id, fwa_id=fwa.id,
        worker_id=fwa.worker_id, subject=payload.subject, body=payload.body,
        category=payload.category, status=SupportTicketStatus.OPEN.value,
    )
    db.add(ticket)
    db.flush()
    log_action(db, fwa=fwa, action=PortalAction.SUPPORT_REQUEST.value, outcome="ok",
               session=principal.session,
               detail={"ticket_id": ticket.id, "subject": payload.subject[:80]},
               ip=_client_ip(request))
    db.commit()
    return {"id": ticket.id, "status": ticket.status, "created_at": ticket.created_at}


# ════════════════════════════════════════════════════════════════════════════
# DATA-RIGHTS REQUESTS  (GDPR access / erasure / portability)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/data-rights-requests")
def open_dsar(payload: DataRightsCreate, request: Request,
              principal: PortalPrincipal = Depends(get_portal_principal),
              db: Session = Depends(get_db)):
    if payload.request_type not in {t.value for t in DataRightsRequestType}:
        raise HTTPException(status_code=422,
                            detail=f"invalid request_type '{payload.request_type}'")
    fwa = principal.fwa
    req = DataRightsRequest(
        id=ids.dsar_id(), tenant_id=fwa.tenant_id, fwa_id=fwa.id,
        worker_id=fwa.worker_id, case_id=fwa.case_id,
        request_type=payload.request_type,
        status=DataRightsRequestStatus.OPEN.value,
        details=payload.details,
        sla_due_at=_now() + timedelta(days=DATA_RIGHTS_SLA_DAYS),
    )
    db.add(req)
    db.flush()
    log_action(db, fwa=fwa, action=PortalAction.DSAR_REQUEST.value, outcome="ok",
               session=principal.session,
               detail={"id": req.id, "request_type": payload.request_type},
               ip=_client_ip(request))
    db.commit()
    return {"id": req.id, "request_type": req.request_type,
            "status": req.status, "sla_due_at": req.sla_due_at}


# ════════════════════════════════════════════════════════════════════════════
# ACCESS LOG  (the §1 "expiry visibility" view — worker's own log only)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/access-log")
def my_access_log(principal: PortalPrincipal = Depends(get_portal_principal),
                  db: Session = Depends(get_db),
                  limit: int = 50):
    rows = (db.query(PortalAccessLog)
            .filter(PortalAccessLog.fwa_id == principal.fwa.id)
            .order_by(PortalAccessLog.occurred_at.desc())
            .limit(max(1, min(limit, 200))).all())
    return [{
        "action": r.action, "outcome": r.outcome, "scope": r.scope,
        "document_ref": r.document_ref, "ip_address": r.ip_address,
        "occurred_at": r.occurred_at,
    } for r in rows]
