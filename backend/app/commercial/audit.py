"""Commercial event emitter (§16) — implements invariant I-4.

Every signup, consent, billing, provisioning, verification, entitlement,
onboarding, upgrade, downgrade, cancellation, suspension, and recovery
event must be logged with the v3.0 envelope:

    event_id, event_type, occurred_at, source_service, tenant_id,
    organization_id, workspace_id, user_id, correlation_id, payload

The emitter writes to commercial_events. Emission is best-effort: if the
DB write fails we log and re-raise so the caller decides whether the
broader transaction should abort. Per I-5 (no silent failures) callers
should *not* swallow these errors blindly — most call sites either run
inside the same transaction as the state mutation they describe, or
inside the orchestrator's retry envelope.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict

from sqlalchemy.orm import Session

from app.commercial import constants, ids
from app.commercial.models import CommercialEvent

_log = logging.getLogger("zoiko.commercial.audit")


def emit_event(
    db: Session,
    *,
    event_type: str,
    source_service: str,
    payload: Optional[Dict[str, Any]] = None,
    organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    state_from: Optional[str] = None,
    state_to: Optional[str] = None,
    error_code: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    commit: bool = False,
) -> CommercialEvent:
    """Write a commercial event row.

    By default does NOT commit — the caller's transaction owns commit.
    Pass commit=True for fire-and-forget emissions outside a request
    (e.g. background schedulers).
    """
    if event_type not in constants.VALID_EVENT_TYPES:
        # Don't reject — fail open with a warning. New event types are
        # added often; better to record than to drop. Operations will
        # see unknown types surface in the events dashboard.
        _log.warning("emit_event called with unknown event_type=%r", event_type)

    ev = CommercialEvent(
        event_id=ids.event_id(),
        event_type=event_type,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        source_service=source_service,
        # tenant_id == organization_id in our model (one tenant per org).
        tenant_id=organization_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload=payload or {},
        state_from=state_from,
        state_to=state_to,
        error_code=error_code,
    )
    db.add(ev)
    db.flush()
    if commit:
        db.commit()
    return ev


def emit_many(db: Session, events: list[dict], *, commit: bool = False) -> list[CommercialEvent]:
    """Batch helper — useful for orchestrator step records."""
    out = []
    for spec in events:
        out.append(emit_event(db, commit=False, **spec))
    if commit:
        db.commit()
    return out


# ──────────────────────────────────────────────────────────────────────────
# Convenience helpers for common envelopes.
# ──────────────────────────────────────────────────────────────────────────
def signup_transition(
    db: Session, *, from_state: str, to_state: str, correlation_id: str,
    user_id: Optional[int] = None, organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None, payload: Optional[dict] = None,
    error_code: Optional[str] = None,
):
    """Emit a signup.* event for a state transition."""
    event_map = {
        ("signup_started", "signup_submitted"): "signup.submitted",
        ("signup_submitted", "validation_failed"): "signup.validation_failed",
        ("signup_submitted", "abuse_check_pending"): "signup.submitted",
        ("abuse_check_pending", "abuse_blocked"): "signup.abuse_flagged",
        ("abuse_check_pending", "manual_review_required"): "signup.abuse_flagged",
        ("trial_created", "signup_complete"): "signup.completed",
        ("subscription_active", "signup_complete"): "signup.completed",
    }
    et = event_map.get((from_state, to_state), "signup.submitted")
    return emit_event(
        db,
        event_type=et,
        source_service="activation-orchestration",
        payload=payload or {},
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
        state_from=from_state,
        state_to=to_state,
        error_code=error_code,
    )
