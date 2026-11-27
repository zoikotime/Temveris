"""Shared helpers for v1 commercial routers."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import Request

from app.client_ip import client_ip as resolve_client_ip
from app.commercial import ids


def client_meta(request: Optional[Request]) -> Tuple[Optional[str], Optional[str]]:
    """Return (ip_address, user_agent_hash) from the request.

    Resolved through the canonical `app.client_ip` helper rather than by reading
    `X-Forwarded-For[0]` here. This value feeds the fraud velocity buckets
    (`fraud.record_attempt` / `_check_ip_velocity`), and the leftmost forwarded
    entry is whatever the client sent — Google appends the real peer after it. A
    caller could therefore have reset its own velocity bucket on every request
    simply by varying the header.
    """
    if request is None:
        return None, None
    ip = resolve_client_ip(request)
    ua = request.headers.get("user-agent", "")
    ua_hash = hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16] if ua else None
    return ip, ua_hash


def error_payload(
    *,
    code: str,
    message: str,
    field: Optional[str] = None,
    recoverable: bool = True,
    recovery_action: Optional[str] = None,
) -> dict:
    """Build the §15 error response body."""
    return {
        "error": {
            "code": code,
            "message": message,
            "field": field,
            "recoverable": recoverable,
            "recovery_action": recovery_action,
            "trace_id": ids.trace_id(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }


def app_base_url() -> str:
    return (os.getenv("FRONTEND_URL") or "http://localhost:5173").rstrip("/")
