"""
Worker Settings router — Account → Settings.

Endpoints
---------
GET    /worker/settings                       Read current preferences (snapshot).
PATCH  /worker/settings                       Apply one or more allowed changes.
POST   /worker/settings/reset                 Reset scoped personal preferences (preserves
                                              presentation_mode per spec §14).
GET    /worker/settings/policy-locks          Workspace-controlled locks with policy refs.
GET    /worker/calendar-connections           List worker calendar connections.
POST   /worker/calendar-connections           Record a calendar connection (OAuth stubbed
                                              for v1; no egress).
DELETE /worker/calendar-connections/{id}      Disconnect a calendar.

Locked-field rejection contract (spec §19)
------------------------------------------
PATCH for a workspace-locked field returns HTTP 409 with this body:

    {
      "error":           "SETTING_LOCKED_BY_WORKSPACE",
      "field":           "<key>",
      "message":         "...",
      "policy_ref":      "<policy id>",
      "policy_version":  "...",
      "current_value":   <value>
    }

Returning current_value lets the UI snap back to the workspace state without a
second round-trip.

Storage strategy (v1)
---------------------
Dedicated tables (`worker_settings`, `worker_calendar_connection`,
`worker_settings_event`) have not yet shipped. Settings are stored as
`AuditLog` payloads:

  entity=WorkerSettings  action=SettingsSet           — latest snapshot
  entity=WorkerSettings  action=SettingsChanged       — per-field event
  entity=WorkerSettings  action=SettingsReset
  entity=WorkerSettings  action=SettingsLockedRejected (compliance-relevant)
  entity=WorkerCalendarConnection action=ConnectionAdded
  entity=WorkerCalendarConnection action=ConnectionRemoved

Compliance audit (spec §17): hide_payroll_values=False, presentation_mode=False,
calendar add/remove, and any locked-field rejection.

Egress
------
None for v1. Calendar OAuth handshakes are intentionally stubbed — the
`POST /worker/calendar-connections` endpoint records the connection metadata
only and the frontend explains that real sync activates with the integrations
rollout. No outbound HTTP from this router.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any, List, Set

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user


router = APIRouter(prefix="/worker", tags=["Worker Settings"])


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS, ENUMS, AND CONSTANTS — kept in lock-step with SettingsModule.jsx.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS: Dict[str, Any] = {
    "theme":                            "system",
    "text_size":                        "default",
    "high_contrast":                    False,
    "time_format":                      "workspace",
    "language":                         "en-US",
    "timezone":                         "UTC",
    "date_format":                      "MM/DD/YYYY",
    "number_format":                    "1,234.56",
    "first_day_of_week":                "workspace",
    "default_landing_page":             "my-work",
    "show_next_shift":                  True,
    "show_weekly_hours":                True,
    "calendar_view":                    "week",
    "show_keyboard_shortcuts":          False,
    "hide_payroll_values":              True,
    "presentation_mode":                False,
    "correction_confirmation_enabled":  True,
    "product_tips_enabled":             True,
    "feature_announcements_enabled":    True,
}

# Enum allow-lists for validation. Anything not in here is rejected as 400.
ENUM_VALUES: Dict[str, Set[str]] = {
    "theme":                {"light", "dark", "system"},
    "text_size":            {"compact", "default", "large", "xlarge"},
    "time_format":          {"12h", "24h", "workspace"},
    "language":             {"en-US", "en-GB", "es", "fr", "de", "pt-BR", "ja", "zh-Hans"},
    "date_format":          {"MM/DD/YYYY", "DD/MM/YYYY", "YYYY-MM-DD", "D MMM YYYY"},
    "number_format":        {"1,234.56", "1.234,56", "1 234,56", "1’234.56"},
    "first_day_of_week":    {"sunday", "monday", "workspace"},
    "default_landing_page": {"my-work", "activity-log", "my-schedule"},
    "calendar_view":        {"day", "week", "month"},
}
BOOL_FIELDS = {
    "high_contrast", "show_next_shift", "show_weekly_hours",
    "show_keyboard_shortcuts", "hide_payroll_values", "presentation_mode",
    "correction_confirmation_enabled", "product_tips_enabled",
    "feature_announcements_enabled",
}
STRING_FIELDS = {"timezone"}  # validated at length only — IANA list too large to enumerate
ALLOWED_FIELDS = set(DEFAULTS.keys())

# Reset scope — never touches presentation_mode (spec §14).
RESET_KEYS = {
    "theme", "text_size", "high_contrast", "time_format",
    "language", "timezone", "date_format", "number_format", "first_day_of_week",
    "default_landing_page", "show_next_shift", "show_weekly_hours",
    "calendar_view", "show_keyboard_shortcuts",
    "hide_payroll_values",
    "correction_confirmation_enabled",
    "product_tips_enabled", "feature_announcements_enabled",
}

# Compliance-relevant changes (spec §17):
#   - hide_payroll_values turned OFF
#   - presentation_mode turned OFF (when previously ON)
#   - any locked-field rejection
#   - calendar connection add / remove
def _is_compliance_relevant(field: str, prev: Any, new: Any) -> bool:
    if field == "hide_payroll_values" and prev is True and new is False:
        return True
    if field == "presentation_mode" and prev is True and new is False:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
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


def _latest_settings_row(db: Session, user_id: int) -> Optional[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == user_id,
            models.AuditLog.entity == "WorkerSettings",
            models.AuditLog.action == "SettingsSet",
        )
        .order_by(models.AuditLog.created_at.desc())
        .first()
    )


def _load_settings(db: Session, user_id: int) -> Dict[str, Any]:
    row = _latest_settings_row(db, user_id)
    base = dict(DEFAULTS)
    if not row or not row.payload:
        return base
    for k, v in row.payload.items():
        if k in ALLOWED_FIELDS:
            base[k] = v
    return base


def _resolve_locks(db: Session, user_id: int) -> Dict[str, Dict[str, Any]]:
    """
    Resolve workspace-controlled locks for this worker.

    v1: there's no dedicated workspace policy table for these fields, so this
    returns an empty dict and the frontend treats every field as
    worker-editable. When workspace locale/timezone policies land, return
    a dict of { field: { current_value, policy_name, policy_version, policy_url } }
    so the frontend can render the locked-state UI without UI assumptions.
    """
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _load_settings(db, current_user.id)


@router.get("/settings/policy-locks")
def get_policy_locks(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _resolve_locks(db, current_user.id)


class SettingsPatch(BaseModel):
    # Each field is optional — PATCH applies only what's sent.
    theme:                            Optional[str]  = None
    text_size:                        Optional[str]  = None
    high_contrast:                    Optional[bool] = None
    time_format:                      Optional[str]  = None
    language:                         Optional[str]  = None
    timezone:                         Optional[str]  = Field(None, max_length=64)
    date_format:                      Optional[str]  = None
    number_format:                    Optional[str]  = None
    first_day_of_week:                Optional[str]  = None
    default_landing_page:             Optional[str]  = None
    show_next_shift:                  Optional[bool] = None
    show_weekly_hours:                Optional[bool] = None
    calendar_view:                    Optional[str]  = None
    show_keyboard_shortcuts:          Optional[bool] = None
    hide_payroll_values:              Optional[bool] = None
    presentation_mode:                Optional[bool] = None
    correction_confirmation_enabled:  Optional[bool] = None
    product_tips_enabled:             Optional[bool] = None
    feature_announcements_enabled:    Optional[bool] = None


def _validate_field(field: str, value: Any) -> None:
    if field in BOOL_FIELDS:
        if not isinstance(value, bool):
            raise HTTPException(400, detail=f"{field} must be a boolean")
        return
    if field in ENUM_VALUES:
        if value not in ENUM_VALUES[field]:
            raise HTTPException(400, detail=f"{field} value '{value}' is not allowed")
        return
    if field in STRING_FIELDS:
        if not isinstance(value, str) or not (1 <= len(value) <= 64):
            raise HTTPException(400, detail=f"{field} must be a 1–64 char string")
        return
    raise HTTPException(400, detail=f"unknown field: {field}")


def _locked_response(field: str, lock: Dict[str, Any]) -> JSONResponse:
    """Spec §19 locked-field rejection contract. HTTP 409 to distinguish from
    400 (validation) and 403 (auth)."""
    return JSONResponse(
        status_code=409,
        content={
            "error":          "SETTING_LOCKED_BY_WORKSPACE",
            "field":          field,
            "message":        "This setting is managed by your workspace policy.",
            "policy_ref":     lock.get("policy_name"),
            "policy_version": lock.get("policy_version"),
            "current_value":  lock.get("current_value"),
        },
    )


@router.patch("/settings")
def patch_settings(
    body: SettingsPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    delta_in: Dict[str, Any] = {k: v for k, v in body.model_dump().items() if v is not None}
    if not delta_in:
        return _load_settings(db, current_user.id)

    locks = _resolve_locks(db, current_user.id)
    current = _load_settings(db, current_user.id)
    delta:   Dict[str, Any] = {}

    for field, value in delta_in.items():
        if field not in ALLOWED_FIELDS:
            continue
        _validate_field(field, value)
        if field in locks:
            # Audit the rejected attempt for compliance, then return 409.
            _audit(
                db,
                actor_id=current_user.id,
                target_id=current_user.id,
                entity="WorkerSettings",
                action="SettingsLockedRejected",
                payload={"field": field, "attempted_value": value, "lock": locks[field]},
                request=request,
                org_id=current_user.organization_id,
            )
            return _locked_response(field, locks[field])
        if current.get(field) != value:
            delta[field] = value

    if not delta:
        return current

    # Per-field event rows (event store) + compliance rows where applicable.
    for field, new_value in delta.items():
        prev = current.get(field)
        compliance = _is_compliance_relevant(field, prev, new_value)
        _audit(
            db,
            actor_id=current_user.id,
            target_id=current_user.id,
            entity="WorkerSettings",
            action="SettingsChanged",
            payload={
                "field":              field,
                "from":               prev,
                "to":                 new_value,
                "compliance_relevant": compliance,
            },
            request=request,
            org_id=current_user.organization_id,
        )

    next_state = {**current, **delta}
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="WorkerSettings",
        action="SettingsSet",
        payload=next_state,
        request=request,
        org_id=current_user.organization_id,
    )
    return next_state


@router.post("/settings/reset")
def reset_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reset only personal preferences. Preserves presentation_mode (spec §14)."""
    current = _load_settings(db, current_user.id)
    next_state = dict(current)
    for k in RESET_KEYS:
        next_state[k] = DEFAULTS[k]
    # Presentation mode + any non-RESET fields are preserved by construction.

    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="WorkerSettings",
        action="SettingsReset",
        payload={"preserved_presentation_mode": current.get("presentation_mode") is True},
        request=request,
        org_id=current_user.organization_id,
    )
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="WorkerSettings",
        action="SettingsSet",
        payload=next_state,
        request=request,
        org_id=current_user.organization_id,
    )
    return next_state


# ─────────────────────────────────────────────────────────────────────────────
# Calendar connections (stubbed — no real OAuth in v1)
# ─────────────────────────────────────────────────────────────────────────────
ALLOWED_PROVIDERS = {"google", "outlook"}


def _project_calendar(row: models.AuditLog) -> Dict[str, Any]:
    p = row.payload or {}
    return {
        "id":                row.id,
        "provider":          p.get("provider"),
        "account_email":     p.get("account_email"),
        "connection_status": p.get("connection_status", "connected"),
        "scopes_granted":    p.get("scopes_granted", []),
        "last_synced_at":    p.get("last_synced_at"),
        "connected_at":      row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/calendar-connections")
def list_calendar_connections(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "WorkerCalendarConnection",
            models.AuditLog.action == "ConnectionAdded",
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(50)
        .all()
    )
    # Filter out entries that have a matching ConnectionRemoved event.
    removed_ids: Set[int] = {
        (r.payload or {}).get("connection_id")
        for r in db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "WorkerCalendarConnection",
            models.AuditLog.action == "ConnectionRemoved",
        ).all()
        if (r.payload or {}).get("connection_id") is not None
    }
    return [_project_calendar(r) for r in rows if r.id not in removed_ids]


class CalendarConnectIn(BaseModel):
    provider:      str           = Field(..., max_length=32)
    account_email: Optional[str] = Field(None, max_length=320)


@router.post("/calendar-connections", status_code=201)
def add_calendar_connection(
    body: CalendarConnectIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    provider = body.provider.lower().strip()
    if provider not in ALLOWED_PROVIDERS:
        raise HTTPException(400, detail=f"provider must be one of {sorted(ALLOWED_PROVIDERS)}")

    # v1: no real OAuth handshake. Record the intent. Real sync activates with
    # the integrations rollout. Compliance audit is written because adding a
    # calendar grants Temveris visibility into meeting metadata.
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="WorkerCalendarConnection",
        action="ConnectionAdded",
        payload={
            "provider":          provider,
            "account_email":     body.account_email or current_user.email,
            "connection_status": "pending_oauth",
            "scopes_granted":    ["calendar.read.freebusy"],
            "last_synced_at":    None,
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return _project_calendar(row)


@router.delete("/calendar-connections/{connection_id}", status_code=200)
def remove_calendar_connection(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.id == connection_id,
            models.AuditLog.entity == "WorkerCalendarConnection",
            models.AuditLog.action == "ConnectionAdded",
        )
        .first()
    )
    if row is None or row.actor_id != current_user.id:
        raise HTTPException(404, detail="Calendar connection not found")

    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="WorkerCalendarConnection",
        action="ConnectionRemoved",
        payload={
            "connection_id": connection_id,
            "provider":      (row.payload or {}).get("provider"),
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"removed": True, "id": connection_id}
