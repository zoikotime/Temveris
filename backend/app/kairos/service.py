"""
Conversation + message persistence layer for Kairos.

Mirrors the behaviour of `KAIROS-BE/backend/services/chatService.js`,
talking to the same Postgres tables but via SQLAlchemy instead of
supabase-js. The Node service maintained an in-process map as a hot cache;
we skip that — Postgres is the single source of truth and reads are cheap
for this volume of traffic.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.kairos.intent_matcher import generate_chat_reply
from app.kairos.knowledge import build_welcome_message
from app.kairos.models import (
    KairosChat,
    KairosConversation,
    KairosNewPrompt,
)
from app.kairos.translate import normalize_prompt

SESSION_TTL = timedelta(hours=24)


# ── Helpers ───────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry() -> datetime:
    return _now() + SESSION_TTL


def _summarize(text: str, max_len: int) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def _new_session_id() -> str:
    return str(uuid.uuid4())


def create_employee_id(company: str, email: str) -> str:
    company_code = (company or "temveris").replace(" ", "")[:4].upper()
    user_code = (email or "user").split("@")[0][:4].upper()
    return f"{company_code}-{user_code}-{str(int(time.time() * 1000))[-4:]}"


def _serialize_conversation(row: KairosConversation) -> dict[str, Any]:
    return {
        "sessionId":     row.session_id,
        "userEmail":     row.user_email,
        "userName":      row.user_name,
        "company":       row.company,
        "employeeId":    row.employee_id,
        "title":         row.title,
        "preview":       row.preview,
        "messageCount":  row.message_count or 0,
        "status":        row.status,
        "startedAt":     row.started_at,
        "lastMessageAt": row.last_message_at,
        "expiresAt":     row.expires_at,
    }


# ── Conversation CRUD ─────────────────────────────────────────────────────

def get_conversation(db: Session, session_id: str) -> KairosConversation | None:
    return db.query(KairosConversation).filter(KairosConversation.session_id == session_id).first()


def list_user_conversations(db: Session, user_email: str) -> list[dict[str, Any]]:
    rows = (
        db.query(KairosConversation)
          .filter(KairosConversation.user_email == user_email)
          .filter(KairosConversation.expires_at > _now())
          .order_by(desc(KairosConversation.last_message_at))
          .all()
    )
    return [_serialize_conversation(r) for r in rows]


def create_conversation(
    db: Session,
    *,
    user_email: str,
    user_name: str = "",
    company: str = "",
    employee_id: str = "",
    title: str = "New conversation",
) -> KairosConversation:
    row = KairosConversation(
        id=str(uuid.uuid4()),
        session_id=_new_session_id(),
        user_email=user_email,
        user_name=user_name,
        company=company,
        employee_id=employee_id,
        title=title,
        preview="",
        message_count=0,
        status="active",
        started_at=_now(),
        last_message_at=_now(),
        expires_at=_expiry(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def find_or_create_active_conversation(
    db: Session,
    *,
    user_email: str,
    user_name: str = "",
    company: str = "",
    employee_id: str = "",
) -> KairosConversation:
    existing = (
        db.query(KairosConversation)
          .filter(KairosConversation.user_email == user_email)
          .filter(KairosConversation.expires_at > _now())
          .order_by(desc(KairosConversation.last_message_at))
          .first()
    )
    if existing:
        return existing
    return create_conversation(
        db,
        user_email=user_email,
        user_name=user_name,
        company=company,
        employee_id=employee_id,
    )


def end_conversation(db: Session, session_id: str, user_email: str) -> None:
    row = get_conversation(db, session_id)
    if not row or row.user_email != user_email:
        return
    row.status = "ended"
    row.last_message_at = _now()
    row.expires_at = _expiry()
    db.commit()


def delete_conversation(db: Session, session_id: str, user_email: str) -> None:
    row = get_conversation(db, session_id)
    if not row or row.user_email != user_email:
        return
    db.query(KairosChat).filter(KairosChat.session_id == session_id).delete()
    db.delete(row)
    db.commit()


# ── Messages ──────────────────────────────────────────────────────────────

def save_message(
    db: Session,
    *,
    session_id: str,
    user_email: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> KairosChat:
    msg = KairosChat(
        id=str(uuid.uuid4()),
        session_id=session_id,
        user_email=user_email,
        role=role,
        content=content,
        extra_meta=metadata or {},
        expires_at=_expiry(),
        created_at=_now(),
    )
    db.add(msg)

    conv = get_conversation(db, session_id)
    if conv:
        new_count = (conv.message_count or 0) + 1
        conv.message_count = new_count
        conv.preview = _summarize(content, 150)
        if role == "user" and (not conv.title or conv.title == "New conversation"):
            conv.title = _summarize(content, 70) or conv.title
        conv.last_message_at = _now()
        conv.expires_at = _expiry()

    db.commit()
    db.refresh(msg)
    return msg


def session_history(db: Session, session_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(KairosChat)
          .filter(KairosChat.session_id == session_id)
          .order_by(KairosChat.created_at.asc())
          .all()
    )
    return [
        {
            "id":        row.id,
            "role":      row.role,
            "content":   row.content,
            "meta":      row.extra_meta or {},
            "timestamp": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def history_with_welcome(db: Session, session_id: str, user_name: str | None = None) -> list[dict[str, Any]]:
    msgs = session_history(db, session_id)
    if msgs:
        return msgs
    return [
        {
            "id":        "welcome",
            "role":      "assistant",
            "content":   build_welcome_message(user_name),
            "timestamp": _now().isoformat(),
        }
    ]


# ── Unknown-prompt tracking ───────────────────────────────────────────────

async def track_unknown_prompt(db: Session, message: str) -> KairosNewPrompt | None:
    if not message or not message.strip():
        return None
    try:
        norm = await normalize_prompt(message)
    except Exception:
        return None
    text = (norm.get("normalizedText") or "").strip()
    if not text:
        return None

    row = db.query(KairosNewPrompt).filter(KairosNewPrompt.prompt == text).first()
    if row:
        row.count = (row.count or 0) + 1
    else:
        row = KairosNewPrompt(prompt=text, count=1)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_unknown_prompts(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(KairosNewPrompt)
          .order_by(desc(KairosNewPrompt.count), KairosNewPrompt.prompt.asc())
          .all()
    )
    return [
        {"prompt": row.prompt, "count": row.count}
        for row in rows
    ]


# ── Reply orchestration ───────────────────────────────────────────────────

async def handle_chat_message(
    db: Session,
    *,
    user,
    session_id: str | None,
    message: str,
) -> tuple[str, dict[str, Any]]:
    """Save the user message, score it, save the reply, and return
    (sessionId, reply). Mirrors `sendChatMessage` in the Node controller."""
    user_email = user.email
    user_name  = getattr(user, "name", "") or ""
    company    = getattr(user, "department", "") or "Temveris"
    employee_code = getattr(user, "employee_id", "") or create_employee_id(company, user_email)

    if session_id:
        conv = get_conversation(db, session_id)
        if not conv or conv.user_email != user_email:
            raise PermissionError("You do not have access to that conversation.")
    else:
        conv = create_conversation(
            db,
            user_email=user_email,
            user_name=user_name,
            company=company,
            employee_id=employee_code,
        )
        session_id = conv.session_id

    save_message(db, session_id=session_id, user_email=user_email, role="user", content=message.strip())

    try:
        reply = generate_chat_reply(message, user_name=user_name)
    except Exception:
        reply = {
            "id":              str(uuid.uuid4()),
            "answer":          "Sorry, something went wrong.",
            "matchedQuestion": None,
            "confidence":      0.0,
            "suggestions":     [],
            "route":           None,
            "intent":          "error",
            "timestamp":       _now().isoformat(),
        }

    # NOTE: unknown-prompt tracking used to happen here, but `normalize_prompt`
    # makes blocking HTTP calls to external translation services (Lingva +
    # MyMemory). When those hang the whole chat reply waits, so the router
    # now schedules the tracking as a FastAPI BackgroundTask after the
    # response is sent.

    save_message(
        db,
        session_id=session_id,
        user_email=user_email,
        role="assistant",
        content=reply["answer"],
        metadata={
            "matchedQuestion": reply.get("matchedQuestion"),
            "confidence":      reply.get("confidence"),
            "suggestions":     reply.get("suggestions"),
            "route":           reply.get("route"),
            "intent":          reply.get("intent"),
        },
    )

    return session_id, reply


# ── Chat-rate limiter (per-user, in-memory) ───────────────────────────────
# The Node service used `rate-limiter-flexible`'s `RateLimiterMemory`. We
# don't need an external dep for 20 req/min/user — a tiny token bucket does
# it. Per-process state is fine: Cloud Run serves one Python worker per
# instance, and abuse from a single user gets caught by whichever instance
# is handling them.

_RATE_LIMIT_POINTS  = 20
_RATE_LIMIT_WINDOW  = 60.0  # seconds
_rate_lock = threading.Lock()
_rate_state: dict[str, tuple[float, int]] = {}


def consume_chat_rate(key: str) -> bool:
    """Returns True if the request is allowed, False if it must be rejected."""
    now = time.monotonic()
    with _rate_lock:
        window_start, count = _rate_state.get(key, (now, 0))
        if now - window_start >= _RATE_LIMIT_WINDOW:
            window_start, count = now, 0
        if count >= _RATE_LIMIT_POINTS:
            _rate_state[key] = (window_start, count)
            return False
        _rate_state[key] = (window_start, count + 1)
        return True
