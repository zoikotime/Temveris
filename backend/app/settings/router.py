"""
Temveris – Settings Router
============================
Company-wide configurable settings stored in Supabase.
Applied dynamically to desktop app and risk engine.

Only settings that are *actually consumed* by the application are kept here.
Everything else has been removed in the cleanup pass — orphan settings
were never read by any code path and only made the save loop slow.

Endpoints:
  GET  /settings          – get all settings (key-value map)
  PUT  /settings/update   – update one or more settings (admin only)
  GET  /settings/defaults – return factory defaults without DB
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.service import get_current_user
from app.settings import scope as settings_scope
from database import models
from database.database import get_db
from websocket_manager import manager

router = APIRouter(prefix="/settings", tags=["Settings"])

# ── Factory defaults — only keys consumed by real code paths ─────────────────
# screenshot_enabled / screenshot_interval / screenshot_blur / idle_threshold_minutes
#   → desktop tracker (main.js)
# work_hours_threshold / risk_alert_enabled → backend risk router
DEFAULTS: Dict[str, Any] = {
    "screenshot_enabled":     "true",   # desktop tracker — enable screen capture
    "screenshot_interval":    "1800",   # desktop tracker — seconds between screenshots (1800 = 30 min)
    "screenshot_blur":        "false",  # desktop tracker — apply privacy blur to screenshots
    "idle_threshold_minutes": "15",     # desktop tracker — minutes idle before "Idle" status
    "work_hours_threshold":   "8:45",   # risk router — minimum daily hours (HH:MM)
    "risk_alert_enabled":     "true",   # risk router — send HR alert when below threshold
    # Activity-tracking flags → desktop tracker (main.js / browser-tracker.js).
    # All default ON to preserve today's always-on behaviour.
    "track_mouse":            "true",   # count mouse clicks / moves / scrolls
    "track_keyboard":         "true",   # count keystrokes
    "track_apps":             "true",   # capture foreground application name
    "track_browser":          "true",   # capture browser tab / domain activity
    "track_urls":             "true",   # capture full URL (off → domain only)
}

DESCRIPTIONS: Dict[str, str] = {
    "screenshot_enabled":     "Enable screen capture on the desktop tracker (true/false).",
    "screenshot_interval":    "Seconds between screenshots taken by the desktop tracker.",
    "screenshot_blur":        "Apply a privacy blur to captured screenshots before upload (true/false).",
    "idle_threshold_minutes": "Minutes of no keyboard/mouse activity before the user is marked idle.",
    "work_hours_threshold":   "Minimum work hours per day (HH:MM). Risk alert fires below this.",
    "risk_alert_enabled":     "Send HR risk alert email when below threshold (true/false).",
    "track_mouse":            "Record mouse activity (clicks, movement, scroll) on the desktop tracker.",
    "track_keyboard":         "Record keystroke counts on the desktop tracker.",
    "track_apps":             "Record which application is in the foreground.",
    "track_browser":          "Record browser tab / domain activity.",
    "track_urls":             "Record the full page URL. Turn off to keep only the domain.",
}

# Active Time Engine tunables (weights + thresholds). Declared here so the
# admin PUT validator accepts them — see app/active_time/config.py.
try:
    from app.active_time.config import SETTING_DEFAULTS as _AT_DEFAULTS, SETTING_DESCRIPTIONS as _AT_DESCRIPTIONS
    DEFAULTS.update(_AT_DEFAULTS)
    DESCRIPTIONS.update(_AT_DESCRIPTIONS)
    # Active Time scoring is computed by a per-ORGANIZATION worker
    # (active_time.service -> load_config(db, org_id)), which reads the org
    # tier. Marking these org_only stops the admin UI offering — and the
    # scoped write endpoint accepting — a team/employee override the scorer
    # would never consult.
    for _k in _AT_DEFAULTS:
        settings_scope.KEY_META.setdefault(_k, {})["org_only"] = True
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Workforce-alert audience. Declared here so it participates in the same
# employee > team > org > default cascade as everything else — a team override
# routes that team's alerts to the team's own inbox. Empty by default: the
# resolution order in app/notifications/recipients.py falls through to the
# workspace HR/support email and then to the organization's own admin/HR users.
# There is deliberately no global default address.
try:
    from app.notifications.recipients import (
        SETTING_DEFAULTS as _NR_DEFAULTS,
        SETTING_DESCRIPTIONS as _NR_DESCRIPTIONS,
        SETTING_ALERT_RECIPIENTS as _NR_KEY,
        SETTING_LEAVE_RECIPIENTS as _NR_LEAVE_KEY,
    )
    DEFAULTS.update(_NR_DEFAULTS)
    DESCRIPTIONS.update(_NR_DESCRIPTIONS)
    settings_scope.KEY_META[_NR_KEY] = {
        "group": "Notifications", "label": "Alert recipients", "type": "text",
        "placeholder": "hr@example.com, ops@example.com",
    }
    settings_scope.KEY_META[_NR_LEAVE_KEY] = {
        "group": "Notifications", "label": "Leave recipients", "type": "text",
        "placeholder": "hr@example.com",
    }
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Controlled Clock-Out policy foundation (Phase 1). Declared here so the admin
# PUT validator accepts them and GET /settings returns them — see
# app/attendance_policy/resolvers.py. early_clockout_enabled /
# disconnect_detection_enabled are future kill switches (OFF); nothing reads
# them yet.
try:
    from app.attendance_policy import SETTING_DEFAULTS as _AP_DEFAULTS, SETTING_DESCRIPTIONS as _AP_DESCRIPTIONS
    DEFAULTS.update(_AP_DEFAULTS)
    DESCRIPTIONS.update(_AP_DESCRIPTIONS)
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Payroll policy — the LWP daily-rate divisor and whether an unpaid-leave span
# consumes non-working days. Both were literals in payroll/service.py.
try:
    from app.payroll.policy import (
        SETTING_DEFAULTS as _PP_DEFAULTS,
        SETTING_DESCRIPTIONS as _PP_DESCRIPTIONS,
    )
    DEFAULTS.update(_PP_DEFAULTS)
    DESCRIPTIONS.update(_PP_DESCRIPTIONS)
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Which named accounts are not employees (the seeded owner/CEO seat). Was a
# module constant matching "Administrator" for every tenant alike.
try:
    from app._system_admin import (
        SETTING_DEFAULTS as _SA_DEFAULTS,
        SETTING_DESCRIPTIONS as _SA_DESCRIPTIONS,
    )
    DEFAULTS.update(_SA_DEFAULTS)
    DESCRIPTIONS.update(_SA_DESCRIPTIONS)
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Anti-fake-activity was REMOVED in Phase 5 (see desktop-tracker/main.js).
# `integrity.enabled` / `integrity.automation_processes` are deliberately NOT
# registered: the tracker no longer reads them and the backend never consumed
# the detections, so registering them would advertise a control that does
# nothing — the exact failure mode this programme has been closing.

# Leave review policy — the sick-leave frequency threshold and the
# long-weekend pattern flag, both of which were literals in leave/router.py.
try:
    from app.leave.policy import (
        SETTING_DEFAULTS as _LV_DEFAULTS,
        SETTING_DESCRIPTIONS as _LV_DESCRIPTIONS,
    )
    DEFAULTS.update(_LV_DEFAULTS)
    DESCRIPTIONS.update(_LV_DESCRIPTIONS)
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Data-retention policy. Screenshot/activity retention used to be one global
# RETENTION_DAYS environment variable applied to every tenant's data, which a
# customer with a shorter statutory retention could not comply with.
try:
    from app.storage.retention import (
        SETTING_DEFAULTS as _RT_DEFAULTS,
        SETTING_DESCRIPTIONS as _RT_DESCRIPTIONS,
    )
    DEFAULTS.update(_RT_DEFAULTS)
    DESCRIPTIONS.update(_RT_DESCRIPTIONS)
except Exception:  # pragma: no cover — never let the import break settings
    pass

# Work Schedule Policy keys (P4 — configuration model only). Declared here so
# the admin PUT validator accepts them and GET /settings returns them through the
# existing employee > team > org > default cascade — see
# app/schedule_policy/keys.py. NOTHING reads them yet: the resolver lands in P5
# and every consumer stays behind schedule.engine_enabled (false) until rollout.
# Deliberately NOT added to settings.scope.KEY_META, so they do not render in the
# admin UI until P8 ships the multiselect / shift_ref / time controls.
try:
    from app.schedule_policy import SETTING_DEFAULTS as _SP_DEFAULTS, SETTING_DESCRIPTIONS as _SP_DESCRIPTIONS
    DEFAULTS.update(_SP_DEFAULTS)
    DESCRIPTIONS.update(_SP_DESCRIPTIONS)
    # P8 — render the Working Schedule section in the Organization Settings admin
    # UI. KEY_META is what makes a setting appear (scoped_view iterates it), so
    # this merge is what turns the P4 keys into visible rows. Merged HERE rather
    # than in scope.py because scope.py importing schedule_policy would create a
    # scope -> resolver -> scope cycle; by this point both modules are fully
    # loaded. Only the 13 curated rows are contributed: engine_enabled,
    # shadow_mode_enabled, absent_after_hour and work_mode are deliberately
    # withheld so they stay deliverable by GET /settings but never rendered.
    from app.schedule_policy.keys import KEY_META_CONTRIBUTION as _SP_META
    settings_scope.KEY_META.update(_SP_META)
except Exception:  # pragma: no cover — never let the import break settings
    pass


def _all_settings(db: Session, org_id: int) -> Dict[str, str]:
    """Org-level effective settings (system default overlaid with org rows).
    Org level = team_id IS NULL AND user_id IS NULL. Used by the admin policies
    aggregate and the org-scope PUT return."""
    return settings_scope.org_settings(db, org_id, DEFAULTS)


# ── GET /settings ─────────────────────────────────────────────────────────────
@router.get("")
def get_settings(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Return the *effective* settings for the calling user, resolved through the
    employee > team > org > default cascade. The desktop tracker and dashboards
    poll this, so a team/employee override reaches them with no client change."""
    return {
        "settings":     settings_scope.resolve_effective(db, user, DEFAULTS),
        "descriptions": DESCRIPTIONS,
    }


# ── GET /settings/defaults ────────────────────────────────────────────────────
@router.get("/defaults")
def get_defaults(_: models.User = Depends(get_current_user)):
    return {"defaults": DEFAULTS, "descriptions": DESCRIPTIONS}


# ── PUT /settings/update ──────────────────────────────────────────────────────
class SettingsPayload(BaseModel):
    settings: Dict[str, str]


def _guard_workweek_boundary_change(db: Session, org_id, incoming) -> None:
    """Refuse a week-start change that would take effect mid-workweek.

    The ECO workweek boundary is the one setting whose change cannot simply
    take effect: days already sealed this week carry the outgoing identity and
    days sealed afterwards would carry the incoming one, leaving one physical
    week folding and completing twice. The guard lives in
    ``work_ledger.workweek`` — this only translates its refusal into the
    transport the settings API speaks.
    """
    from app.work_ledger.workweek import (
        WEEK_START_SETTING_KEY,
        WorkweekBoundaryChangeRefused,
        WorkweekBoundaryError,
        assert_boundary_change_allowed,
    )

    if WEEK_START_SETTING_KEY not in (incoming or {}):
        return
    try:
        assert_boundary_change_allowed(
            db, organization_id=org_id,
            new_value=incoming[WEEK_START_SETTING_KEY],
        )
    except WorkweekBoundaryChangeRefused as exc:
        # 409: the request is well-formed, the current state refuses it.
        raise HTTPException(409, str(exc))
    except WorkweekBoundaryError as exc:
        raise HTTPException(400, str(exc))


@router.put("/update")
def update_settings(
    body:  SettingsPayload,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can update settings")

    # Validate first — reject the whole payload if any key is unknown
    incoming = body.settings or {}
    unknown  = [k for k in incoming if k not in DEFAULTS]
    if unknown:
        raise HTTPException(400, f"Unknown setting key(s): {', '.join(unknown)}")

    org_id = admin.organization_id
    if not incoming:
        return {"success": True, "updated": [], "settings": _all_settings(db, org_id)}

    _guard_workweek_boundary_change(db, org_id, incoming)

    # Bulk-fetch existing rows in a single query, then upsert in memory.
    keys     = list(incoming.keys())
    existing = {
        r.key: r
        for r in db.query(models.AppSettings)
                   .filter(
                       models.AppSettings.organization_id == org_id,
                       models.AppSettings.key.in_(keys),
                       models.AppSettings.team_key.is_(None),  # org level only
                       models.AppSettings.user_id.is_(None),
                   )
                   .all()
    }
    now = datetime.now(timezone.utc)

    for key, value in incoming.items():
        row = existing.get(key)
        if row:
            row.value      = value
            row.updated_by = admin.id
            row.updated_at = now
        else:
            db.add(models.AppSettings(
                organization_id=org_id,
                key=key, value=value,
                description=DESCRIPTIONS.get(key, ""),
                updated_by=admin.id,
            ))

    db.commit()

    # Build the new merged settings map locally — no second SELECT needed.
    new_settings = {
        key: incoming.get(key, existing[key].value if key in existing else str(default))
        for key, default in DEFAULTS.items()
    }

    # Push the change to every dashboard in this org so the UI reflects it
    # instantly. Best-effort: never let a broadcast failure block the response.
    try:
        if admin.organization_id:
            loop = asyncio.get_event_loop()
            loop.create_task(manager.broadcast_to_org(admin.organization_id, json.dumps({
                "type":            "settings_updated",
                "organization_id": admin.organization_id,
                "settings":        new_settings,
            })))
    except Exception:
        pass

    return {"success": True, "updated": list(incoming.keys()), "settings": new_settings}


# ── Scoped overrides (team / employee) ────────────────────────────────────────
# The Organization Settings admin UI reads/writes overrides at a specific tier.
# Org-level edits keep using PUT /settings/update above; these handle the two
# override tiers plus reset-to-parent.

def _require_admin(user: models.User) -> None:
    if user.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can manage settings")


def _resolve_scope(db: Session, org_id: int, scope: str, team, user_id):
    """Validate a scope request and return (store_team_key, store_user_id,
    view_team_key) — view_team_key lets the employee view show team inheritance.
    Team scope keys on the team NAME string (== User.team membership)."""
    if scope == "org":
        return None, None, None
    if scope == "team":
        tkey = (team or "").strip()
        if not tkey:
            raise HTTPException(400, "team is required for team scope")
        return tkey, None, None
    if scope == "employee":
        if not user_id:
            raise HTTPException(400, "user_id is required for employee scope")
        target = db.query(models.User).filter(
            models.User.id == user_id, models.User.organization_id == org_id).first()
        if not target:
            raise HTTPException(404, "Employee not found")
        return None, user_id, settings_scope.team_key_for_user(db, target)
    raise HTTPException(400, f"Unknown scope '{scope}'")


def _broadcast_scope_changed(org_id: int) -> None:
    """Tell dashboards to re-fetch /settings. Running trackers also re-poll every
    60s, so an override reaches them without a live push."""
    try:
        if org_id:
            loop = asyncio.get_event_loop()
            loop.create_task(manager.broadcast_to_org(org_id, json.dumps({
                "type": "settings_updated", "organization_id": org_id, "scoped": True,
            })))
    except Exception:
        pass


@router.get("/scoped")
def get_scoped_settings(
    scope:   str = "org",
    team:    str = None,
    user_id: int = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Per-key view for the admin UI: value at this scope (if overridden),
    inherited parent value, and metadata for rendering the control."""
    _require_admin(user)
    org_id = user.organization_id
    st, su, view_team = _resolve_scope(db, org_id, scope, team, user_id)
    keys = settings_scope.scoped_view(
        db, org_id, DEFAULTS, DESCRIPTIONS,
        scope=scope, team_key=(view_team if scope == "employee" else st), user_id=su,
    )
    return {"scope": scope, "team": team, "user_id": user_id,
            "group_order": settings_scope.GROUP_ORDER, "keys": keys}


class ScopedPayload(BaseModel):
    scope:    str
    # Optional[...] is load-bearing, not decoration. Under Pydantic 2 a field
    # annotated `str` with a `None` default is still REQUIRED to be a string
    # when the key is present — the default only applies when the key is
    # absent. The admin UI always sends both keys, explicitly null for the
    # scopes they do not apply to (scopedBody in OrganizationSettingsTab.jsx),
    # so every save was rejected with 422 before reaching the handler:
    #   org      -> team=null AND user_id=null  -> 2 errors
    #   team     -> user_id=null                -> 1 error
    #   employee -> team=null                   -> 1 error
    # That is why the database has never held a single schedule.* row.
    team:     Optional[str] = None
    user_id:  Optional[int] = None
    settings: Dict[str, str] = {}


@router.put("/scoped")
def put_scoped_settings(body: ScopedPayload, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    """Upsert override rows at a specific scope. Org scope is delegated to the
    existing /settings/update semantics (team/user NULL)."""
    _require_admin(user)
    org_id = user.organization_id
    incoming = body.settings or {}
    unknown = [k for k in incoming if k not in DEFAULTS]
    if unknown:
        raise HTTPException(400, f"Unknown setting key(s): {', '.join(unknown)}")

    # Reject an override at a scope whose CONSUMER cannot see it. Accepting the
    # write, audit-logging it and rendering it as an active override while the
    # code reads the org tier is worse than a 400: the admin believes the
    # policy changed and it did not.
    if body.scope != "org":
        org_only = [k for k in incoming if settings_scope.is_org_only(k)]
        if org_only:
            raise HTTPException(
                400,
                f"These settings are organization-wide only and cannot be "
                f"overridden per {body.scope}: {', '.join(sorted(org_only))}",
            )

    _guard_workweek_boundary_change(db, org_id, incoming)

    st, su, _ = _resolve_scope(db, org_id, body.scope, body.team, body.user_id)

    existing = {
        r.key: r
        for r in db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key.in_(list(incoming.keys()) or [""]),
            (models.AppSettings.team_key == st) if st is not None else models.AppSettings.team_key.is_(None),
            (models.AppSettings.user_id == su) if su is not None else models.AppSettings.user_id.is_(None),
        ).all()
    }
    now = datetime.now(timezone.utc)
    for key, value in incoming.items():
        row = existing.get(key)
        if row:
            row.value, row.updated_by, row.updated_at = value, user.id, now
        else:
            db.add(models.AppSettings(
                organization_id=org_id, team_key=st, user_id=su,
                key=key, value=value, description=DESCRIPTIONS.get(key, ""),
                updated_by=user.id,
            ))
    if incoming:
        db.add(models.AuditLog(
            organization_id=org_id, actor_id=user.id, target_id=su,
            entity="Settings", action="ScopedSet",
            payload={"scope": body.scope, "team": st, "user_id": su,
                     "keys": list(incoming.keys())},
        ))
    db.commit()
    _broadcast_scope_changed(org_id)
    return {"success": True, "updated": list(incoming.keys())}


class ScopedResetPayload(BaseModel):
    scope:   str
    # Same Pydantic 2 nullability rule as ScopedPayload above — "Reset to
    # inherited" sends the identical scopedBody() shape, so it was 422ing too.
    team:    Optional[str] = None
    user_id: Optional[int] = None
    keys:    list = []


@router.delete("/scoped")
def delete_scoped_settings(body: ScopedResetPayload, db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """Delete override rows at a scope → those keys fall back to the parent."""
    _require_admin(user)
    org_id = user.organization_id
    st, su, _ = _resolve_scope(db, org_id, body.scope, body.team, body.user_id)
    keys = [k for k in (body.keys or []) if k in DEFAULTS]
    if not keys:
        return {"success": True, "reset": []}
    deleted = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key.in_(keys),
        (models.AppSettings.team_key == st) if st is not None else models.AppSettings.team_key.is_(None),
        (models.AppSettings.user_id == su) if su is not None else models.AppSettings.user_id.is_(None),
    ).delete(synchronize_session=False)
    db.add(models.AuditLog(
        organization_id=org_id, actor_id=user.id, target_id=su,
        entity="Settings", action="ScopedReset",
        payload={"scope": body.scope, "team": st, "user_id": su, "keys": keys},
    ))
    db.commit()
    _broadcast_scope_changed(org_id)
    return {"success": True, "reset": keys, "deleted": deleted}
