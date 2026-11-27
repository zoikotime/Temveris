"""
Pydantic request/response schemas for Kairos endpoints.

Field naming mirrors the Node JSON contract (camelCase) so the React widget
needs no changes beyond its baseURL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Chat ──────────────────────────────────────────────────────────────────

class ChatMessageIn(BaseModel):
    sessionId: str | None = None
    message:   str = Field(..., min_length=1, max_length=4000)
    language:  str = "en"


class ChatReply(BaseModel):
    id:              str
    answer:          str
    matchedQuestion: str | None = None
    confidence:      float = 0.0
    suggestions:     list[str] = Field(default_factory=list)
    route:           str | dict[str, Any] | None = None
    intent:          str | None = None
    timestamp:       str


class ChatMessageOut(BaseModel):
    success:   bool = True
    sessionId: str
    message:   ChatReply


# ── Sessions / history ────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    sessionId:     str
    title:         str | None = None
    preview:       str | None = None
    messageCount:  int = 0
    status:        str = "active"
    startedAt:     datetime | None = None
    lastMessageAt: datetime | None = None
    expiresAt:     datetime | None = None


class HistoryMessage(BaseModel):
    id:        str
    role:      str
    content:   str
    meta:      dict[str, Any] | None = None
    timestamp: datetime | str | None = None


class CreateSessionOut(BaseModel):
    success:  bool = True
    session:  dict[str, Any]


# ── Mail ──────────────────────────────────────────────────────────────────

class MailSendIn(BaseModel):
    sessionId: str
    to:        str
    subject:   str
    body:      str | None = ""
    user:      dict[str, Any] | None = None
    email:     str | None = None


class MailStatusOut(BaseModel):
    success:           bool = True
    allowed:           bool
    remaining:         int
    limit:             int
    msBeforeNextReset: int = 0


# ── Context ───────────────────────────────────────────────────────────────

class ChatContextOut(BaseModel):
    success: bool = True
    context: dict[str, Any]
