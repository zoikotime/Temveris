"""
Support-email pipeline. Replaces `KAIROS-BE/backend/services/mailService.js`
plus the email-formatting + rate-limit helpers.

Sending is delegated to the existing `app.email_service._send`, which is
already Resend-first with SMTP fallback (works on Cloud Run; SMTP alone
does not).
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.email_service import _send  # type: ignore[attr-defined]
from app.kairos.models import KairosEmailRateLimit
from app.kairos.translate import (
    detect_language_from_text,
    get_lang_code,
    process_support_email,
    translate_transcript_messages,
)

EMAIL_LIMIT = 5
WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Rate limiting (DB-backed, mirrors Node behaviour) ─────────────────────

def get_mail_limit_status(db: Session, key: str) -> dict[str, Any]:
    if not key:
        return {
            "allowed":          True,
            "remaining":        EMAIL_LIMIT,
            "limit":            EMAIL_LIMIT,
            "msBeforeNextReset": 0,
        }

    row = db.query(KairosEmailRateLimit).filter(KairosEmailRateLimit.key == key).first()
    if not row or _now() >= row.expire:
        return {
            "allowed":          True,
            "remaining":        EMAIL_LIMIT,
            "limit":            EMAIL_LIMIT,
            "msBeforeNextReset": 0,
        }

    remaining = max(0, EMAIL_LIMIT - (row.points or 0))
    return {
        "allowed":          remaining > 0,
        "remaining":        remaining,
        "limit":            EMAIL_LIMIT,
        "msBeforeNextReset": max(0, int((row.expire - _now()).total_seconds() * 1000)),
    }


def consume_mail_quota(db: Session, key: str) -> tuple[bool, int]:
    """Returns (allowed, msBeforeReset). On allow, increments the counter."""
    if not key:
        return True, 0
    row = db.query(KairosEmailRateLimit).filter(KairosEmailRateLimit.key == key).first()
    now = _now()

    if not row:
        row = KairosEmailRateLimit(key=key, points=1, expire=now + WINDOW)
        db.add(row)
        db.commit()
        return True, 0

    if now >= row.expire:
        row.points = 1
        row.expire = now + WINDOW
        db.commit()
        return True, 0

    if (row.points or 0) >= EMAIL_LIMIT:
        return False, max(0, int((row.expire - now).total_seconds() * 1000))

    row.points = (row.points or 0) + 1
    db.commit()
    return True, 0


# ── Body extraction + HTML rendering ──────────────────────────────────────

_ISSUE_RE = re.compile(
    r"Issue:\s*(.*?)\s*-{2,}\s*User Description:",
    re.IGNORECASE | re.DOTALL,
)
_DESCRIPTION_RE = re.compile(
    r"User Description:\s*(.*?)\s*-{2,}\s*Chat History:",
    re.IGNORECASE | re.DOTALL,
)


def _extract_sections(raw_body: str, fallback_subject: str) -> dict[str, str]:
    body = raw_body or ""
    issue_match = _ISSUE_RE.search(body)
    desc_match  = _DESCRIPTION_RE.search(body)
    return {
        "issue":       (issue_match.group(1) if issue_match else fallback_subject or "").strip(),
        "description": (desc_match.group(1) if desc_match else body).strip(),
    }


def _format_time(value) -> str:
    if not value:
        return "--:--"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "--:--"
    return value.strftime("%H:%M")


def _clean_transcript_message(text: str, role: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if role != "assistant":
        return cleaned
    cleaned = re.sub(r"•\s*\d+️⃣[\s\S]*$", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"Just type a number or describe what you need!?[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _format_chat_history(
    messages: list[dict[str, Any]],
    user: dict[str, Any],
    subject: str,
    description: str,
    translation: dict[str, Any],
) -> str:
    translated_subject     = translation.get("translatedSubject") or subject or "N/A"
    translated_description = translation.get("translatedDescription") or description or "N/A"
    original_language      = translation.get("originalLanguage") or "Unknown"
    was_translated         = bool(translation.get("wasTranslated"))

    rows = []
    for msg in messages:
        is_user  = msg.get("role") == "user"
        sender   = (user.get("name") or "User") if is_user else "Bot"
        time_str = _html.escape(_format_time(msg.get("timestamp")))
        body     = _html.escape(_clean_transcript_message(
            msg.get("translatedContent") or msg.get("content") or "",
            msg.get("role") or "assistant",
        ))
        color = "#1d4ed8" if is_user else "#166534"
        rows.append(
            f'<div style="color:{color};">{time_str} {_html.escape(sender)}: {body}</div>'
        )
    transcript_html = "".join(rows) or "<div>No messages</div>"

    blocks = [
        f"<p><b>User:</b> {_html.escape(user.get('name') or 'N/A')}</p>",
        f"<p><b>Email:</b> {_html.escape(user.get('email') or 'N/A')}</p>",
        f"<p><b>Original Language:</b> {_html.escape(original_language)}</p>",
        "<p><b>Translated To:</b> English</p>",
        f"<p><b>Issue In English:</b> {_html.escape(translated_subject)}</p>",
    ]
    if was_translated:
        blocks.append(f"<p><b>Original Issue:</b> {_html.escape(subject or 'N/A')}</p>")
    blocks.append(f"<p><b>Description In English:</b> {_html.escape(translated_description)}</p>")
    if was_translated:
        blocks.append(f"<p><b>Original Description:</b> {_html.escape(description or 'N/A')}</p>")
    blocks.append(f"<p><b>Chat Conversation:</b></p><div>{transcript_html}</div>")

    return (
        '<div style="font-family:Arial, sans-serif; padding:16px; color:#111;">'
        + "".join(blocks)
        + "</div>"
    )


# ── Public entry point ────────────────────────────────────────────────────

def _resolve_original_language(
    *,
    issue: str,
    description: str,
    translated_messages: list[dict[str, Any]],
    translated_body: dict[str, Any],
    translated_subject: dict[str, Any],
) -> str:
    detected = detect_language_from_text(f"{description} {issue}".strip())
    if detected and detected.lower() != "unknown":
        return detected
    for translated in (translated_body, translated_subject):
        src = (translated.get("sourceLang") or "").lower()
        if src and src not in {"unknown", "english"}:
            return translated["sourceLang"]
    for msg in translated_messages:
        if msg.get("role") != "user":
            continue
        lang = (msg.get("detectedLanguage") or "").lower()
        if lang and lang not in {"unknown", "english"}:
            return msg["detectedLanguage"]
    return "English"


async def send_support_email(
    *,
    history: list[dict[str, Any]],
    user: dict[str, Any],
    to: str,
    subject: str,
    body: str,
) -> bool:
    extracted = _extract_sections(body or "", subject)

    translated_subject = await process_support_email(extracted["issue"] or subject)
    translated_body    = await process_support_email(extracted["description"] or "")
    translated_msgs    = await translate_transcript_messages(history[-10:])

    original_language = _resolve_original_language(
        issue=extracted["issue"] or subject,
        description=extracted["description"] or "",
        translated_messages=translated_msgs,
        translated_body=translated_body,
        translated_subject=translated_subject,
    )

    html = _format_chat_history(
        translated_msgs,
        user,
        extracted["issue"] or subject,
        extracted["description"] or "",
        {
            "originalLanguage":       original_language,
            "originalLangCode":       get_lang_code(original_language),
            "translatedSubject":      translated_subject.get("englishBody") or extracted["issue"] or subject,
            "translatedDescription":  translated_body.get("englishBody") or extracted["description"] or "",
            "wasTranslated":          translated_subject.get("wasTranslated") or translated_body.get("wasTranslated"),
        },
    )

    final_subject = translated_subject.get("englishBody") or subject
    return _send(to=to, subject=final_subject, html=html)
