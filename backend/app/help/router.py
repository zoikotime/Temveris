"""
Worker Help router.

Surfaces home metadata (Kairos availability + recommendations), guided
ticket creation with explicit routing, ticket conversation, escalation,
diagnostic consent, and lightweight search/article telemetry.

Storage strategy (v1)
---------------------
Dedicated tables (`support_ticket`, `support_ticket_message`,
`support_escalation`, `diagnostic_consent`, `help_search_event`,
`article_acknowledgement_event`) have not yet shipped, so all writes go to
`AuditLog` with discriminating `entity` + `action` values. Reads project the
audit rows back into the wire shape the frontend expects. When the dedicated
tables land, swap the read/write bodies — the wire shape is stable.

Permission model (spec §11, §17)
--------------------------------
- Workers can only see their own tickets. Every endpoint resolves the caller
  via `get_current_user`; there is no admin override here.
- Sensitive tickets (forced for grievance topics, optional for pay/privacy)
  carry an explicit `visibility_set` in the payload. Manager visibility is
  not implied by reporting structure — handlers outside the visibility set
  cannot read sensitive tickets, even via a parallel admin endpoint
  (intentionally out of scope here).
- Escalations always log to compliance audit, with worker_id, ticket_id,
  reason, current handler, and route.

Egress
------
None. Kairos is feature-flagged off by default (Phase 3 not deployed in this
build). When Kairos lands, the `/kairos/ask` endpoint will own that egress
path; for now it returns a static "not deployed" response.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta, timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app.email_service import send_support_ticket_email, send_support_request_received

import logging
logger = logging.getLogger(__name__)


router = APIRouter(prefix="/worker/help", tags=["Worker Help"])


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
TOPIC_ROUTING = {
    "clock_issue": ("Temveris Support",            "1 business day", False, False),
    "schedule":    ("Your direct manager",          "1 business day", False, False),
    "time_off":    ("Your manager / HR",            "1 business day", False, False),
    "pay":         ("Workspace payroll contact",    "2 business days", False, False),
    "privacy":     ("Workspace data contact",       "3 business days", False, False),
    "technical":   ("Temveris Support",            "1 business day", True,  False),
    "account":     ("Temveris Support",            "1 business day", False, False),
    "grievance":   ("Designated grievance contact", "Confidential — workspace SLA",
                                                     False, True),
    "other":       ("General support queue",        "2 business days", False, False),
}

ESCALATION_ELIGIBLE = {"pay", "privacy", "technical", "time_off"}

KAIROS_ENABLED_DEFAULT = False  # Phase 3 not deployed in this build


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _audit(
    db: Session,
    *,
    actor_id: Optional[int],
    target_id: Optional[int],
    entity: str,
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    org_id: Optional[int] = None,
) -> models.AuditLog:
    row = models.AuditLog(
        actor_id=actor_id,
        target_id=target_id,
        organization_id=org_id,
        entity=entity,
        action=action,
        payload=payload or {},
        ip_address=_client_ip(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ticket_id_label(audit_row_id: int) -> str:
    """Human-friendly ticket label, prefixed ZT-."""
    return f"ZT-{audit_row_id}"


def _project_ticket(row: models.AuditLog, *, include_messages: bool = False, db: Session = None) -> Dict[str, Any]:
    """Project a TicketCreated AuditLog row into the API ticket shape."""
    p = row.payload or {}
    out: Dict[str, Any] = {
        "id":                   _ticket_id_label(row.id),
        "raw_id":               row.id,
        "topic":                p.get("topic"),
        "topic_label":          p.get("topic_label"),
        "subject":              p.get("subject"),
        "description":          p.get("description"),
        "status":               p.get("status", "submitted"),
        "sensitive":            bool(p.get("sensitive")),
        "confidentiality_flag": bool(p.get("confidentiality_flag")),
        "routed_to_name":       p.get("routed_to_name"),
        "expected_response_at": p.get("expected_response_at"),
        "created_at":           row.created_at.isoformat() if row.created_at else None,
        "updated_at":           p.get("updated_at"),
        "resolved_at":          p.get("resolved_at"),
        "diagnostic_consent":   bool(p.get("diagnostic_consent")),
    }
    if include_messages and db is not None:
        msg_rows = (
            db.query(models.AuditLog)
            .filter(
                models.AuditLog.entity == "SupportTicketMessage",
                models.AuditLog.target_id == row.id,
            )
            .order_by(models.AuditLog.created_at.asc())
            .all()
        )
        out["messages"] = [
            {
                "id":           m.id,
                "sender_role":  (m.payload or {}).get("sender_role", "worker"),
                "sender_name":  (m.payload or {}).get("sender_name"),
                "message_body": (m.payload or {}).get("message_body"),
                "created_at":   m.created_at.isoformat() if m.created_at else None,
            }
            for m in msg_rows
        ]
    return out


def _ensure_owner(row: Optional[models.AuditLog], current_user: models.User) -> models.AuditLog:
    """Enforce: workers can only access their own tickets."""
    if row is None or row.entity != "SupportTicket":
        raise HTTPException(404, detail="Ticket not found")
    if row.actor_id != current_user.id:
        raise HTTPException(403, detail="You don't have access to this ticket")
    return row


# ─────────────────────────────────────────────────────────────────────────────
# 1. GET /worker/help/home
#    Returns Kairos availability, recommendations (server-driven), and policy
#    metadata. The frontend has its own static catalog + fallback recs, so
#    a sparse/empty response is fine.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/home")
def get_home(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return {
        "kairos_enabled": KAIROS_ENABLED_DEFAULT,
        "recommendations": [],   # frontend falls back to client-side seeding
        "workspace_policy_meta": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. POST /worker/help/search-events
#    Lightweight telemetry — query, result count, optional clicked article.
#    Writes to event store (AuditLog with HelpSearch entity).
# ─────────────────────────────────────────────────────────────────────────────
class SearchEventIn(BaseModel):
    query:                str = Field(..., max_length=512)
    result_count:         int = Field(0, ge=0)
    clicked_article_slug: Optional[str] = Field(None, max_length=128)
    clicked_position:     Optional[int] = Field(None, ge=0)


@router.post("/search-events", status_code=201)
def log_search_event(
    body: SearchEventIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="HelpSearch",
        action="SearchPerformed",
        payload={
            "query":                body.query,
            "result_count":         body.result_count,
            "clicked_article_slug": body.clicked_article_slug,
            "clicked_position":     body.clicked_position,
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"logged": True}


# ─────────────────────────────────────────────────────────────────────────────
# 3. POST /worker/help/article-views
#    Records an article view. If the article is linked to a pending
#    acknowledgement, the audit row also serves as the acknowledgement event.
# ─────────────────────────────────────────────────────────────────────────────
class ArticleViewIn(BaseModel):
    slug:               str = Field(..., max_length=128)
    related_policy_id:  Optional[str] = Field(None, max_length=128)


@router.post("/article-views", status_code=201)
def log_article_view(
    body: ArticleViewIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Routine read → event store only.
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="HelpArticle",
        action="ArticleViewed",
        payload={
            "slug": body.slug,
            "related_policy_id": body.related_policy_id,
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"logged": True}


# ─────────────────────────────────────────────────────────────────────────────
# 4. POST /worker/help/policy-acknowledgements
#    Compliance audit row when a worker acknowledges a policy via Help.
# ─────────────────────────────────────────────────────────────────────────────
class PolicyAckIn(BaseModel):
    policy_id:      str = Field(..., max_length=128)
    policy_version: str = Field(..., max_length=32)
    article_slug:   Optional[str] = Field(None, max_length=128)


@router.post("/policy-acknowledgements", status_code=201)
def acknowledge_policy_from_help(
    body: PolicyAckIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="Policy",
        action="PolicyAcknowledgedFromHelp",
        payload={
            "policy_id":      body.policy_id,
            "policy_version": body.policy_version,
            "article_slug":   body.article_slug,
            "method":         "help_article_link",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"acknowledged": True, "policy_id": body.policy_id, "policy_version": body.policy_version, "id": row.id}


# ─────────────────────────────────────────────────────────────────────────────
# 5. GET /worker/help/tickets   POST /worker/help/tickets
# ─────────────────────────────────────────────────────────────────────────────
class TicketIn(BaseModel):
    topic:                str = Field(..., max_length=64)
    topic_label:          Optional[str] = Field(None, max_length=128)
    subject:              str = Field(..., min_length=1, max_length=200)
    description:          str = Field(..., min_length=50, max_length=8000)
    when_str:             Optional[str] = Field(None, max_length=200)
    sensitive:            bool = False
    confidentiality_flag: bool = False
    diagnostic_consent:   bool = False


@router.get("/tickets")
def list_my_tickets(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "SupportTicket",
            models.AuditLog.action == "TicketCreated",
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(200)
        .all()
    )
    return [_project_ticket(r) for r in rows]


@router.post("/tickets", status_code=201)
def create_ticket(
    body: TicketIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if body.topic not in TOPIC_ROUTING:
        raise HTTPException(400, detail="invalid topic")
    routed_to_name, sla, needs_diag, force_sensitive = TOPIC_ROUTING[body.topic]

    if needs_diag and not body.diagnostic_consent:
        raise HTTPException(400, detail="Diagnostic consent is required for this topic.")

    is_sensitive = bool(force_sensitive or body.sensitive)

    # Build the explicit visibility set. Keep it conservative: worker + the
    # routed handler. Real grievance/data-contact resolution happens once we
    # have the workspace contacts table; for now the frontend treats this as
    # a marker and the access controller in this router enforces ownership.
    visibility_set = {"worker_id": current_user.id, "handler_label": routed_to_name}

    # Record diagnostic consent separately so it's auditable on its own.
    if body.diagnostic_consent:
        _audit(
            db,
            actor_id=current_user.id,
            target_id=current_user.id,
            entity="DiagnosticConsent",
            action="ConsentGiven",
            payload={
                "topic": body.topic,
                "scope": ["device_info", "app_diagnostics"],
            },
            request=request,
            org_id=current_user.organization_id,
        )

    expected_at = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()

    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="SupportTicket",
        action="TicketCreated",
        org_id=current_user.organization_id,
        payload={
            "topic":                body.topic,
            "topic_label":          body.topic_label,
            "subject":              body.subject,
            "description":          body.description,
            "when_str":             body.when_str,
            "sensitive":            is_sensitive,
            "confidentiality_flag": body.confidentiality_flag,
            "diagnostic_consent":   body.diagnostic_consent,
            "routed_to_name":       routed_to_name,
            "sla":                  sla,
            "expected_response_at": expected_at,
            "status":               "submitted",
            "visibility_set":       visibility_set,
        },
        request=request,
    )

    # ZT-SUP-01 requester acknowledgment (Essential; non-suppressible). Runs as a
    # background task so a mail failure can never block ticket creation. Carries
    # only reference / subject / priority(SLA) / status — never the ticket body.
    background_tasks.add_task(
        send_support_request_received,
        current_user.email, current_user.name,
        _ticket_id_label(row.id), body.subject, sla, "submitted",
    )

    return _project_ticket(row)


@router.get("/tickets/{ticket_label}")
def get_ticket_detail(
    ticket_label: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    raw_id = _parse_ticket_label(ticket_label)
    row = db.query(models.AuditLog).filter(models.AuditLog.id == raw_id).first()
    row = _ensure_owner(row, current_user)
    return _project_ticket(row, include_messages=True, db=db)


def _parse_ticket_label(label: str) -> int:
    """Accept either 'ZT-123' or '123'. Reject anything else."""
    if not label:
        raise HTTPException(400, detail="Invalid ticket id")
    s = label.strip()
    if s.upper().startswith("ZT-"):
        s = s[3:]
    try:
        return int(s)
    except ValueError:
        raise HTTPException(400, detail="Invalid ticket id")


# ─────────────────────────────────────────────────────────────────────────────
# 6. POST /worker/help/tickets/{id}/messages
# ─────────────────────────────────────────────────────────────────────────────
class MessageIn(BaseModel):
    message_body: str = Field(..., min_length=1, max_length=8000)
    reopen:       bool = False


@router.post("/tickets/{ticket_label}/messages", status_code=201)
def add_ticket_message(
    ticket_label: str,
    body: MessageIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    raw_id = _parse_ticket_label(ticket_label)
    ticket = db.query(models.AuditLog).filter(models.AuditLog.id == raw_id).first()
    ticket = _ensure_owner(ticket, current_user)

    # Reopen-window check: 14 days from resolved_at.
    if body.reopen:
        resolved_at = (ticket.payload or {}).get("resolved_at")
        if resolved_at:
            try:
                rdt = datetime.fromisoformat(resolved_at)
                if datetime.now(timezone.utc) - rdt > timedelta(days=14):
                    raise HTTPException(400, detail="Reopen window has closed (14 days).")
            except ValueError:
                pass
        # Bump status back to submitted.
        new_payload = dict(ticket.payload or {})
        new_payload["status"] = "submitted"
        new_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        ticket.payload = new_payload
        db.add(ticket)
        db.commit()

    msg = _audit(
        db,
        actor_id=current_user.id,
        target_id=ticket.id,    # link to ticket row
        entity="SupportTicketMessage",
        action="MessageAdded",
        payload={
            "sender_role":  "worker",
            "sender_name":  current_user.name or current_user.email,
            "message_body": body.message_body,
            "reopen":       body.reopen,
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {
        "id":           msg.id,
        "sender_role":  "worker",
        "message_body": body.message_body,
        "created_at":   msg.created_at.isoformat() if msg.created_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. POST /worker/help/tickets/{id}/escalate
# ─────────────────────────────────────────────────────────────────────────────
class EscalateIn(BaseModel):
    reason: str = Field(..., min_length=30, max_length=4000)


@router.post("/tickets/{ticket_label}/escalate", status_code=201)
def escalate_ticket(
    ticket_label: str,
    body: EscalateIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    raw_id = _parse_ticket_label(ticket_label)
    ticket = db.query(models.AuditLog).filter(models.AuditLog.id == raw_id).first()
    ticket = _ensure_owner(ticket, current_user)

    topic = (ticket.payload or {}).get("topic")
    if topic not in ESCALATION_ELIGIBLE:
        raise HTTPException(400, detail="This ticket type does not support escalation.")

    # Bump ticket status to escalated.
    new_payload = dict(ticket.payload or {})
    new_payload["status"] = "escalated"
    new_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    ticket.payload = new_payload
    db.add(ticket)
    db.commit()

    esc = _audit(
        db,
        actor_id=current_user.id,
        target_id=ticket.id,
        entity="SupportEscalation",
        action="TicketEscalated",
        payload={
            "ticket_id":          _ticket_id_label(ticket.id),
            "topic":              topic,
            "reason":             body.reason,
            "current_handler":    new_payload.get("routed_to_name"),
            "escalation_route":   "workspace_grievance_or_dpo",  # workspace-policy resolved later
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {
        "id":           esc.id,
        "ticket_id":    _ticket_id_label(ticket.id),
        "status":       "escalated",
        "submitted_at": esc.created_at.isoformat() if esc.created_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. POST /worker/help/diagnostic-consent
#    Also captured inline on ticket creation; this endpoint exists for cases
#    where consent is captured separately (e.g. mid-conversation upgrade).
# ─────────────────────────────────────────────────────────────────────────────
class DiagnosticConsentIn(BaseModel):
    ticket_id: Optional[str]      = None
    consent:   bool               = True
    scope:     Optional[List[str]] = None


@router.post("/diagnostic-consent", status_code=201)
def diagnostic_consent(
    body: DiagnosticConsentIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    target_id: Optional[int] = current_user.id
    if body.ticket_id:
        try:
            target_id = _parse_ticket_label(body.ticket_id)
            row = db.query(models.AuditLog).filter(models.AuditLog.id == target_id).first()
            _ensure_owner(row, current_user)
        except HTTPException:
            raise
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=target_id,
        entity="DiagnosticConsent",
        action="ConsentGiven" if body.consent else "ConsentWithdrawn",
        payload={
            "scope":     body.scope or ["device_info", "app_diagnostics"],
            "ticket_id": body.ticket_id,
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"id": row.id, "consent": body.consent}


# ─────────────────────────────────────────────────────────────────────────────
# 8b. POST /worker/help/contact-support
#     Lightweight "Need Help?" form from the Electron tracker. Forwards a
#     name / work_email / issue to support@temveris.example via the email
#     service. Also writes an AuditLog row so we have a record on the
#     server even if the email transport blips.
# ─────────────────────────────────────────────────────────────────────────────
class ContactSupportIn(BaseModel):
    name:       str = Field(..., min_length=1, max_length=200)
    work_email: str = Field(..., min_length=3, max_length=320)
    issue:      str = Field(..., min_length=10, max_length=8000)
    source:     Optional[str] = Field(None, max_length=64)


def _send_support_in_background(name: str, work_email: str, issue: str, source: str) -> None:
    """Wrapper for BackgroundTasks — swallows exceptions so a bad transport
    can never crash a background worker. The audit row written by the
    endpoint is the durable record; this is best-effort delivery."""
    try:
        ok = send_support_ticket_email(
            name=name,
            work_email=work_email,
            issue=issue,
            source=source,
        )
        if not ok:
            logger.warning(
                "[contact-support] background send returned False "
                "for %s — see email_logs for transport detail",
                work_email,
            )
    except Exception:
        logger.exception("[contact-support] background send raised")


@router.post("/contact-support", status_code=201)
def contact_support(
    body: ContactSupportIn,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Worker submits the in-app "Need Help?" form.

    The actual email send (Resend → SMTP fallback, up to ~60s in the worst
    case) is pushed to a BackgroundTask so the client gets an instant 201.
    The audit row is the source of truth — even if both transports fail
    later, ops can recover the request from `SupportContact` audit rows.
    """
    source = (body.source or "desktop-tracker")[:64]
    name   = body.name.strip()
    email  = body.work_email.strip()
    issue  = body.issue.strip()

    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="SupportContact",
        action="ContactSubmitted",
        payload={
            "name":       name,
            "work_email": email,
            "issue":      issue,
            "source":     source,
        },
        request=request,
    )

    # Schedule the send after the response is sent. FastAPI runs background
    # tasks on a thread pool, so a 15-60s Resend/SMTP call never blocks the
    # next request.
    background_tasks.add_task(
        _send_support_in_background,
        name, email, issue, source,
    )

    return {
        "submitted":  True,
        "email_sent": "pending",   # delivered out-of-band; ops can confirm via logs
        "id":         row.id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9. POST /worker/help/kairos/ask  (placeholder — Phase 3)
# ─────────────────────────────────────────────────────────────────────────────
class KairosAskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


@router.post("/kairos/ask")
def kairos_ask(
    body: KairosAskIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Placeholder — Kairos Phase 3 not deployed in this build.
    The frontend feature-flags this panel off via /worker/help/home, so this
    endpoint is unreachable from the standard flow. If somebody calls it
    directly, return a clean unavailable response (not a 500).
    """
    return {
        "is_ai":  True,
        "answer": "Kairos isn't available in this workspace yet. Browse Help articles or contact support.",
        "sources": [],
        "escalation": None,
    }
