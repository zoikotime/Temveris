"""Requires-Attention monitor — notifies each organization's own Admin + HR
about workers who need follow-up on their attendance today.

What it detects
---------------
For every employee, in THEIR organization's local time, against THEIR effective
schedule:

  • Late   — no clock-in yet, past shift start + grace, before the absence
             cutoff  (``ArrivalStatus.LATE_PENDING``)
  • Absent — no clock-in, no approved leave, past the absence cutoff
             (``ArrivalStatus.ABSENT``)

Both come from ``classify_arrival`` fed by ``resolve_work_schedule``. This
module performs NO threshold arithmetic of its own. It used to: a global
``SHIFT_NOON_HOUR = 12``, a global ``DEFAULT_GRACE_MIN = 20``, a global
``weekday() >= 5`` weekend test and a global IST clock — one set of numbers
applied to every tenant in a single sweep, so a Monday–Saturday organization
got no Saturday alerts because another organization does not work Saturdays,
and an organization outside India had its cutoffs evaluated on India's clock.

Tenancy
-------
The sweep is organized BY ORGANIZATION. Each org gets its own local "today",
its own working-day answer, its own cutoffs, its own holiday calendar and its
own recipient list. One tenant's configuration can no longer influence another's
alerts, and one tenant's holiday can no longer silence another's.

Why a scheduler (not the dashboard poll)
-----------------------------------------
The Absent email also fires from ``GET /employees/team``, which only runs while
an admin has the Overview open. A notification rule should fire whether or not
anyone is watching, so this dedicated monitor runs on a fixed cadence
(registered in ``main.py`` lifespan, like the other §21 monitors).

Dedup is via AuditLog rows written for the current local day:
  • LATE   → action "EmployeeMarkedLate"
  • ABSENT → action "EmployeeMarkedAbsent" (shared with /employees/team, so a
             worker is emailed at most once per day regardless of which path
             detects first)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database import models
from app import _tz
from app._system_admin import (
    NON_WORKING_ROLES,
    exclude_system_admin,
    is_non_working_role,
)
from app.email_service import (
    send_absent_alert_to_admin_hr,
    send_late_arrival_alert_to_admin_hr,
)
from app.notifications.recipients import alert_recipients
from websocket_manager import manager

_log = logging.getLogger("zoiko.attendance_alerts")

# Roles we never alert about (no point telling an admin they're "late" — and
# the seeded Administrator/CEO seat is stripped via exclude_system_admin).
#
# This was a local `{"admin", "hr"}`, re-typed as a literal tuple twice more in
# employees/router.py. Three copies of one policy is why it drifted: the product
# grew to ten roles and the alert paths still knew two, so a workspace's
# `owner`, `super_admin` or `billing_admin` seat was treated as a worker who had
# failed to turn up and its admins were emailed about it every working day.
# The set now lives in app._system_admin next to the display-name list — see the
# derivation there, and note it is a DENYLIST: an unknown role stays tracked.
_SKIP_ROLES = NON_WORKING_ROLES


def _clocked_in_ids(db: Session, org_id, day_start_utc: datetime,
                    day_end_utc: datetime) -> Set[int]:
    """User ids in THIS org with at least one clock-in during its local day.

    Scoped to the organization and bounded at both ends. The previous version
    queried every tenant's sessions with a single open-ended IST lower bound.
    """
    rows = (
        db.query(models.ClockSession.user_id)
        .filter(
            models.ClockSession.organization_id == org_id,
            models.ClockSession.clock_in_time >= day_start_utc,
            models.ClockSession.clock_in_time < day_end_utc,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _on_leave_ids(db: Session, user_ids, on_date) -> Set[int]:
    """User ids with an APPROVED leave covering this date. Restricted to the
    organization's own users rather than scanning every tenant's leave table."""
    if not user_ids:
        return set()
    day_start = datetime.combine(on_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = datetime.combine(on_date, datetime.max.time(), tzinfo=timezone.utc)
    rows = (
        db.query(models.LeaveRequest.user_id)
        .filter(
            and_(
                models.LeaveRequest.user_id.in_(list(user_ids)),
                models.LeaveRequest.status == "approved",
                models.LeaveRequest.from_date <= day_end,
                models.LeaveRequest.to_date >= day_start,
            )
        )
        .all()
    )
    return {r[0] for r in rows}


def _already_alerted_ids(db: Session, user_ids, action: str,
                         day_start_utc: datetime) -> Set[int]:
    """User ids already alerted today for the given audit action — the dedup
    set that keeps a worker to one alert per day per state. Bounded to this
    organization's users so the query cost does not grow with the platform."""
    if not user_ids:
        return set()
    rows = (
        db.query(models.AuditLog.target_id)
        .filter(
            and_(
                models.AuditLog.target_id.in_(list(user_ids)),
                models.AuditLog.entity == "User",
                models.AuditLog.action == action,
                models.AuditLog.created_at >= day_start_utc,
            )
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _broadcast_bell(sub: str, user, *, late_minutes: int = 0) -> None:
    """Push a `requires_attention` event so the admin dashboard bell surfaces
    it live — to THIS EMPLOYEE'S ORGANIZATION ONLY.

    This used `broadcast_sync`, which websocket_manager documents as "never use
    for business data": it fans out to every org room, so one tenant's admin
    saw another tenant's employee name, department and designation in their
    bell. The email path below is correctly scoped by `alert_recipients`; the
    live push silently defeated it. No-op when no loop/clients are connected.
    """
    org_id = getattr(user, "organization_id", None)
    if org_id is None:
        return
    try:
        manager.broadcast_to_org_sync(org_id, json.dumps({
            "type": "requires_attention",
            "sub": sub,                       # "late" | "absent"
            "user_id": user.id,
            "organization_id": user.organization_id,
            "user_name": user.name or (user.email.split("@")[0] if user.email else "Employee"),
            "department": user.department or "",
            "designation": user.designation or "",
            "late_minutes": late_minutes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        _log.exception("requires_attention broadcast failed for user %s", getattr(user, "id", "?"))


def _users_by_org(db: Session) -> Dict[object, List[models.User]]:
    """Every alertable active employee, grouped by organization."""
    grouped: Dict[object, List[models.User]] = {}
    for u in exclude_system_admin(db.query(models.User).all(), db):
        if not u.is_active:
            continue
        if is_non_working_role(u):
            continue
        if u.organization_id is None:
            continue
        grouped.setdefault(u.organization_id, []).append(u)
    return grouped


def _scan_org(db: Session, org_id, users: List[models.User]) -> int:
    """One organization's sweep. Returns how many workers were freshly alerted.

    Every threshold in here is resolved, never assumed. A failure inside one
    organization is logged and skipped so it cannot stop the others.
    """
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedules

    # Re-assert the non-worker filter HERE, next to the sends, rather than
    # trusting whoever built `users`. `_users_by_org` already applies it, so on
    # the live path this is a no-op — but the whole defect being fixed was one
    # policy living apart from the code that acts on it, and a second caller of
    # this function would otherwise reintroduce it silently. Same shared
    # predicate, so there is nothing to drift.
    users = [u for u in users if not is_non_working_role(u)]
    if not users:
        return 0

    now_local = _tz.local_now(db, org_id)
    today = now_local.date()
    day_start_utc, day_end_utc = _tz.local_day_bounds(db, org_id, today)

    user_ids = [u.id for u in users]
    clocked_in = _clocked_in_ids(db, org_id, day_start_utc, day_end_utc)
    on_leave = _on_leave_ids(db, user_ids, today)
    already_late = _already_alerted_ids(db, user_ids, "EmployeeMarkedLate", day_start_utc)
    already_absent = _already_alerted_ids(db, user_ids, "EmployeeMarkedAbsent", day_start_utc)

    # One prefetch for the whole organization (five queries), then pure
    # in-memory resolution per user.
    scheds = resolve_work_schedules(db, users, today)

    late_to_alert: List[Tuple[models.User, int, str]] = []
    absent_to_alert: List[models.User] = []

    for u in users:
        if u.id in clocked_in or u.id in on_leave:
            continue
        sched = scheds.get(u.id)
        if sched is None:
            continue
        # Not a working day for THIS employee (weekly pattern, roster off-day,
        # or this organization's holiday calendar) → nothing to alert about.
        if not sched.is_working_day:
            continue
        verdict = classify_arrival(None, sched, now_local)
        if verdict.status == ArrivalStatus.ABSENT:
            if u.id not in already_absent:
                absent_to_alert.append(u)
        elif verdict.status == ArrivalStatus.LATE_PENDING:
            if u.id not in already_late:
                late_to_alert.append((
                    u, int(verdict.late_minutes or 0),
                    sched.start_local.strftime("%H:%M") if sched.start_local else "",
                ))

    if not late_to_alert and not absent_to_alert:
        return 0

    date_str = today.isoformat()
    # Rendered on the RECIPIENT ORGANIZATION's wall clock, with the zone named.
    # This was a UTC string, so an admin in Kolkata read "06:32 UTC" on an email
    # about something their team saw at noon. Presentation only — `now_local` is
    # the same instant the verdict above was computed from, and no threshold is
    # derived from this string.
    detected_at = f"{now_local:%Y-%m-%d %H:%M} {_tz.org_timezone_name(db, org_id)}"

    # Write audit rows BEFORE sending so a mail-provider crash can't cause us to
    # re-alert the same worker on the next tick (mirrors /employees/team).
    for u, late_min, _shift in late_to_alert:
        db.add(models.AuditLog(
            actor_id=None, target_id=u.id, organization_id=org_id, entity="User",
            action="EmployeeMarkedLate",
            payload={"date": date_str, "detected_at": detected_at,
                     "late_minutes": late_min},
        ))
    for u in absent_to_alert:
        db.add(models.AuditLog(
            actor_id=None, target_id=u.id, organization_id=org_id, entity="User",
            action="EmployeeMarkedAbsent",
            payload={"date": date_str, "detected_at": detected_at},
        ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        _log.exception("attendance-alert audit write failed org=%s; skipping sends", org_id)
        return 0

    # This organization's own audience. Resolved once per org per tick.
    recipients = alert_recipients(db, org_id, purpose="attendance.requires_attention")

    fired = 0
    for u, late_min, shift_label in late_to_alert:
        try:
            send_late_arrival_alert_to_admin_hr(
                recipients=recipients,
                employee_name=u.name or (u.email.split("@")[0] if u.email else "Employee"),
                employee_email=u.email or "",
                late_date=date_str,
                late_minutes=late_min,
                shift_start=shift_label,
                department=u.department or "",
                designation=u.designation or "",
                detected_at=detected_at,
            )
        except Exception:
            _log.exception("late-arrival email failed for user %s", u.id)
        _broadcast_bell("late", u, late_minutes=late_min)
        fired += 1

    for u in absent_to_alert:
        try:
            send_absent_alert_to_admin_hr(
                recipients=recipients,
                employee_name=u.name or (u.email.split("@")[0] if u.email else "Employee"),
                employee_email=u.email or "",
                absent_date=date_str,
                department=u.department or "",
                designation=u.designation or "",
                detected_at=detected_at,
            )
        except Exception:
            _log.exception("absent email failed for user %s", u.id)
        _broadcast_bell("absent", u)
        fired += 1

    return fired


def run_once_sync() -> int:
    """One scan across every organization. Returns workers freshly alerted."""
    fired = 0
    db = SessionLocal()
    try:
        for org_id, users in _users_by_org(db).items():
            try:
                fired += _scan_org(db, org_id, users)
            except Exception:
                # One tenant's bad data must never stop the others.
                try:
                    db.rollback()
                except Exception:
                    pass
                _log.exception("attendance-alert sweep failed for organization %s", org_id)
        return fired
    finally:
        db.close()


async def run_forever(interval_seconds: int = 300) -> None:
    _log.info("Requires-Attention monitor started (interval=%ss)", interval_seconds)
    while True:
        try:
            fired = await asyncio.to_thread(run_once_sync)
            if fired:
                _log.info("Requires-Attention tick alerted %s worker(s)", fired)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Requires-Attention tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
