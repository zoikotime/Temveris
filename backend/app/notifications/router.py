"""
Notifications module router — worker-facing notification preferences,
history, and test delivery.

Endpoints
---------
GET    /notifications/preferences   Read worker preferences (posture, channels,
                                    quiet hours, custom overrides).
PATCH  /notifications/preferences   Partial update — every accepted field
                                    becomes a NotificationPreferenceChanged
                                    audit event.
GET    /notifications/history       Inbox of past notification events with
                                    filtering by date / category / status / q.
POST   /notifications/test          Send a sample notification to the signed-in
                                    worker only — never to managers, peers, or
                                    the full recipient list.

Storage strategy (v1)
---------------------
Dedicated tables (`worker_notification_preference`, `worker_notification_event`,
etc.) have not yet shipped, so this router stores both preferences and event
records as `AuditLog` rows. The wire shape matches the spec, so when the
real tables land, swap the read/write bodies and the frontend keeps working
with no UI changes.

Multitenancy isolation
----------------------
All AuditLog reads and writes are scoped to the caller's organization_id.
Org A cannot see org B's notification preferences, history, or event log —
even though all records share the same audit_logs table.

Permission model (spec §11)
---------------------------
Every endpoint resolves the caller via `get_current_user`. There is no
admin override here — workers can only read or change their own preferences.
The frontend still renders locked rows, but the source of truth is the
catalog below: the server treats any attempt to disable a `LOCKED_TYPES`
notification as a no-op, regardless of what the client sent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user
# Inert metadata leaf (no I/O, no cycle) — provides message classification and
# the sets that decide which classes are suppressible / quiet-hours eligible.
from app.notifications import classification as _classification
# Dependency-light leaf — HMAC token + suppression store for one-click
# unsubscribe. Imports only stdlib + the ORM model (no auth, no transport).
from app.notifications import suppression as _suppression


router = APIRouter(prefix="/notifications", tags=["Notifications"])


# ─────────────────────────────────────────────────────────────────────────────
# Input validators — keep junk out of the snapshot before it gets persisted to
# the audit log (where bad values would silently corrupt the GET response).
# ─────────────────────────────────────────────────────────────────────────────
_HHMM_RE  = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Approved IANA timezones plus the Temveris-specific "workspace" sentinel.
# Restricted to the set the frontend dropdown can actually emit so the GET
# round-trip never returns an option the picker can't render.
_VALID_TIMEZONES = {
    "workspace", "utc", "ist", "est", "pst", "gmt",
    "UTC", "Asia/Kolkata", "America/New_York", "America/Los_Angeles",
    "Europe/London", "Europe/Berlin", "Australia/Sydney",
}


def _validate_hhmm(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    if not _HHMM_RE.match(value):
        raise HTTPException(400, detail=f"{field} must be a 24-hour HH:MM time (received {value!r})")
    return value


def _validate_timezone(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if value not in _VALID_TIMEZONES:
        raise HTTPException(400, detail=f"timezone must be one of {sorted(_VALID_TIMEZONES)}")
    return value


def _validate_recipients(value: Optional[Dict[str, List[str]]]) -> Optional[Dict[str, List[str]]]:
    if value is None:
        return None
    cleaned: Dict[str, List[str]] = {}
    for group, emails in value.items():
        if not isinstance(emails, list):
            raise HTTPException(400, detail=f"custom_recipients[{group}] must be a list of emails")
        valid: List[str] = []
        for raw in emails:
            email = str(raw or "").strip()
            if not email:
                continue  # silently drop blanks (UI splits on comma)
            if not _EMAIL_RE.match(email):
                raise HTTPException(400, detail=f"Invalid email in {group}: {email!r}")
            valid.append(email)
        # De-duplicate while preserving order.
        seen, deduped = set(), []
        for e in valid:
            if e.lower() not in seen:
                seen.add(e.lower())
                deduped.append(e)
        cleaned[group] = deduped
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS — kept in lock-step with the frontend catalog.
# Anything in LOCKED_TYPES cannot be disabled by the worker; PATCH attempts
# to set `enabled=false` for these are silently ignored.
# ─────────────────────────────────────────────────────────────────────────────
LOCKED_TYPES = {
    # Pay
    "payslip_available", "payroll_issue", "tax_document",
    # Account & Data Security
    "new_signin", "password_changed", "mfa_changed",
    "policy_ack_required", "data_export_ready", "dsar_status",
    "unusual_data_access",
}

CRITICAL_TYPES = {
    "payroll_issue", "new_signin", "password_changed", "mfa_changed",
    "policy_ack_required", "unusual_data_access",
}

VALID_POSTURES = {"essential", "balanced", "detailed", "custom"}

DEFAULT_PREFS: Dict[str, Any] = {
    "posture": "balanced",
    "channels": {"in_app": True, "email": True, "push": True, "sms": False},
    "quiet_hours_enabled": True,
    "quiet_hours_start":   "20:00",
    "quiet_hours_end":     "07:00",
    "timezone":            "UTC",
    "overrides":           {},
}


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
    organization_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> models.AuditLog:
    row = models.AuditLog(
        actor_id=actor_id,
        target_id=target_id,
        organization_id=organization_id,
        entity=entity,
        action=action,
        payload=payload or {},
        ip_address=_client_ip(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _latest_pref_row(db: Session, user_id: int, org_id: Optional[int] = None) -> Optional[models.AuditLog]:
    """Last NotificationPreferenceSet event for this user, scoped by actor_id."""
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == user_id,
            models.AuditLog.entity == "NotificationPreference",
            models.AuditLog.action == "NotificationPreferenceSet",
        )
        .order_by(models.AuditLog.created_at.desc())
        .first()
    )


def _load_prefs(db: Session, user_id: int, org_id: Optional[int] = None) -> Dict[str, Any]:
    row = _latest_pref_row(db, user_id, org_id)
    base = dict(DEFAULT_PREFS)
    if not row or not row.payload:
        return base
    base.update({k: v for k, v in row.payload.items() if k in DEFAULT_PREFS})
    # Always merge channels rather than replace, so a partial save can't drop
    # the locked in_app key.
    base["channels"] = {**DEFAULT_PREFS["channels"], **(row.payload.get("channels") or {})}
    return base


def _normalize_overrides(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Strip any override that targets a locked type or sets enabled=false on it."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        if k in LOCKED_TYPES:
            # Locked rows can't be disabled. Drop any override that tries to.
            if v.get("enabled") is False:
                continue
        out[k] = {kk: vv for kk, vv in v.items() if kk in ("enabled", "channels", "timing")}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SEND ENFORCEMENT (read-side) — spec §2 "Preference and fatigue control"
# -----------------------------------------------------------------------------
# Turns the preferences persisted above into an actual send/suppress decision.
# This is the counterpart to _load_prefs: the UI writes preferences, and this
# reads them to decide whether a routine email may go out.
#
# IMPORTANT — this is INERT until a caller opts in:
#   * It is a pure decision function; it sends nothing and is not wired into
#     email_service or any existing sender.
#   * Only SUPPRESSIBLE classes (operational / customer_success / marketing) are
#     ever gated. Critical / Essential / Commercial-account short-circuit to
#     ALLOW before any preference read — so security, billing, approval, and
#     receipt mail can never be suppressed.
#   * If the recipient can't be identified (no db / no user id) it FAILS OPEN
#     (allows). Existing callers, which pass no context, therefore see no change.
#
# v1 limitation (documented): quiet-hours are evaluated in the `now` timezone
# passed in (UTC by default). Per-recipient timezone conversion is a follow-up;
# it affects no currently-wired sender because none opt in yet.
# ─────────────────────────────────────────────────────────────────────────────
def _in_quiet_hours(prefs: Dict[str, Any], now: datetime) -> bool:
    """True if `now` falls inside the recipient's quiet-hours window."""
    try:
        start = prefs.get("quiet_hours_start") or "20:00"
        end = prefs.get("quiet_hours_end") or "07:00"
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        cur = now.hour * 60 + now.minute
        s = sh * 60 + sm
        e = eh * 60 + em
        if s == e:
            return False
        if s < e:
            return s <= cur < e
        return cur >= s or cur < e  # overnight wrap (e.g. 20:00 -> 07:00)
    except Exception:
        return False


def evaluate_send(
    *,
    to_email: str,
    classification: Optional[str] = None,
    db: Optional[Session] = None,
    recipient_user_id: Optional[int] = None,
    family: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Decide whether a routine email may be sent. Returns (allowed, reason).

    Fails OPEN on any ambiguity. Never blocks non-suppressible classes.
    Unsubscribe-list suppression is intentionally NOT consulted here (that is a
    separate feature); this decision is preference + quiet-hours only.
    """
    cls = (classification or _classification.ESSENTIAL).lower()

    # Non-suppressible classes always send (spec §2).
    if cls not in _classification.SUPPRESSIBLE:
        return True, f"non_suppressible:{cls}"

    # Can't identify the recipient -> can't read prefs -> fail open.
    if db is None or not recipient_user_id:
        return True, "no_recipient_context"

    prefs = _load_prefs(db, recipient_user_id)

    # Email channel switched off entirely.
    if prefs.get("channels", {}).get("email") is False:
        return False, "email_channel_off"

    # Posture "essential" => only essential/critical get through.
    if prefs.get("posture") == "essential":
        return False, "posture_essential"

    # Per-family override toggle.
    override = (prefs.get("overrides") or {}).get(family or "", {})
    if isinstance(override, dict) and override.get("enabled") is False:
        return False, "type_disabled"

    # Quiet hours (suppressible classes only reach here).
    if prefs.get("quiet_hours_enabled") and cls in _classification.QUIET_HOURS_ELIGIBLE:
        if _in_quiet_hours(prefs, now or datetime.now(timezone.utc)):
            return False, "quiet_hours"

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# 1. GET /notifications/preferences
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/preferences")
def get_preferences(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _load_prefs(db, current_user.id, current_user.organization_id)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PATCH /notifications/preferences
#    Each accepted field writes a NotificationPreferenceChanged audit row;
#    the resulting merged state is then snapshotted as
#    NotificationPreferenceSet for fast reads.
# ─────────────────────────────────────────────────────────────────────────────
class PreferencesPatch(BaseModel):
    posture:             Optional[str]                    = None
    channels:            Optional[Dict[str, bool]]        = None
    quiet_hours_enabled: Optional[bool]                   = None
    quiet_hours_start:   Optional[str]                    = Field(None, max_length=5)
    quiet_hours_end:     Optional[str]                    = Field(None, max_length=5)
    timezone:            Optional[str]                    = Field(None, max_length=64)
    overrides:           Optional[Dict[str, Dict[str, Any]]] = None


@router.patch("/preferences")
def patch_preferences(
    body: PreferencesPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    current = _load_prefs(db, current_user.id, org_id)
    delta:  Dict[str, Any] = {}

    if body.posture is not None:
        if body.posture not in VALID_POSTURES:
            raise HTTPException(400, detail=f"posture must be one of {sorted(VALID_POSTURES)}")
        delta["posture"] = body.posture

    if body.channels is not None:
        merged = {**current["channels"], **body.channels}
        merged["in_app"] = True   # never let in_app drop, regardless of payload
        delta["channels"] = merged

    if body.quiet_hours_enabled is not None:
        delta["quiet_hours_enabled"] = bool(body.quiet_hours_enabled)
    if body.quiet_hours_start is not None:
        delta["quiet_hours_start"]   = _validate_hhmm(body.quiet_hours_start, "quiet_hours_start")
    if body.quiet_hours_end is not None:
        delta["quiet_hours_end"]     = _validate_hhmm(body.quiet_hours_end, "quiet_hours_end")
    if body.timezone is not None:
        delta["timezone"]            = _validate_timezone(body.timezone)

    if body.overrides is not None:
        delta["overrides"] = _normalize_overrides(body.overrides)

    if not delta:
        return current  # no-op patch — nothing to audit

    next_state = {**current, **delta}

    # Per-field compliance audit row (spec §12: material preference changes).
    for k, v in delta.items():
        _audit(
            db,
            actor_id=current_user.id,
            target_id=current_user.id,
            organization_id=org_id,
            entity="NotificationPreference",
            action="NotificationPreferenceChanged",
            payload={"field": k, "from": current.get(k), "to": v},
            request=request,
        )

    # Snapshot of the merged state — what GET will read next.
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        organization_id=org_id,
        entity="NotificationPreference",
        action="NotificationPreferenceSet",
        payload=next_state,
        request=request,
    )

    return next_state


# ─────────────────────────────────────────────────────────────────────────────
# 3. GET /notifications/history
#    Returns events the worker is the target/recipient of. Filters: days,
#    category, delivery_status, q (keyword on title/body).
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/history")
def get_history(
    days:     str       = "30",
    category: str       = "all",
    status:   str       = "all",
    q:        str       = "",
    limit:    int       = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    qry = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.entity == "Notification",
            models.AuditLog.target_id == current_user.id,
        )
    )

    if days and days != "all":
        try:
            n = max(1, int(days))
            qry = qry.filter(models.AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(days=n))
        except ValueError:
            pass

    rows = (
        qry.order_by(models.AuditLog.created_at.desc())
        .limit(max(1, min(int(limit or 100), 500)))
        .all()
    )

    out: List[Dict[str, Any]] = []
    needle = q.strip().lower()
    for r in rows:
        p = r.payload or {}
        cat   = p.get("category") or "schedule"
        st    = p.get("delivery_status") or "delivered"
        title = p.get("title") or ""
        body  = p.get("body")  or ""

        if category != "all" and cat != category:
            continue
        if status != "all" and st != status:
            continue
        if needle and needle not in title.lower() and needle not in body.lower():
            continue

        out.append({
            "id":                r.id,
            "title":             title,
            "body":              body,
            "category":          cat,
            "delivery_status":   st,
            "channels":          p.get("channels") or [p.get("channel") or "in_app"],
            "channel":           p.get("channel"),
            "sent_at":           r.created_at.isoformat() if r.created_at else None,
            "read_at":           p.get("read_at"),
            "suppressed_reason": p.get("suppressed_reason"),
            "action_url":        p.get("action_url"),
            "notification_type": p.get("notification_type"),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 4. POST /notifications/test
#    Sends a sample notification only to the signed-in worker. No fan-out.
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/test", status_code=201)
def send_test(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    prefs = _load_prefs(db, current_user.id, org_id)

    # Reflect the worker's enabled channels in the test event so they can
    # verify their setup end-to-end. in_app is always present.
    enabled_channels = ["in_app"]
    for ch in ("email", "push", "sms"):
        if prefs.get("channels", {}).get(ch):
            enabled_channels.append(ch)

    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        organization_id=org_id,
        entity="Notification",
        action="NotificationDelivered",
        payload={
            "title":             "Test notification",
            "body":              "If you can see this, your notification setup is working.",
            "category":          "schedule",
            "delivery_status":   "delivered",
            "channels":          enabled_channels,
            "notification_type": "_test",
            "is_test":           True,
        },
        request=request,
    )
    return {
        "id":        row.id,
        "delivered": True,
        "channels":  enabled_channels,
        "sent_at":   row.created_at.isoformat() if row.created_at else None,
    }


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN — Org-wide notification settings (Platform Settings → Notifications)
# Spec: Temveris Notifications Tab Platform Settings Implementation Wireframe.
# Endpoints
# ---------
# GET    /admin/notifications/settings    Read org-wide settings.
# PATCH  /admin/notifications/settings    Update org-wide settings.
# POST   /admin/notifications/test        Send test notification to caller.
# GET    /admin/notifications/event-log   Recent delivery events for audit.
#
# Storage: same AuditLog-row strategy as worker prefs. The org-level row is
# keyed by entity="OrgNotificationPreference" with target_id=NULL so it's
# distinct from per-worker preference snapshots. organization_id scopes every
# read and write so org A settings can never leak to org B. When dedicated
# tables ship, swap the bodies and the frontend keeps working.
# ═════════════════════════════════════════════════════════════════════════════

admin_router = APIRouter(prefix="/admin/notifications", tags=["Admin Notifications"])


def _require_admin_role(user: models.User):
    if user.role not in ("admin", "hr", "owner", "payroll_admin", "finance_admin"):
        raise HTTPException(403, "Only Account Owner / Admin / HR / Finance Admin can manage org notification settings")


ORG_DEFAULT_SETTINGS: Dict[str, Any] = {
    "posture": "balanced",
    "overrides": {},
    "channels": {
        "in_app": True, "email": True, "push": False, "sms": False,
    },
    "quiet_hours_enabled":         True,
    "quiet_hours_start":           "20:00",
    "quiet_hours_end":             "07:00",
    "quiet_hours_timezone":        "workspace",
    "quiet_hours_critical_bypass": True,
    "custom_recipients":           {},
}


def _latest_org_settings_row(db: Session, org_id: int) -> Optional[models.AuditLog]:
    """Last OrgNotificationPreferenceSet event for this organization."""
    _org_admin_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == org_id)
        .all()
    ]
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id.in_(_org_admin_ids),
            models.AuditLog.entity == "OrgNotificationPreference",
            models.AuditLog.action == "OrgNotificationPreferenceSet",
        )
        .order_by(models.AuditLog.created_at.desc())
        .first()
    )


def _load_org_settings(db: Session, org_id: int) -> Dict[str, Any]:
    row = _latest_org_settings_row(db, org_id)
    base = dict(ORG_DEFAULT_SETTINGS)
    if not row or not row.payload:
        return base
    base.update({k: v for k, v in row.payload.items() if k in ORG_DEFAULT_SETTINGS})
    base["channels"] = {**ORG_DEFAULT_SETTINGS["channels"], **(row.payload.get("channels") or {})}
    base["channels"]["in_app"] = True   # always-on, non-negotiable per spec §8
    return base


class OrgSettingsPatch(BaseModel):
    posture:                      Optional[str]                       = None
    overrides:                    Optional[Dict[str, Dict[str, Any]]] = None
    channels:                     Optional[Dict[str, bool]]           = None
    quiet_hours_enabled:          Optional[bool]                      = None
    quiet_hours_start:            Optional[str]                       = Field(None, max_length=5)
    quiet_hours_end:              Optional[str]                       = Field(None, max_length=5)
    quiet_hours_timezone:         Optional[str]                       = Field(None, max_length=64)
    quiet_hours_critical_bypass:  Optional[bool]                      = None
    custom_recipients:            Optional[Dict[str, List[str]]]      = None


@admin_router.get("/settings")
def get_org_notification_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin_role(current_user)
    return _load_org_settings(db, current_user.organization_id)


@admin_router.patch("/settings")
def patch_org_notification_settings(
    body: OrgSettingsPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin_role(current_user)
    org_id = current_user.organization_id
    current = _load_org_settings(db, org_id)
    delta: Dict[str, Any] = {}

    if body.posture is not None:
        if body.posture not in VALID_POSTURES:
            raise HTTPException(400, detail=f"posture must be one of {sorted(VALID_POSTURES)}")
        delta["posture"] = body.posture
    if body.overrides is not None:
        delta["overrides"] = body.overrides
    if body.channels is not None:
        merged = {**current["channels"], **body.channels}
        merged["in_app"] = True   # spec: in-app always-on
        delta["channels"] = merged
    if body.quiet_hours_enabled is not None:
        delta["quiet_hours_enabled"] = bool(body.quiet_hours_enabled)
    if body.quiet_hours_start is not None:
        delta["quiet_hours_start"] = _validate_hhmm(body.quiet_hours_start, "quiet_hours_start")
    if body.quiet_hours_end is not None:
        delta["quiet_hours_end"] = _validate_hhmm(body.quiet_hours_end, "quiet_hours_end")
    if body.quiet_hours_timezone is not None:
        delta["quiet_hours_timezone"] = _validate_timezone(body.quiet_hours_timezone)
    if body.quiet_hours_critical_bypass is not None:
        delta["quiet_hours_critical_bypass"] = bool(body.quiet_hours_critical_bypass)
    if body.custom_recipients is not None:
        delta["custom_recipients"] = _validate_recipients(body.custom_recipients)

    if not delta:
        return current  # no-op patch — nothing to audit

    next_state = {**current, **delta}

    # Per-field audit row (spec §16: material settings changes).
    for k, v in delta.items():
        _audit(
            db,
            actor_id=current_user.id,
            target_id=None,                     # org-wide change, no individual target
            organization_id=org_id,
            entity="OrgNotificationPreference",
            action="OrgNotificationPreferenceChanged",
            payload={"field": k, "from": current.get(k), "to": v},
            request=request,
        )

    # Snapshot for fast reads.
    _audit(
        db,
        actor_id=current_user.id,
        target_id=None,
        organization_id=org_id,
        entity="OrgNotificationPreference",
        action="OrgNotificationPreferenceSet",
        payload=next_state,
        request=request,
    )

    return next_state


@admin_router.post("/test")
def send_admin_test_notification(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Send a test notification only to the signed-in administrator.
    Per spec §17: must NOT fan out to all configured recipients.
    """
    _require_admin_role(current_user)
    org_id = current_user.organization_id
    settings = _load_org_settings(db, org_id)
    enabled_channels = [k for k, v in settings.get("channels", {}).items() if v]
    if "in_app" not in enabled_channels:
        enabled_channels.append("in_app")

    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        organization_id=org_id,
        entity="OrgNotificationEvent",
        action="OrgNotificationTestSent",
        payload={
            "channels":          enabled_channels,
            "notification_type": "_admin_test",
            "is_test":           True,
        },
        request=request,
    )
    return {
        "id":        row.id,
        "delivered": True,
        "channels":  enabled_channels,
        "sent_at":   row.created_at.isoformat() if row.created_at else None,
    }


@admin_router.get("/event-log")
def get_admin_event_log(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Recent org-level notification delivery events for audit.
    Returns the latest N OrgNotificationEvent / OrgNotificationPreference rows
    scoped to the caller's organization — cross-org rows are never returned.
    """
    _require_admin_role(current_user)
    limit = max(1, min(int(limit), 200))
    _event_log_user_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == current_user.organization_id)
        .all()
    ]
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id.in_(_event_log_user_ids),
            models.AuditLog.entity.in_([
                "OrgNotificationEvent", "OrgNotificationPreference",
            ]),
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    events = []
    for r in rows:
        payload = r.payload or {}
        events.append({
            "id":                r.id,
            "notification_type": payload.get("notification_type") or payload.get("field") or r.action,
            "channel":           ", ".join(payload.get("channels") or []) if payload.get("channels") else "—",
            "recipient":         "self" if r.target_id == r.actor_id else (str(r.target_id or "—")),
            "sent_at":           r.created_at.isoformat() if r.created_at else None,
            "delivery_status":   "delivered" if r.action.endswith("Sent") else "config_change",
            "actor_id":          r.actor_id,
            "action":            r.action,
        })
    return {"events": events}


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC UNSUBSCRIBE ENDPOINTS  (no auth — recipient may not be logged in)
# -----------------------------------------------------------------------------
# Backs the List-Unsubscribe header (wired in Phase 4B). Abuse is prevented by
# the HMAC token in the URL (suppression.make_token): you can only unsubscribe
# an address if you hold a link Temveris actually sent to it.
#
# Per the Commercial Email Communications Estate, unsubscribe applies to
# MARKETING / subscribed messages only — essential account, security,
# attendance, approval, and billing communications are always delivered.
#
# Registered separately in main.py as `public_router` so these routes are NOT
# gated by the authenticated `router` above.
# ═════════════════════════════════════════════════════════════════════════════
public_router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _unsub_page(title: str, message: str, ok: bool = True) -> str:
    accent = "#059669" if ok else "#DC2626"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Temveris</title></head>
<body style="margin:0;background:#F1F5F9;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:520px;margin:64px auto;background:#fff;border-radius:16px;padding:40px;text-align:center;">
    <h1 style="color:#0F172A;font-size:22px;margin:0 0 12px;">{title}</h1>
    <p style="color:#475569;font-size:15px;line-height:1.6;margin:0;">{message}</p>
    <p style="color:{accent};font-size:13px;margin-top:20px;">This affects Temveris marketing email only. Essential account, security, attendance, approval, and billing communications are always delivered.</p>
  </div>
</body></html>"""


@public_router.post("/unsubscribe")
def one_click_unsubscribe(
    email: str = Query(...),
    token: str = Query(""),
    scope: str = Query("marketing"),
    db: Session = Depends(get_db),
):
    """RFC 8058 one-click POST target."""
    if not _suppression.verify_token(email, token):
        return {"ok": False, "error": "invalid_or_expired_token"}
    _suppression.suppress(db, email, scope=scope, source="one_click")
    return {"ok": True, "email": email.strip().lower(), "status": "unsubscribed", "scope": scope}


@public_router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_landing(
    email: str = Query(...),
    token: str = Query(""),
    db: Session = Depends(get_db),
):
    """Human landing page. A click also honors the request (one-click friendly)."""
    if not _suppression.verify_token(email, token):
        return HTMLResponse(
            _unsub_page("Link not valid", "This unsubscribe link is invalid or has expired.", ok=False),
            status_code=400,
        )
    _suppression.suppress(db, email, scope="marketing", source="one_click")
    return HTMLResponse(
        _unsub_page(
            "You're unsubscribed",
            f"<strong>{email.strip().lower()}</strong> will no longer receive Temveris marketing email.",
            ok=True,
        )
    )


@public_router.post("/resubscribe")
def resubscribe(
    email: str = Query(...),
    token: str = Query(""),
    db: Session = Depends(get_db),
):
    """Undo a prior unsubscribe (re-opt-in)."""
    if not _suppression.verify_token(email, token):
        return {"ok": False, "error": "invalid_or_expired_token"}
    _suppression.resubscribe(db, email)
    return {"ok": True, "email": email.strip().lower(), "status": "resubscribed"}


@public_router.get("/unsubscribe/status")
def unsubscribe_status(
    email: str = Query(...),
    token: str = Query(""),
    db: Session = Depends(get_db),
):
    """Read current suppression state (used by the frontend page)."""
    if not _suppression.verify_token(email, token):
        return {"ok": False, "error": "invalid_or_expired_token"}
    return {
        "ok": True,
        "email": email.strip().lower(),
        "unsubscribed": _suppression.is_unsubscribed_any(db, email),
    }
