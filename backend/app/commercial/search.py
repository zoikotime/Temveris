"""Commercial operations search / correlation resolver (Ops Center).

Resolves any operator-facing identifier — correlation id, support reference
(ZT-…), org / workspace / signup id, event id, or email — to the underlying
entity and its ordered event timeline (the "correlation chain"). This closes
the audit §9 gap: "Support must be able to search by email, organization,
workspace ID, trace ID, correlation ID …".

Pure read over existing tables (commercial_events.correlation_id is indexed);
no new state, no new events. Trace IDs are not persisted today (they are minted
per error response), so a ``trc_`` query resolves to nothing — reported plainly
rather than faked.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.commercial.models import (
    CommercialEvent, CommercialSignup, CommercialOrganization, CommercialWorkspace,
)

_TIMELINE_CAP = 200


def _event_dict(r: CommercialEvent) -> dict:
    return {
        "event_id": r.event_id,
        "event_type": r.event_type,
        "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        "source_service": r.source_service,
        "organization_id": r.organization_id,
        "workspace_id": r.workspace_id,
        "user_id": r.user_id,
        "state_from": r.state_from,
        "state_to": r.state_to,
        "error_code": r.error_code,
        "correlation_id": r.correlation_id,
    }


def _timeline_for_correlation(db: Session, correlation_id: str) -> list[dict]:
    rows = (db.query(CommercialEvent)
            .filter(CommercialEvent.correlation_id == correlation_id)
            .order_by(CommercialEvent.occurred_at.asc())
            .limit(_TIMELINE_CAP).all())
    return [_event_dict(r) for r in rows]


def _timeline_for_org(db: Session, organization_id: str) -> list[dict]:
    rows = (db.query(CommercialEvent)
            .filter(CommercialEvent.organization_id == organization_id)
            .order_by(CommercialEvent.occurred_at.asc())
            .limit(_TIMELINE_CAP).all())
    return [_event_dict(r) for r in rows]


def _classify(q: str) -> str:
    ql = q.lower()
    if ql.startswith("cor_"):
        return "correlation_id"
    if q.upper().startswith("ZT-"):
        return "support_reference"
    if ql.startswith("trc_"):
        return "trace_id"
    if ql.startswith("org_"):
        return "organization"
    if ql.startswith(("wsp_", "ws_")):
        return "workspace"
    if ql.startswith("sgn_"):
        return "signup"
    if ql.startswith("evt_"):
        return "event"
    if "@" in q:
        return "email"
    return "text"


def resolve(db: Session, q: str) -> dict:
    """Resolve an identifier to {kind, entity, correlation_id, timeline}."""
    q = (q or "").strip()
    out = {"query": q, "kind": None, "entity": None,
           "correlation_id": None, "timeline": [], "note": None}
    if not q:
        out["note"] = "empty query"
        return out

    kind = _classify(q)
    out["kind"] = kind
    correlation_id: Optional[str] = None
    organization_id: Optional[str] = None

    if kind == "correlation_id":
        correlation_id = q

    elif kind == "support_reference":
        code = q[3:].strip().lower()  # strip "ZT-"
        ev = (db.query(CommercialEvent)
              .filter(CommercialEvent.correlation_id.ilike(f"cor_{code}%"))
              .first())
        if ev:
            correlation_id = ev.correlation_id
        else:
            out["note"] = "no activity found for that support reference"

    elif kind == "trace_id":
        out["note"] = "trace IDs are per-response and not persisted; search by correlation ID instead"

    elif kind == "organization":
        org = db.query(CommercialOrganization).filter_by(id=q).one_or_none()
        if org:
            organization_id = org.id
            out["entity"] = {"type": "organization", "id": org.id, "name": org.name,
                             "commercial_state": org.commercial_state}
        else:
            out["note"] = "organization not found"

    elif kind == "workspace":
        ws = db.query(CommercialWorkspace).filter_by(id=q).one_or_none()
        if ws:
            organization_id = ws.organization_id
            out["entity"] = {"type": "workspace", "id": ws.id,
                             "organization_id": ws.organization_id,
                             "provisioning_state": ws.provisioning_state}
        else:
            out["note"] = "workspace not found"

    elif kind == "signup":
        sg = db.query(CommercialSignup).filter_by(id=q).one_or_none()
        if sg:
            correlation_id = sg.correlation_id
            organization_id = sg.organization_id
            out["entity"] = {"type": "signup", "id": sg.id, "email": sg.email,
                             "state": sg.state, "organization_id": sg.organization_id}
        else:
            out["note"] = "signup not found"

    elif kind == "event":
        ev = db.query(CommercialEvent).filter_by(event_id=q).one_or_none()
        if ev:
            correlation_id = ev.correlation_id
            organization_id = ev.organization_id
        else:
            out["note"] = "event not found"

    elif kind in ("email", "text"):
        sg = (db.query(CommercialSignup)
              .filter(CommercialSignup.email == q.strip().lower())
              .order_by(CommercialSignup.created_at.desc())
              .first())
        if sg:
            correlation_id = sg.correlation_id
            organization_id = sg.organization_id
            out["kind"] = "email"
            out["entity"] = {"type": "signup", "id": sg.id, "email": sg.email,
                             "state": sg.state, "organization_id": sg.organization_id}
        else:
            out["note"] = "no signup found for that value"

    out["correlation_id"] = correlation_id
    if correlation_id:
        out["timeline"] = _timeline_for_correlation(db, correlation_id)
    elif organization_id:
        out["timeline"] = _timeline_for_org(db, organization_id)
    return out
