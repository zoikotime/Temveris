"""Desktop-only clock-in policy — a per-organization SETTING.

WHY THIS EXISTS
    Some organizations want the Temveris desktop app to be the only way an
    employee can START a shift, because clocking in from the website leaves no
    activity tracking running for that session. Clock-OUT is deliberately
    untouched: if the desktop app crashes, the worker must still be able to end
    their day from the website rather than sit clocked in overnight.

WHAT CHANGED
    This used to be gated on ``DESKTOP_ONLY_CLOCK_IN_ORG_IDS``, an environment
    variable defaulting to ``"1"`` — i.e. one specific tenant's numeric id,
    baked into deployment configuration and shared by every tenant in the
    process. Two things were wrong with that in a multi-tenant product:

      * ``organization_id == 1`` is not a business rule. It is an autoincrement
        value. A fresh deployment where a different customer happens to be org 1
        silently inherited another company's clock-in restriction.
      * A per-tenant workforce policy cannot live in infrastructure config. The
        customer who wants the rule cannot switch it on, and the customer who
        does not cannot switch it off, without an operator editing an env var
        that affects everyone.

    It is now ``attendance.desktop_only_clock_in`` (default ``false``),
    resolved through the standard employee > team > organization > system
    default cascade like every other policy. A team that works in the field can
    be exempted; an individual can be exempted.

SCOPE — still intentionally narrow
    • Applies ONLY where the effective setting is true.
    • Applies ONLY to role == "employee". Every other role (admin, hr, owner,
      manager, billing_admin, super_admin) is unaffected everywhere.
    • Applies ONLY to clock-in. Never to clock-out, breaks, timesheets,
      active-time, the early-clock-out flow, or any admin override.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from app.auth.tokens import is_desktop_client

log = logging.getLogger(__name__)

# Machine-readable code the frontend keys off. Also returned to anyone who
# calls the API directly instead of going through the UI.
DESKTOP_APP_REQUIRED = "DESKTOP_APP_REQUIRED"

USER_MESSAGE = "Please clock in using the Temveris Desktop App."

# Only these roles are restricted. Anything not listed here keeps working from
# both the website and the desktop app.
RESTRICTED_ROLES = frozenset({"employee"})


def desktop_clock_in_required(user, db=None) -> bool:
    """True when `user` may only clock in from the desktop app.

    ``db`` is optional so the pure-predicate call sites that have no session
    (and the ones that only need the role check) keep working; without it the
    setting cannot be read and the answer is False — fail OPEN, because a bug
    here must never be able to block an entire organization from clocking in.

    ``GET /employees/me`` polls this every ~5 seconds per user, so the settings
    read is short-circuited by the role check first.
    """
    try:
        if (getattr(user, "role", "") or "").strip().lower() not in RESTRICTED_ROLES:
            return False
        if getattr(user, "organization_id", None) is None or db is None:
            return False
        from app.attendance_policy import policy_bool, SETTING_DESKTOP_ONLY_CLOCK_IN
        return policy_bool(db, user, SETTING_DESKTOP_ONLY_CLOCK_IN)
    except Exception:
        log.exception("desktop_clock_in_required failed; allowing clock-in")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# WEB ATTENDANCE LOCKOUT — product rule, all roles, all tenants
# ──────────────────────────────────────────────────────────────────────────────
# Attendance ACTIONS (clock in / clock out / start break / resume work) happen in
# the Temveris Desktop App only. The website stays fully READ-ONLY for
# attendance: dashboards, worked hours, active time, breaks, activity,
# screenshots, timesheets, reports, schedules and leave are all untouched.
#
# Deliberately a CODE-LEVEL constant, not a per-organization setting:
#   • it applies to every existing tenant and every future tenant with no
#     provisioning step and no admin having to configure anything, and
#   • there is no tenant-specific condition anywhere — org A, org B and every
#     org created tomorrow behave identically.
#
# The narrower, per-org `attendance.desktop_only_clock_in` policy below is left
# exactly as it was: it still works, it still cascades employee > team > org, and
# it remains the lever for exempting or further restricting a single tenant when
# this global rule is lifted.
#
# To restore website attendance actions, set this back to False (and flip
# WEB_ATTENDANCE_ACTIONS_ENABLED in frontend/src/config.js). Nothing is deleted.
WEB_ATTENDANCE_DISABLED = True

WEB_ATTENDANCE_MESSAGE = (
    "Attendance actions are available in the Temveris Desktop App."
)


def enforce_desktop_attendance(user, request: Optional[Request], db=None) -> None:
    """Raise 403 DESKTOP_APP_REQUIRED for any attendance ACTION not made from
    the desktop app. Role-agnostic and tenant-agnostic by design.

    ``request is None`` means the call did NOT arrive over HTTP — FastAPI always
    injects the Request for an annotated parameter, so a None here is an
    in-process caller (a scheduled job, another service function, a test calling
    the endpoint function directly). Those are not browsers and must not be
    blocked; the network path always carries a request and is always checked.

    Desktop detection reuses `is_desktop_client` — the SAME helper the auth layer
    and the desktop-only clock-in policy already use, so "is this the desktop
    app" has exactly one definition in the codebase. Note this is a client-type
    signal, not a security boundary: a header is forgeable by an already
    authenticated user. It is the right strength for a workforce-policy control
    over a user's own attendance.
    """
    if not WEB_ATTENDANCE_DISABLED:
        return
    if request is None:
        return
    if is_desktop_client(request):
        return
    raise HTTPException(
        status_code=403,
        detail={"code": DESKTOP_APP_REQUIRED, "message": WEB_ATTENDANCE_MESSAGE},
    )


def enforce_desktop_clock_in(user, request: Optional[Request], db=None) -> None:
    """Raise 403 DESKTOP_APP_REQUIRED if this clock-in is not from the app.

    No-op for every user the policy does not cover. Desktop detection reuses
    `is_desktop_client` (X-Client-Type: desktop header, User-Agent fallback) —
    the same helper the auth layer already uses to decide refresh-token
    delivery, so there is only one definition of "is this the desktop app".
    """
    if not desktop_clock_in_required(user, db):
        return
    if is_desktop_client(request):
        return
    raise HTTPException(
        status_code=403,
        detail={"code": DESKTOP_APP_REQUIRED, "message": USER_MESSAGE},
    )
