"""
Who receives a workforce alert — resolved per tenant, never globally.
=====================================================================
Every attendance / leave / break / risk alert names an employee, their
department and their attendance record. Until this module existed those emails
went to two process-wide constants (``ADMIN_EMAIL`` / ``HR_EMAIL`` in
``app.email_service``, defaulting to Temveris's own inboxes), so an alert about
an employee of *any* organization was delivered to Temveris staff and to nobody
at the organization it concerned. That is a cross-tenant disclosure of employee
PII, and it is what this module removes.

Resolution order — every step reads data owned by the SAME organization:

  1. ``notifications.alert_recipients`` through the normal settings cascade
     (employee > team > org > system default). Comma-separated addresses. A team
     override is the point: a team lead can receive their own team's alerts.
  2. ``OrgSettings.hr_email``, then ``OrgSettings.support_email`` — already
     editable under Company & Workspace Profile.
  3. The organization's own active admin / HR / owner users.

Leave purposes (``purpose`` starting with ``"leave."``) first consult
``notifications.leave_recipients`` through the same cascade, so an organization
can send leave / formal HR mail to a different inbox than its attendance
alerts. When that setting is empty the leave alert falls through to steps 1–3
exactly as before. Every other purpose ignores it.

If all three are empty the alert is NOT sent and an error is logged. Dropping a
notification is the correct failure mode: the alternative is delivering one
tenant's employee records to an address that belongs to someone else. A tenant
with no configured inbox and no admin user is a provisioning fault, and the log
line says so.

There is deliberately no global fallback address in this module.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from sqlalchemy.orm import Session

_log = logging.getLogger("zoiko.notifications.recipients")

SETTING_ALERT_RECIPIENTS = "notifications.alert_recipients"
SETTING_LEAVE_RECIPIENTS = "notifications.leave_recipients"

# Empty by default: the org/profile fields and the org's own admins are the
# intended sources. A non-empty default here would be a global address again.
SETTING_DEFAULTS = {
    SETTING_ALERT_RECIPIENTS: "",
    SETTING_LEAVE_RECIPIENTS: "",
}

SETTING_DESCRIPTIONS = {
    SETTING_ALERT_RECIPIENTS:
        "Comma-separated email addresses that receive workforce alerts "
        "(absent, late, idle, break overrun, low hours, leave requests) for "
        "this scope. Leave empty to use the workspace HR/support email, and "
        "failing that this organization's admin and HR users.",
    SETTING_LEAVE_RECIPIENTS:
        "Comma-separated email addresses that receive leave requests for this "
        "scope. Leave empty to send leave requests to the alert recipients "
        "above (and their usual fallbacks).",
}

_LEAVE_PURPOSE_PREFIX = "leave."

# Roles that stand in for "the people who run this workspace" when nothing is
# configured. Owner is included because a small tenant may have no HR seat.
_FALLBACK_ROLES = ("admin", "hr", "owner")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _clean(values) -> List[str]:
    """De-duplicate, lower-case and drop anything that is not an address.

    Order-preserving so the primary recipient stays first in the To: header.
    """
    out: List[str] = []
    seen = set()
    for raw in values:
        for part in str(raw or "").replace(";", ",").split(","):
            addr = part.strip().lower()
            if not addr or addr in seen or not _EMAIL_RE.match(addr):
                continue
            seen.add(addr)
            out.append(addr)
    return out


def _configured(db: Session, org_id, user=None,
                key: str = SETTING_ALERT_RECIPIENTS) -> List[str]:
    """Tier 1 — the settings cascade for ``key``. Uses the ONE resolver in
    ``app.settings.scope``; this module never queries ``app_settings`` itself."""
    if org_id is None:
        return []
    try:
        from app.settings import scope as settings_scope
        from database import models
        probe = user if user is not None else models.User(organization_id=org_id)
        merged = settings_scope.resolve_effective(db, probe, SETTING_DEFAULTS)
        return _clean([merged.get(key)])
    except Exception:
        _log.warning("alert recipients: cascade read failed org=%s", org_id,
                     exc_info=True)
        return []


def _profile_inboxes(db: Session, org_id) -> List[str]:
    """Tier 2 — Company & Workspace Profile."""
    if org_id is None:
        return []
    try:
        from database import models
        row = (
            db.query(models.OrgSettings.hr_email, models.OrgSettings.support_email)
            .filter(models.OrgSettings.organization_id == org_id)
            .first()
        )
    except Exception:
        return []
    return _clean(list(row) if row else [])


def _workspace_operators(db: Session, org_id) -> List[str]:
    """Tier 3 — this organization's own admin / HR / owner accounts.

    Strictly filtered on ``organization_id``, so this can only ever produce
    addresses belonging to the tenant the alert is about.
    """
    if org_id is None:
        return []
    try:
        from database import models
        rows = (
            db.query(models.User.email)
            .filter(
                models.User.organization_id == org_id,
                models.User.is_active == True,          # noqa: E712
                models.User.role.in_(_FALLBACK_ROLES),
            )
            .all()
        )
    except Exception:
        return []
    return _clean([r[0] for r in rows])


def alert_recipients(db: Session, org_id, user=None,
                     *, purpose: str = "alert") -> List[str]:
    """Addresses that should receive a workforce alert for ``org_id``.

    Pass ``user`` when the alert is about a specific employee so team- and
    employee-scope overrides of ``notifications.alert_recipients`` apply; the
    user's own ``organization_id`` is NOT trusted over ``org_id`` — the caller
    states which tenant it is acting for.

    Returns ``[]`` when nothing is configured, and logs that at ERROR. Callers
    must treat an empty list as "do not send".
    """
    if (purpose or "").startswith(_LEAVE_PURPOSE_PREFIX):
        leave = _configured(db, org_id, user, key=SETTING_LEAVE_RECIPIENTS)
        if leave:
            return leave
    for tier in (_configured(db, org_id, user),
                 _profile_inboxes(db, org_id),
                 _workspace_operators(db, org_id)):
        if tier:
            return tier
    _log.error(
        "No alert recipient configured for organization %s (%s). The "
        "notification was dropped rather than delivered to another tenant's "
        "inbox. Set notifications.alert_recipients, or an HR/support email "
        "under Company & Workspace Profile.",
        org_id, purpose,
    )
    return []


def recipients_for_user(db: Session, user, *, purpose: str = "alert") -> List[str]:
    """Convenience wrapper for the common "alert about this employee" case."""
    return alert_recipients(db, getattr(user, "organization_id", None), user,
                            purpose=purpose)
