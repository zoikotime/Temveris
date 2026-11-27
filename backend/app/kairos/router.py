"""
Kairos HTTP API. Mounted at `/api/v1/kairos` from `backend/main.py`.

Auth strategy: every endpoint reuses the main Temveris `get_current_user`
dependency — the legacy two-token bootstrap (POST /auth/verify → mint Kairos
JWT) was removed when the chatbot moved in-process.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.service import get_current_user
from app.kairos import mail as mail_pipeline
from app.kairos import service as chat_service
from app.kairos.database import SessionLocal, get_kairos_db as get_db
from app.kairos.knowledge import chat_context
from app.kairos.schemas import (
    ChatContextOut,
    ChatMessageIn,
    ChatMessageOut,
    CreateSessionOut,
    MailSendIn,
    MailStatusOut,
)
from database.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kairos", tags=["Kairos"])


async def _track_fallback_in_background(message: str) -> None:
    """Runs after the response is sent so the user-facing reply isn't
    blocked by the external translation calls in `normalize_prompt`."""
    db = SessionLocal()
    try:
        await chat_service.track_unknown_prompt(db, message)
    except Exception:
        logger.warning("Background unknown-prompt tracking failed", exc_info=True)
    finally:
        db.close()


# ── Chat ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatMessageOut)
async def post_chat(
    body:             ChatMessageIn,
    background_tasks: BackgroundTasks,
    db:               Session = Depends(get_db),
    current_user:     User    = Depends(get_current_user),
):
    if not chat_service.consume_chat_rate(current_user.email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
        )

    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        session_id, reply = await chat_service.handle_chat_message(
            db,
            user=current_user,
            session_id=body.sessionId,
            message=body.message,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception:
        # Log with traceback so the failure mode (missing tables, dropped
        # connection, etc.) is recoverable from Cloud Run logs; the UI gets
        # a structured payload it can render instead of an opaque 500.
        logger.exception("Kairos chat failed for %s", current_user.email)
        raise HTTPException(
            status_code=500,
            detail={"code": "SERVER_ERROR", "message": "Kairos couldn't process that message. Please try again."},
        )

    if reply.get("intent") == "fallback":
        background_tasks.add_task(_track_fallback_in_background, body.message)

    return {"success": True, "sessionId": session_id, "message": reply}


@router.get("/chat/context", response_model=ChatContextOut)
def get_context():
    return {"success": True, "context": chat_context()}


@router.get("/chat/sessions")
def list_sessions(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    sessions = chat_service.list_user_conversations(db, current_user.email)
    return {"success": True, "sessions": sessions}


@router.post("/chat/sessions", response_model=CreateSessionOut)
def create_session(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    conv = chat_service.create_conversation(
        db,
        user_email=current_user.email,
        user_name=current_user.name or "",
        company=current_user.department or "",
        employee_id=current_user.employee_id or "",
    )
    return {
        "success": True,
        "session": {"sessionId": conv.session_id, "expiresAt": conv.expires_at},
    }


@router.patch("/chat/sessions/{session_id}/end")
def patch_end_session(
    session_id:   str,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    chat_service.end_conversation(db, session_id, current_user.email)
    return {"success": True}


@router.delete("/chat/sessions/{session_id}")
def delete_session(
    session_id:   str,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    chat_service.delete_conversation(db, session_id, current_user.email)
    return {"success": True}


@router.get("/chat/history/{session_id}")
def get_history(
    session_id:   str,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    conv = chat_service.get_conversation(db, session_id)
    if not conv or conv.user_email != current_user.email:
        raise HTTPException(status_code=403, detail="You do not have access to that conversation.")
    return {"success": True, "messages": chat_service.history_with_welcome(db, session_id, current_user.name or "")}


@router.get("/chat/new-prompts")
def get_new_prompts(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """Super-admin only — surfaces unmatched user prompts so platform curators
    can extend the knowledge base.

    KairosNewPrompt stores the prompt text as its primary key with no
    organization_id column, making per-org filtering impossible without a
    schema redesign. The table is therefore platform-level data and must only
    be accessible to super_admin (platform operators), not tenant admins who
    would otherwise see prompts from every organization on the platform.
    """
    if current_user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required.")
    return {"success": True, "prompts": chat_service.list_unknown_prompts(db)}


# ── Mail ──────────────────────────────────────────────────────────────────

@router.get("/mail/status", response_model=MailStatusOut)
def mail_status(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    return {
        "success": True,
        **mail_pipeline.get_mail_limit_status(db, current_user.email),
    }


@router.post("/mail/send")
async def post_mail(
    body:         MailSendIn,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    if not body.sessionId or not body.to or not body.subject:
        raise HTTPException(status_code=400, detail={"code": "MISSING_FIELDS", "message": "Missing required fields."})

    conv = chat_service.get_conversation(db, body.sessionId)
    if not conv or conv.user_email != current_user.email:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You do not have access to that conversation."})

    allowed, ms_before_reset = mail_pipeline.consume_mail_quota(db, current_user.email)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"code": "EMAIL_LIMIT_REACHED", "msBeforeNextReset": ms_before_reset},
        )

    history = chat_service.session_history(db, body.sessionId)

    ok = await mail_pipeline.send_support_email(
        history=history,
        user={
            "name":  current_user.name or "",
            "email": current_user.email,
        },
        to=body.to,
        subject=body.subject,
        body=body.body or "",
    )
    if not ok:
        raise HTTPException(
            status_code=500,
            detail={"code": "SERVER_ERROR", "message": "Email failed to send. Please try again later."},
        )

    return {"success": True, "message": "Email sent successfully."}


# ── Escalation (placeholder, matches Node behaviour) ──────────────────────

@router.post("/escalate", status_code=501)
def post_escalate():
    raise HTTPException(
        status_code=501,
        detail={"success": False, "message": "Human escalation is planned for the next phase and is not enabled yet."},
    )
