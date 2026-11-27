"""
Kairos SQLAlchemy models.

Single-database architecture: these tables live in the SAME Supabase
Postgres instance as the rest of Temveris (`users`, `employees`,
`leave`, etc.). There is no separate Kairos DB.

Naming: all chatbot tables use the `chatbot_` prefix so they're
immediately distinguishable from main-app tables and won't collide with
future Temveris features (e.g. a generic `chats` for team messaging).

History:
  - Pre-consolidation table names: conversations, chats, new_prompts,
    email_rate_limits. Migrated to chatbot_* via
    `backend/scripts/migrate_kairos_to_chatbot.py`.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from database.database import Base


class KairosConversation(Base):
    __tablename__ = "chatbot_conversations"

    id              = Column(String, primary_key=True)
    session_id      = Column(String,  unique=True, index=True, nullable=False)
    # FK to the unified Temveris `users.email`. ON DELETE SET NULL so we
    # keep chat history when a user is removed (audit/compliance).
    user_email      = Column(
        String,
        ForeignKey("users.email", ondelete="SET NULL"),
        index=True, nullable=True,
    )
    user_name       = Column(String,  nullable=True)
    company         = Column(String,  nullable=True)
    employee_id     = Column(String,  nullable=True)
    title           = Column(String,  nullable=True, default="New conversation")
    preview         = Column(String,  nullable=True, default="")
    message_count   = Column(Integer, nullable=False, default=0)
    status          = Column(String,  nullable=False, default="active")
    started_at      = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_message_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    expires_at      = Column(DateTime(timezone=True), nullable=False)
    created_at      = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at      = Column(DateTime(timezone=True), nullable=True)


class KairosChat(Base):
    __tablename__ = "chatbot_messages"

    id          = Column(String, primary_key=True)
    session_id  = Column(String,  index=True, nullable=False)
    user_email  = Column(
        String,
        ForeignKey("users.email", ondelete="SET NULL"),
        index=True, nullable=True,
    )
    role        = Column(String,  nullable=False)   # "user" | "assistant"
    content     = Column(Text,    nullable=False)
    extra_meta  = Column("metadata", JSON, nullable=True)
    expires_at  = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), nullable=True)


class KairosNewPrompt(Base):
    """Tracks user prompts the matcher couldn't classify. Natural PK on
    the prompt text so duplicates increment `count` instead of stacking.
    Not linked to users — purely a moderation/curation surface."""
    __tablename__ = "chatbot_unknown_prompts"

    prompt = Column(String,  primary_key=True)
    count  = Column(Integer, nullable=False, default=1)


class KairosEmailRateLimit(Base):
    """Per-user support-mail rate limiter. `key` is the user's email; we
    don't FK it because rows can outlive the user (and the rate-limit
    state should still hold for re-onboarded addresses)."""
    __tablename__ = "chatbot_email_rate_limits"

    key        = Column(String,   primary_key=True)
    points     = Column(Integer,  nullable=False, default=0)
    expire     = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=True)
