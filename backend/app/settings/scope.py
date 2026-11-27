"""
Settings scope cascade — employee > team > org > system default.
=================================================================
The desktop tracker, dashboards and backend consumers all read settings through
``GET /settings``. This module resolves the *effective* value for a given user
by walking the cascade, and exposes the per-scope read/write helpers the admin
Organization Settings UI uses to manage overrides.

Storage is the existing ``app_settings`` key/value table (see database/models.py)
extended with nullable ``team_key`` / ``user_id`` columns:
  - org level      → team_key IS NULL AND user_id IS NULL
  - team level     → team_key = <team name> AND user_id IS NULL
  - employee level → user_id = <user>   (team_key NULL)

Team membership is the ``User.team`` name string (there is no teams.id FK on
users), so team scope keys on that same string.

Only keys present in ``DEFAULTS`` are ever read or written — the same allow-list
the settings router validates against.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database import models

# KEY_META drives the generic admin UI: which group a setting belongs to, its
# human label, the control type, and (for selects) the allowed options. Only
# keys the platform actually consumes today are listed — new keys get added here
# as their consumers are wired, so the UI never shows a setting nothing reads.
#   type: "bool" | "int" | "time" | "enum"
KEY_META: Dict[str, Dict[str, Any]] = {
    "screenshot_enabled": {
        "group": "Screenshot Monitoring", "label": "Enable screenshots",
        "type": "bool",
    },
    "screenshot_interval": {
        "group": "Screenshot Monitoring", "label": "Screenshot interval",
        "type": "enum", "unit": "seconds",
        "options": ["60", "120", "300", "600", "900", "1800", "3600"],
        "option_labels": {
            "60": "Every 1 min", "120": "Every 2 min", "300": "Every 5 min",
            "600": "Every 10 min", "900": "Every 15 min", "1800": "Every 30 min",
            "3600": "Every 1 hour",
        },
    },
    "screenshot_blur": {
        "group": "Screenshot Monitoring", "label": "Blur sensitive data",
        "type": "bool",
    },
    "idle_threshold_minutes": {
        "group": "Attendance", "label": "Idle timeout (minutes)",
        "type": "int", "min": 1, "max": 120,
    },
    "work_hours_threshold": {
        # DEPRECATED legacy fallback, consulted only when
        # schedule.expected_work_hours is unconfigured, and read at the ORG
        # tier by attendance_policy._get_setting_value. Marked org_only so the
        # admin UI stops offering a team/employee override that the resolver
        # would ignore — use schedule.expected_work_hours, which cascades.
        "org_only": True,
        "group": "Working Hours", "label": "Daily work hours threshold",
        "type": "enum",
        "options": ["6:00", "6:30", "7:00", "7:30", "8:00", "8:30", "8:45", "9:00", "9:30", "10:00"],
    },
    "risk_alert_enabled": {
        "group": "Notifications", "label": "Low-hours risk alert to HR",
        "type": "bool",
    },
    "track_mouse": {
        "group": "Activity Tracking", "label": "Mouse tracking", "type": "bool",
    },
    "track_keyboard": {
        "group": "Activity Tracking", "label": "Keyboard tracking", "type": "bool",
    },
    "track_apps": {
        "group": "Activity Tracking", "label": "Application tracking", "type": "bool",
    },
    "track_browser": {
        "group": "Activity Tracking", "label": "Browser tracking", "type": "bool",
    },
    "track_urls": {
        "group": "Activity Tracking", "label": "URL tracking (off = domain only)", "type": "bool",
    },
}

# NOTE: the Work Schedule Policy rows are merged into KEY_META by
# app/settings/router.py, next to where that module already merges the schedule
# DEFAULTS/DESCRIPTIONS. Doing it here instead would create an import cycle —
# scope -> schedule_policy -> resolver -> scope — which happens to work today
# only because the resolver uses this module lazily, and would silently lose
# every schedule row (swallowed by a try/except) the moment someone added a
# from-import. See router.py.

# Group display order for the UI. "Working Schedule" leads because it is the
# section admins configure first; the existing groups keep their relative order.
GROUP_ORDER = ["Working Schedule", "Working Hours", "Attendance",
               "Screenshot Monitoring", "Activity Tracking", "Notifications"]


# Keys whose CONSUMER only ever reads the organization row, for keys that are
# NOT in KEY_META. The `org_only` flag inside KEY_META covers the keys this
# admin UI renders; it cannot cover a key deliberately kept out of the UI, and
# `KEY_META.get(k, {}).get("org_only")` silently returns falsy for one — which
# is how `schedule.week_start_day` came to accept a per-employee write that
# nothing would ever read.
#
# Listing it here rather than adding it to KEY_META keeps the refusal without
# surfacing the control in the admin UI, which the schedule-policy keys defer
# to P8 along with the three control types that section still needs.
ORG_ONLY_KEYS = frozenset({
    # The ECO workweek boundary. Organization-level by owner ruling: one
    # boundary per organization, for everyone in it. `week_start_date()` and
    # `work_ledger.workweek` both read the organization row only, so an
    # override at any narrower scope would change nothing.
    "schedule.week_start_day",
})


def is_org_only(key: str) -> bool:
    """May this setting only be written at organization scope?

    One answer for both sources, so a caller cannot consult the flag and miss
    the list. Advertising a scope the behaviour does not honour is worse than
    not offering it — the principle `put_scoped_settings` already states.
    """
    if key in ORG_ONLY_KEYS:
        return True
    return bool(KEY_META.get(key, {}).get("org_only"))


def team_key_for_user(db: Session, user: models.User) -> Optional[str]:
    """The team a user belongs to, as the ``User.team`` name string. This is the
    real membership field (there is no teams.id FK on users)."""
    t = (getattr(user, "team", None) or "").strip()
    return t or None


def _rows_for_scope(db: Session, org_id: int, keys, *, team_key=None, user_id=None) -> Dict[str, str]:
    """Fetch the raw override rows that exist at exactly one scope level."""
    q = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key.in_(keys),
    )
    # Match the exact scope: use IS NULL, not == None comparisons.
    q = q.filter(models.AppSettings.team_key == team_key) if team_key is not None \
        else q.filter(models.AppSettings.team_key.is_(None))
    q = q.filter(models.AppSettings.user_id == user_id) if user_id is not None \
        else q.filter(models.AppSettings.user_id.is_(None))
    return {r.key: r.value for r in q.all()}


def org_settings(db: Session, org_id: int, defaults: Dict[str, Any]) -> Dict[str, str]:
    """Org-level effective map: system default overlaid with org rows.
    This is what admin 'org scope' editing and legacy org consumers see."""
    rows = _rows_for_scope(db, org_id, defaults.keys())
    return {k: rows.get(k, str(v)) for k, v in defaults.items()}


def merge_tiers(
    defaults: Dict[str, Any],
    *,
    org: Optional[Dict[str, str]] = None,
    team: Optional[Dict[str, str]] = None,
    employee: Optional[Dict[str, str]] = None,
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Pure cascade merge: ``defaults <- org <- team <- employee``.

    Returns ``(merged, sources)``. No database access — the caller supplies each
    tier's rows. This exists so the DB-backed path and the prefetched/batched
    path (see ``app/schedule_policy/resolver.py``) share ONE implementation of
    the precedence rather than each having its own.
    """
    merged = {k: str(v) for k, v in defaults.items()}
    sources = {k: "system" for k in defaults}
    for tier, rows in (("org", org), ("team", team), ("employee", employee)):
        for k, v in (rows or {}).items():
            if k in merged:
                merged[k] = v
                sources[k] = tier
    return merged, sources


def resolve_effective_with_sources(
    db: Session, user: models.User, defaults: Dict[str, Any],
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Effective settings for a user PLUS which tier each value came from.

    Returns ``(merged, sources)`` where every source is one of
    ``"system" | "org" | "team" | "employee"``. Same cascade as
    ``resolve_effective`` — that function delegates here so there is exactly one
    implementation of the precedence. The provenance map exists for the Schedule
    Resolver, which has to know whether a field was actually configured at some
    tier or is still sitting on the system default.
    """
    org_id = user.organization_id
    if not org_id:
        return merge_tiers(defaults)

    keys = list(defaults.keys())
    tkey = team_key_for_user(db, user)
    return merge_tiers(
        defaults,
        org=_rows_for_scope(db, org_id, keys),
        team=_rows_for_scope(db, org_id, keys, team_key=tkey) if tkey is not None else None,
        employee=_rows_for_scope(db, org_id, keys, user_id=user.id),
    )


def resolve_effective(db: Session, user: models.User, defaults: Dict[str, Any]) -> Dict[str, str]:
    """Effective settings for a user: default <- org <- team <- employee."""
    merged, _ = resolve_effective_with_sources(db, user, defaults)
    return merged


def parent_effective(db: Session, org_id: int, defaults: Dict[str, Any],
                     *, scope: str, team_key=None) -> Dict[str, str]:
    """The value each key would inherit if the given scope had no override.
      - org scope inherits from system defaults
      - team scope inherits from org-effective
      - employee scope inherits from team-effective (team_key given) or org-effective
    """
    if scope == "org":
        return {k: str(v) for k, v in defaults.items()}
    merged = org_settings(db, org_id, defaults)          # org-effective
    if scope == "employee" and team_key is not None:
        merged.update(_rows_for_scope(db, org_id, defaults.keys(), team_key=team_key))
    return merged


def scoped_view(db: Session, org_id: int, defaults: Dict[str, Any], descriptions: Dict[str, str],
                *, scope: str, team_key=None, user_id=None) -> List[Dict[str, Any]]:
    """Per-key view for the admin UI: the override value at this scope (if any),
    the inherited parent value, whether it's overridden, plus metadata."""
    own = _rows_for_scope(
        db, org_id, defaults.keys(),
        team_key=team_key if scope == "team" else None,
        user_id=user_id if scope == "employee" else None,
    )
    parent = parent_effective(db, org_id, defaults, scope=scope, team_key=team_key)
    out = []
    # Only surface curated keys — advanced engine tunables live in DEFAULTS (so
    # GET /settings still delivers them) but aren't shown in this admin UI.
    for key in KEY_META.keys():
        if key not in defaults:
            continue
        meta = KEY_META[key]
        # A key whose CONSUMER only ever reads the org tier must not be offered
        # at team or employee scope: the write would succeed, the audit log
        # would record it, the UI would show it as an override, and nothing
        # would change. Advertising a scope the behaviour does not honour is
        # worse than not offering it.
        if scope != "org" and is_org_only(key):
            continue
        overridden = key in own
        out.append({
            "key": key,
            "group": meta.get("group", "Other"),
            "label": meta.get("label", key),
            "type": meta.get("type", "int"),
            "options": meta.get("options"),
            "option_labels": meta.get("option_labels"),
            "unit": meta.get("unit"),
            "min": meta.get("min"),
            "max": meta.get("max"),
            "description": descriptions.get(key, ""),
            "default": str(defaults[key]),
            "parent_value": parent.get(key),      # what it inherits
            "value": own.get(key) if overridden else None,
            "is_override": overridden,
        })
    return out
