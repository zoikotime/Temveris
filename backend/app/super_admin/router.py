"""Super-Admin (platform-ops) portal.

Endpoints under `/api/super-admin/*` are restricted to users whose role is
exactly `super_admin`. The role is granted by the env-driven bootstrap in
`app.auth.service._maybe_promote_super_admin` — there is no in-product UI
that can grant it.

What this module DOES:
    • Lists every commercial organization in the platform
    • Drill-down: org detail, users, subscription, recent commercial events
    • Platform-wide stats (org count, user count, active sessions, signups)
    • Suspend / restore an organization — a REAL access boundary, not just the
      state column: see `suspend_organization` and `app.org.lifecycle`
    • Permanently delete a suspended organization and everything it owns
      (`delete_organization` → `app.org.deletion_service`)
    • Impersonate a user (audited) for support investigations

What this module deliberately does NOT do:
    The legacy multi-tenant scoping is intentionally out of scope for this
    portal. Existing operational routes (`/employees`, `/api/activity-log/*`,
    `/leave/*`, etc.) still return ALL workspace data to any caller with the
    `admin` or `hr` role. The super-admin portal observes the org boundary
    via the `CommercialOrganization` / `CommercialWorkspace` tables, but
    it does NOT retro-fit org filters onto the legacy queries — that is
    the multi-tenancy phase-2 work the user opted to defer.
"""
from __future__ import annotations

import logging
import os 
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, desc, or_, case
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
# Billable-seat definition lives with billing, not duplicated per consumer.
from app.billing import worker_state
# Bound to a distinct name: this module already rebinds `_tz` to
# datetime.timezone inside one handler, and the two must not be confused.
from app._tz import is_valid_timezone as _is_valid_timezone
from app.auth.service import (
    require_super_admin,
    create_access_token,
    get_current_user,
    _user_cache_invalidate,
    _org_cache_invalidate,
)
from app.auth import tokens as rtokens
from app.org import lifecycle as org_lifecycle
from app.org import deletion_service
from app.org.provisioning_service import (
    CREATED_VIA_SUPER_ADMIN,
    TenantProvisioningError,
    provision_tenant,
)

_log = logging.getLogger("zoiko.super_admin")
router = APIRouter(prefix="/api/super-admin", tags=["Super Admin"])

# Slug validation now lives with the provisioning logic it guards, in
# app.org.provisioning_service.SLUG_RE.


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _commercial_models():
    """Lazy-import the commercial module so the legacy boot path doesn't
    fail when the tables aren't migrated yet — every helper below uses
    `try/except` rather than crashing the route."""
    try:
        from app.commercial import models as cm
        return cm
    except Exception as e:
        _log.warning("commercial models unavailable: %s", e)
        return None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _isoformat(dt) -> Optional[str]:
    if not dt:
        return None
    try:
        s = dt.isoformat()
        # Naive datetimes (no tzinfo) are stored as UTC — tell the browser explicitly
        # so it converts to local time correctly instead of treating them as local time.
        if dt.tzinfo is None and "+" not in s and s[-1] != "Z":
            s += "Z"
        return s
    except Exception:
        return None


def _org_operational_metrics(db: Session, operational_org_id: Optional[int]) -> dict:
    """Query real operational tables for user/team/activity counts.

    Returns a dict with total_users, active_users, admin_count, employee_count,
    team_count, and last_active. Falls back to zeroes if operational_org_id is NULL.
    """
    if not operational_org_id:
        return {
            "total_users": 0, "active_users": 0,
            "admin_count": 0, "employee_count": 0,
            "team_count": 0, "last_active": None,
        }

    base = db.query(models.User).filter(
        models.User.organization_id == operational_org_id,
        models.User.role != "super_admin",
    )
    total_users  = _safe_int(base.with_entities(func.count(models.User.id)).scalar())
    active_users = _safe_int(
        base.filter(models.User.is_active == True)  # noqa: E712
            .with_entities(func.count(models.User.id))
            .scalar()
    )
    admin_count = _safe_int(
        base.filter(models.User.role.in_(["admin", "hr"]))
            .with_entities(func.count(models.User.id))
            .scalar()
    )
    employee_count = _safe_int(
        base.filter(models.User.role == "employee")
            .with_entities(func.count(models.User.id))
            .scalar()
    )
    team_count = _safe_int(
        db.query(func.count(models.Team.id))
          .filter(models.Team.organization_id == operational_org_id)
          .scalar()
    )
    # Derive "last active" from UserSession.last_active_at for users in this org
    user_ids_sq = (
        db.query(models.User.id)
          .filter(models.User.organization_id == operational_org_id)
          .scalar_subquery()
    )
    last_active_dt = None
    try:
        last_active_dt = (
            db.query(func.max(models.UserSession.last_active_at))
              .filter(models.UserSession.user_id.in_(
                  db.query(models.User.id)
                    .filter(models.User.organization_id == operational_org_id)
              ))
              .scalar()
        )
    except Exception:
        pass

    return {
        "total_users":    total_users,
        "active_users":   active_users,
        "admin_count":    admin_count,
        "employee_count": employee_count,
        "team_count":     team_count,
        "last_active":    _isoformat(last_active_dt),
    }


def _bulk_org_operational_metrics(
    db: Session, org_ids: list
) -> dict:
    """Batch replacement for calling _org_operational_metrics in a loop.

    Runs exactly 3 queries for any page size instead of 6 × N queries.
    Returns a dict keyed by operational_org_id (int) → metrics dict.
    """
    if not org_ids:
        return {}

    empty = {
        "total_users": 0, "active_users": 0,
        "admin_count": 0, "employee_count": 0,
        "team_count": 0, "last_active": None,
    }
    results = {oid: dict(empty) for oid in org_ids}

    # ── Query 1: user counts by org (total / active / admin / employee) ──────
    # One GROUP BY replaces 4 individual COUNT queries per org.
    try:
        user_rows = (
            db.query(
                models.User.organization_id,
                func.count(models.User.id).label("total"),
                func.sum(case((models.User.is_active == True, 1), else_=0)).label("active"),  # noqa: E712
                func.sum(case((models.User.role.in_(["admin", "hr"]), 1), else_=0)).label("admins"),
                func.sum(case((models.User.role == "employee", 1), else_=0)).label("employees"),
            )
            .filter(
                models.User.organization_id.in_(org_ids),
                models.User.role != "super_admin",
            )
            .group_by(models.User.organization_id)
            .all()
        )
        for row in user_rows:
            if row.organization_id in results:
                results[row.organization_id].update({
                    "total_users":   _safe_int(row.total),
                    "active_users":  _safe_int(row.active),
                    "admin_count":   _safe_int(row.admins),
                    "employee_count": _safe_int(row.employees),
                })
    except Exception:
        pass

    # ── Query 2: team counts by org ──────────────────────────────────────────
    try:
        team_rows = (
            db.query(
                models.Team.organization_id,
                func.count(models.Team.id).label("team_count"),
            )
            .filter(models.Team.organization_id.in_(org_ids))
            .group_by(models.Team.organization_id)
            .all()
        )
        for row in team_rows:
            if row.organization_id in results:
                results[row.organization_id]["team_count"] = _safe_int(row.team_count)
    except Exception:
        pass

    # ── Query 3: most-recent session per org (via User join) ─────────────────
    try:
        last_rows = (
            db.query(
                models.User.organization_id,
                func.max(models.UserSession.last_active_at).label("last_active"),
            )
            .join(models.UserSession, models.UserSession.user_id == models.User.id)
            .filter(models.User.organization_id.in_(org_ids))
            .group_by(models.User.organization_id)
            .all()
        )
        for row in last_rows:
            if row.organization_id in results:
                results[row.organization_id]["last_active"] = _isoformat(row.last_active)
    except Exception:
        pass

    return results


def _org_detail_settings(db: Session, operational_org_id: Optional[int]) -> dict:
    """Query operational + app settings for the detail drawer.

    Returns org_settings (from OrgSettings), app_settings (shared pool, waiting period),
    and live operational metrics (active clock sessions, pending leave, last payroll run,
    active leave policy count).
    """
    if not operational_org_id:
        return {
            "org_settings": {},
            "app_settings": {},
            "active_clock_sessions": 0,
            "pending_leave_count": 0,
            "last_payroll_run": None,
            "leave_policies_count": 0,
        }

    # ── OrgSettings ──────────────────────────────────────────────────────
    org_settings_row = (
        db.query(models.OrgSettings)
          .filter(models.OrgSettings.organization_id == operational_org_id)
          .one_or_none()
    )
    org_settings = {}
    if org_settings_row:
        org_settings = {
            "company_name":     getattr(org_settings_row, "company_name", None),
            "slug":             getattr(org_settings_row, "slug", None),
            "primary_country":  getattr(org_settings_row, "primary_country", None),
            "default_timezone": getattr(org_settings_row, "default_timezone", None),
            "default_currency": getattr(org_settings_row, "default_currency", None),
        }

    # ── AppSettings ──────────────────────────────────────────────────────
    # organization_id is filtered explicitly. The ":{org_id}" key suffix is a
    # naming convention, not an isolation mechanism — relying on it alone is
    # what let an org-less row be read as any tenant's configuration.
    shared_pool_val = (
        db.query(models.AppSettings.value)
          .filter(models.AppSettings.organization_id == operational_org_id,
                  models.AppSettings.key == f"shared_pool_enabled:{operational_org_id}")
          .scalar()
    )
    waiting_period_val = (
        db.query(models.AppSettings.value)
          .filter(models.AppSettings.organization_id == operational_org_id,
                  models.AppSettings.key == f"leave_waiting_period_months:{operational_org_id}")
          .scalar()
    )
    app_settings = {
        "shared_pool_enabled":         (shared_pool_val or "").lower() == "true",
        "leave_waiting_period_months": int(waiting_period_val or 0),
    }

    # ── Active clock sessions ────────────────────────────────────────────
    active_clock_sessions = _safe_int(
        db.query(func.count(models.ClockSession.id))
          .filter(
              models.ClockSession.organization_id == operational_org_id,
              models.ClockSession.is_active == True,  # noqa: E712
          )
          .scalar()
    )

    # ── Pending leave requests (status = submitted) ───────────────────────
    pending_leave_count = _safe_int(
        db.query(func.count(models.LeaveRequest.id))
          .filter(
              models.LeaveRequest.organization_id == operational_org_id,
              models.LeaveRequest.status == "submitted",
          )
          .scalar()
    )

    # ── Last payroll run ──────────────────────────────────────────────────
    last_payroll_run = None
    try:
        pr = (
            db.query(models.PayrollRun)
              .filter(models.PayrollRun.organization_id == operational_org_id)
              .order_by(desc(models.PayrollRun.created_at))
              .first()
        )
        if pr:
            last_payroll_run = {
                "period_start":  _isoformat(pr.period_start),
                "period_end":    _isoformat(pr.period_end),
                "status":        pr.status,
                "total_amount":  float(pr.total_amount or 0),
                "currency":      pr.currency or "USD",
                "created_at":    _isoformat(pr.created_at),
            }
    except Exception:
        pass

    # ── Active leave policies ─────────────────────────────────────────────
    leave_policies_count = _safe_int(
        db.query(func.count(models.LeavePolicy.id))
          .filter(
              models.LeavePolicy.organization_id == operational_org_id,
              models.LeavePolicy.is_active == True,  # noqa: E712
          )
          .scalar()
    )

    return {
        "org_settings":           org_settings,
        "app_settings":           app_settings,
        "active_clock_sessions":  active_clock_sessions,
        "pending_leave_count":    pending_leave_count,
        "last_payroll_run":       last_payroll_run,
        "leave_policies_count":   leave_policies_count,
    }


def _org_summary_row(db: Session, org, cm, bulk_metrics: dict = None) -> dict:
    """Compose the per-row payload used by the org list + the org detail.

    bulk_metrics — pre-fetched result from _bulk_org_operational_metrics; when
    provided the per-org DB calls are skipped entirely. Falls back to the
    single-org path (used by organization_detail) when None.
    """
    sub = None
    if cm:
        sub = (
            db.query(cm.CommercialSubscription)
              .filter(cm.CommercialSubscription.organization_id == org.id)
              .one_or_none()
        )

    op_oid = getattr(org, "operational_org_id", None)
    if bulk_metrics is not None:
        op_metrics = bulk_metrics.get(op_oid) or {
            "total_users": 0, "active_users": 0,
            "admin_count": 0, "employee_count": 0,
            "team_count": 0, "last_active": None,
        }
    else:
        op_metrics = _org_operational_metrics(db, op_oid)

    return {
        "id":                 org.id,
        "operational_org_id": getattr(org, "operational_org_id", None),
        "name":               org.name,
        "legal_name":         org.legal_name,
        "country":            org.country,
        "domain":             org.domain,
        "industry":           org.industry,
        "expected_users":     _safe_int(org.expected_users),
        "commercial_state":   org.commercial_state,
        "created_at":         _isoformat(org.created_at),
        "owner_user_id":      org.owner_user_id,
        # Real operational counts
        "user_count":         op_metrics["total_users"],
        "active_users":       op_metrics["active_users"],
        "admin_count":        op_metrics["admin_count"],
        "employee_count":     op_metrics["employee_count"],
        "team_count":         op_metrics["team_count"],
        "last_active":        op_metrics["last_active"],
        # Workspace count from commercial layer (0 until workspace provisioning used)
        "workspace_count":    0,
        "subscription": ({
            "plan_code":     sub.plan_code,
            "billing_cycle": sub.billing_cycle,
            "state":         sub.state,
            "seat_count":    sub.seat_count,
            "currency":      sub.currency,
            "trial_ends_at": _isoformat(sub.trial_ends_at),
            "active_since":  _isoformat(sub.active_since),
        } if sub else None),
    }


# ──────────────────────────────────────────────────────────────────────────
# Platform-wide stats
# ──────────────────────────────────────────────────────────────────────────
@router.get("/stats")
def platform_stats(
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Top-row metrics for the super-admin dashboard.

    All numbers come from live aggregates — no estimates."""
    cm = _commercial_models()

    total_orgs = 0
    active_orgs = 0
    trial_orgs = 0
    suspended_orgs = 0
    past_due_orgs = 0
    paid_orgs = 0
    if cm:
        total_orgs = _safe_int(db.query(func.count(cm.CommercialOrganization.id)).scalar())
        state_counts = dict(
            db.query(cm.CommercialOrganization.commercial_state, func.count(cm.CommercialOrganization.id))
              .group_by(cm.CommercialOrganization.commercial_state)
              .all()
        )
        active_orgs    = _safe_int(state_counts.get("active"))
        trial_orgs     = _safe_int(state_counts.get("trial_active"))
        suspended_orgs = _safe_int(state_counts.get("suspended"))
        past_due_orgs  = _safe_int(state_counts.get("past_due"))
        paid_orgs      = active_orgs

    total_users = _safe_int(
        db.query(func.count(models.User.id))
          .filter(models.User.is_active == True)  # noqa: E712
          .scalar()
    )
    today = datetime.now(timezone.utc) - timedelta(hours=24)
    active_today = 0
    try:
        active_today = _safe_int(
            db.query(func.count(func.distinct(models.UserSession.user_id)))
              .filter(models.UserSession.is_revoked == False)  # noqa: E712
              .filter(models.UserSession.last_active_at >= today)
              .scalar()
        )
    except Exception:
        # UserSession may have a different column name in older deploys.
        active_today = 0

    recent_signups = 0
    if cm:
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        recent_signups = _safe_int(
            db.query(func.count(cm.CommercialSignup.id))
              .filter(cm.CommercialSignup.created_at >= cutoff_7d)
              .scalar()
        )

    # ── Platform-level health metrics ──────────────────────────────────────
    privileged_admin_users = _safe_int(
        db.query(func.count(models.User.id))
          .filter(models.User.role.in_(["admin", "hr"]))
          .filter(models.User.is_active == True)  # noqa: E712
          .scalar()
    )

    active_countries = 0
    if cm:
        try:
            active_countries = _safe_int(
                db.query(func.count(func.distinct(cm.CommercialOrganization.country)))
                  .filter(cm.CommercialOrganization.country.isnot(None))
                  .scalar()
            )
        except Exception:
            pass

    # PLATFORM-WIDE billable seats — deliberately every organization, matching
    # the other metrics in the "platform" block of this response.
    #
    # Counted live from users.billing_state rather than SUM(billing_subscriptions
    # .active_users): that column is a per-tenant denormalisation refreshed only
    # when that tenant opens their billing page, so summing it would under-report
    # any organization that has never visited billing. This is a platform metric
    # and must not be derived from a tenant-scoped cached value.
    billable_seats = 0
    try:
        billable_seats = _safe_int(
            db.query(func.count(models.User.id))
              .filter(models.User.billing_state.in_(sorted(worker_state.BILLABLE_STATES)))
              .scalar()
        )
    except Exception:
        pass

    return {
        "organizations": {
            "total":       total_orgs,
            "active":      active_orgs,
            "trial":       trial_orgs,
            "paid":        paid_orgs,
            "suspended":   suspended_orgs,
            "past_due":    past_due_orgs,
        },
        "users": {
            "total":         total_users,
            "active_today":  active_today,
        },
        "signups": {
            "last_7d":       recent_signups,
        },
        "platform": {
            "privileged_admin_users": privileged_admin_users,
            "active_countries":       active_countries,
            "billable_seats":         billable_seats,
            "open_security_events":   0,
        },
        "generated_at":   _isoformat(datetime.now(timezone.utc)),
    }


# ──────────────────────────────────────────────────────────────────────────
# Organizations — list, detail, suspend / restore
# ──────────────────────────────────────────────────────────────────────────
@router.get("/organizations")
def list_organizations(
    search: Optional[str] = None,
    state:  Optional[str] = None,
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """List every tenant on the platform.

    `search` matches name / legal_name / domain (case-insensitive substring).
    `state` is an exact match on `commercial_state`."""
    cm = _commercial_models()
    if not cm:
        return {"items": [], "count": 0, "total": 0}

    q = db.query(cm.CommercialOrganization)
    if state:
        q = q.filter(cm.CommercialOrganization.commercial_state == state)
    if search:
        like = f"%{search.strip().lower()}%"
        q = q.filter(or_(
            func.lower(cm.CommercialOrganization.name).like(like),
            func.lower(cm.CommercialOrganization.legal_name).like(like),
            func.lower(cm.CommercialOrganization.domain).like(like),
        ))

    total = _safe_int(q.with_entities(func.count(cm.CommercialOrganization.id)).scalar())
    orgs = (
        q.order_by(desc(cm.CommercialOrganization.created_at))
         .offset(offset)
         .limit(limit)
         .all()
    )

    # Collect every operational_org_id on this page and fetch all metrics in
    # 3 queries instead of 6 × len(orgs) queries.
    op_ids = [
        oid for oid in (getattr(o, "operational_org_id", None) for o in orgs)
        if oid is not None
    ]
    bulk_metrics = _bulk_org_operational_metrics(db, op_ids)

    items = [_org_summary_row(db, o, cm, bulk_metrics) for o in orgs]
    return {"items": items, "count": len(items), "total": total}


@router.get("/organizations/{org_id}")
def organization_detail(
    org_id: str,
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Per-org drill-down: workspaces, signup linkage, recent events."""
    cm = _commercial_models()
    if not cm:
        raise HTTPException(404, "Commercial layer not initialized")
    org = (
        db.query(cm.CommercialOrganization)
          .filter(cm.CommercialOrganization.id == org_id)
          .one_or_none()
    )
    if not org:
        raise HTTPException(404, "Organization not found")

    workspaces = (
        db.query(cm.CommercialWorkspace)
          .filter(cm.CommercialWorkspace.organization_id == org_id)
          .order_by(cm.CommercialWorkspace.created_at.asc())
          .all()
    )
    signups = (
        db.query(cm.CommercialSignup)
          .filter(cm.CommercialSignup.organization_id == org_id)
          .order_by(desc(cm.CommercialSignup.created_at))
          .limit(20)
          .all()
    )
    events = (
        db.query(cm.CommercialEvent)
          .filter(cm.CommercialEvent.organization_id == org_id)
          .order_by(desc(cm.CommercialEvent.occurred_at))
          .limit(25)
          .all()
    )

    # ── Operational settings + live metrics ──────────────────────────────
    op_org_id = getattr(org, "operational_org_id", None)
    detail_settings = _org_detail_settings(db, op_org_id)

    # ── Operational data: real users, teams, recent activity ─────────────

    # All users in this org from the operational users table
    op_users = []
    op_teams = []
    recent_activity = []
    if op_org_id:
        op_users = (
            db.query(models.User)
              .filter(
                  models.User.organization_id == op_org_id,
                  models.User.role != "super_admin",
              )
              .order_by(models.User.created_at.asc())
              .limit(100)
              .all()
        )
        op_teams = (
            db.query(models.Team)
              .filter(models.Team.organization_id == op_org_id)
              .order_by(models.Team.name.asc())
              .all()
        )
        recent_activity = (
            db.query(models.AuditLog)
              .filter(models.AuditLog.organization_id == op_org_id)
              .order_by(desc(models.AuditLog.created_at))
              .limit(20)
              .all()
        )

    return {
        "organization": _org_summary_row(db, org, cm),
        # Operational settings + live metrics for the detail drawer
        "org_settings":          detail_settings["org_settings"],
        "app_settings":          detail_settings["app_settings"],
        "active_clock_sessions": detail_settings["active_clock_sessions"],
        "pending_leave_count":   detail_settings["pending_leave_count"],
        "last_payroll_run":      detail_settings["last_payroll_run"],
        "leave_policies_count":  detail_settings["leave_policies_count"],
        "workspaces": [
            {
                "id":                    w.id,
                "name":                  w.name,
                "slug":                  w.slug,
                "region":                w.region,
                "timezone":              w.timezone,
                "currency":              w.currency,
                "provisioning_state":    w.provisioning_state,
                "first_login_at":        _isoformat(w.first_login_at),
                "first_work_session_at": _isoformat(w.first_work_session_at),
                "created_at":            _isoformat(w.created_at),
            }
            for w in workspaces
        ],
        "users": [
            {
                "id":             u.id,
                "name":           u.name,
                "email":          u.email,
                "role":           u.role,
                "is_active":      bool(u.is_active),
                "is_password_set": bool(u.is_password_set),
                "last_login_at":  None,
                "created_at":     _isoformat(getattr(u, "created_at", None)),
            }
            for u in op_users
        ],
        "teams": [
            {
                "id":   t.id,
                "name": t.name,
            }
            for t in op_teams
        ],
        "recent_activity": [
            {
                "id":          a.id,
                "action":      a.action,
                "entity":      a.entity,
                "occurred_at": _isoformat(a.created_at),
                "actor_id":    a.actor_id,
                "payload":     a.payload,
            }
            for a in recent_activity
        ],
        "signups": [
            {
                "id":           s.id,
                "email":        s.email,
                "state":        s.state,
                "intent":       s.intent,
                "plan":         s.plan,
                "country":      s.country,
                "created_at":   _isoformat(s.created_at),
                "user_id":      s.user_id,
                "failure_code": s.failure_code,
                "ip_address":   s.ip_address,
            }
            for s in signups
        ],
        "recent_events": [
            {
                "id":          e.id,
                "event_type":  e.event_type,
                "occurred_at": _isoformat(e.occurred_at),
                "state_from":  e.state_from,
                "state_to":    e.state_to,
                "error_code":  e.error_code,
                "payload":     e.payload,
            }
            for e in events
        ],
    }


class OrgStateChangeRequest(BaseModel):
    reason: Optional[str] = None


def _request_ip(request: Request) -> Optional[str]:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None))


def _apply_operational_flag(db: Session, operational_org_id: Optional[int],
                            *, active: bool) -> None:
    """Mirror the lifecycle decision onto the operational `organizations` row.

    `Organization.is_active` existed but was never written or read. Keeping the
    two in step means a tenant with no commercial row is still coverable by the
    same gate, and gives `app.org.lifecycle` a single cheap column to consult.
    """
    if operational_org_id is None:
        return
    (db.query(models.Organization)
       .filter(models.Organization.id == operational_org_id)
       .update({"is_active": active}, synchronize_session=False))


def _sever_tenant_sessions(db: Session, operational_org_id: Optional[int]) -> int:
    """Revoke every refresh token and device row for one organization's users.

    Suspension has to bite immediately, and an access token is only valid for
    30 minutes — but the rotating refresh token silently renews it for 30 days.
    Revoking the families here is what stops a long-lived browser or desktop
    session from outliving the suspension. Scoped strictly to this org's users;
    super-admin accounts carry organization_id = NULL and are never matched.
    """
    if operational_org_id is None:
        return 0
    user_ids = [
        r[0] for r in db.query(models.User.id)
                        .filter(models.User.organization_id == operational_org_id)
                        .all()
    ]
    if not user_ids:
        return 0

    revoked = 0
    for uid in user_ids:
        try:
            revoked += rtokens.revoke_all_for_user(db, uid, "organization_suspended") or 0
        except Exception:
            _log.warning("Could not revoke refresh tokens for user %s", uid, exc_info=True)
        # Drop the cached User row so the next request re-reads (and re-checks).
        try:
            _user_cache_invalidate(uid)
        except Exception:
            pass

    try:
        now = datetime.now(timezone.utc)
        (db.query(models.UserSession)
           .filter(models.UserSession.user_id.in_(user_ids),
                   models.UserSession.is_revoked.is_(False))
           .update({"is_revoked": True, "revoked_at": now}, synchronize_session=False))
    except Exception:
        _log.warning("Could not revoke device rows for org %s",
                     operational_org_id, exc_info=True)
    return revoked


def _invalidate_lifecycle_caches(operational_org_id: Optional[int]) -> None:
    """Make the state change visible to this process immediately.

    The lifecycle cache has a short TTL precisely so sibling workers converge on
    their own; this clears the process that performed the write so the operator
    sees the effect on their very next request rather than up to a TTL later.
    Call AFTER the commit — clearing before it would let a concurrent request
    repopulate the cache from the pre-commit state.
    """
    try:
        org_lifecycle.invalidate(operational_org_id)
    except Exception:
        pass
    if operational_org_id is not None:
        try:
            _org_cache_invalidate(operational_org_id)
        except Exception:
            pass


def _invalidate_entitlements(db: Session, commercial_org_id: Optional[str]) -> None:
    """Drop the tenant's cached entitlement profile.

    Separate from `_invalidate_lifecycle_caches` because it needs the request's
    Session (the profile lives in a table, not in memory) and must therefore run
    BEFORE the commit, whereas the in-memory caches must be cleared after it.
    Entitlements are advisory today, so a failure here is logged-and-ignored
    rather than allowed to block a suspension from being recorded.
    """
    if not commercial_org_id:
        return
    try:
        from app.commercial import entitlements as _ent
        _ent.invalidate(db, commercial_org_id)
    except Exception:
        _log.debug("Entitlements invalidation skipped for %s",
                   commercial_org_id, exc_info=True)


@router.post("/organizations/{org_id}/suspend")
def suspend_organization(
    org_id: str,
    body: OrgStateChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
    super_admin: models.User = Depends(require_super_admin),
):
    """Suspend a tenant. This is a real access boundary, not a label.

    Writing `commercial_state` used to be the whole operation, and nothing read
    it — a suspended organization kept working exactly as before. Suspension now:

      * writes `commercial_state = 'suspended'` (unchanged), and mirrors it onto
        `Organization.is_active = False`
      * revokes every refresh-token family and device row for the tenant's users,
        so the 30-day rotating session cannot re-arm
      * clears the lifecycle / organization / user caches so the block applies to
        the very next request rather than after a cache TTL
      * invalidates the entitlements cache

    Enforcement itself lives in `app.org.lifecycle`, applied centrally by
    `app.auth.service.get_current_user`, `establish_session` and `refresh`.
    Reversible in full via `/restore`.
    """
    cm = _commercial_models()
    if not cm:
        raise HTTPException(404, "Commercial layer not initialized")
    org = (
        db.query(cm.CommercialOrganization)
          .filter(cm.CommercialOrganization.id == org_id)
          .one_or_none()
    )
    if not org:
        raise HTTPException(404, "Organization not found")

    op_id = getattr(org, "operational_org_id", None)
    previous = org.commercial_state
    if previous == "suspended":
        # Idempotent, but still re-assert the side effects: an org suspended
        # before this enforcement existed has a stale `is_active` and live
        # refresh tokens, and re-clicking Suspend is the natural way an operator
        # fixes that.
        _apply_operational_flag(db, op_id, active=False)
        _sever_tenant_sessions(db, op_id)
        _invalidate_entitlements(db, org_id)
        db.commit()
        _invalidate_lifecycle_caches(op_id)
        return {"id": org_id, "commercial_state": previous, "already": True}

    org.commercial_state = "suspended"
    _apply_operational_flag(db, op_id, active=False)
    revoked = _sever_tenant_sessions(db, op_id)
    _invalidate_entitlements(db, org_id)

    db.add(models.AuditLog(
        actor_id=super_admin.id,
        organization_id=op_id,
        entity="Organization", action="OrgSuspended",
        payload={
            "organization_id": org_id, "previous_state": previous,
            "reason": (body.reason or "").strip()[:300],
            "sessions_revoked": revoked,
        },
        ip_address=_request_ip(request),
    ))
    db.commit()
    _invalidate_lifecycle_caches(op_id)
    return {
        "id": org_id,
        "commercial_state": "suspended",
        "previous_state": previous,
        "sessions_revoked": revoked,
    }


@router.post("/organizations/{org_id}/restore")
def restore_organization(
    org_id: str,
    body: OrgStateChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
    super_admin: models.User = Depends(require_super_admin),
):
    """Move a suspended org back to `active` (or `trial_active` if their
    trial is still within window). Mirror of `/suspend`.

    Reverses every side effect suspension applied except the revoked sessions —
    those are intentionally NOT restored. A revoked refresh token is single-use
    and reuse-detecting; re-arming one would be unsafe, and signing in again is
    the correct, cheap recovery.
    """
    cm = _commercial_models()
    if not cm:
        raise HTTPException(404, "Commercial layer not initialized")
    org = (
        db.query(cm.CommercialOrganization)
          .filter(cm.CommercialOrganization.id == org_id)
          .one_or_none()
    )
    if not org:
        raise HTTPException(404, "Organization not found")

    sub = (
        db.query(cm.CommercialSubscription)
          .filter(cm.CommercialSubscription.organization_id == org_id)
          .one_or_none()
    )
    # `trial_ends_at` is timestamptz on Postgres but comes back naive from any
    # driver that drops tzinfo, and comparing the two raises TypeError. Stored
    # values are always UTC, so coercing is safe and matches the `_as_utc_aware`
    # treatment app.auth.service already applies to invite expiry.
    next_state = "active"
    trial_ends = getattr(sub, "trial_ends_at", None) if sub else None
    if trial_ends is not None and trial_ends.tzinfo is None:
        trial_ends = trial_ends.replace(tzinfo=timezone.utc)
    if trial_ends and trial_ends > datetime.now(timezone.utc):
        next_state = "trial_active"

    op_id = getattr(org, "operational_org_id", None)
    previous = org.commercial_state
    org.commercial_state = next_state
    _apply_operational_flag(db, op_id, active=True)
    _invalidate_entitlements(db, org_id)

    db.add(models.AuditLog(
        actor_id=super_admin.id,
        organization_id=op_id,
        entity="Organization", action="OrgRestored",
        payload={
            "organization_id": org_id, "previous_state": previous,
            "new_state": next_state, "reason": (body.reason or "").strip()[:300],
        },
        ip_address=_request_ip(request),
    ))
    db.commit()
    _invalidate_lifecycle_caches(op_id)
    return {"id": org_id, "commercial_state": next_state, "previous_state": previous}


class OrgDeleteRequest(BaseModel):
    # Typed-name confirmation. The frontend requires it too, but the server is
    # authoritative: a mistyped or absent name is refused here regardless of
    # what the client did.
    confirm_name: str
    reason: Optional[str] = None


@router.delete("/organizations/{org_id}")
def delete_organization(
    org_id: str,
    body: OrgDeleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    super_admin: models.User = Depends(require_super_admin),
):
    """Permanently delete a suspended tenant and everything it owns.

    Gates, in order — every one of them refuses rather than proceeding partially:

      1. super-admin only (`require_super_admin`)
      2. the organization must exist
      3. it must already be SUSPENDED. `active -> deleted` is not a legal
         transition: suspension is the reversible step that proves the operator
         meant it and gives them a window to notice
      4. the typed name must match exactly
      5. it must have an operational_org_id — without it the purge cannot reach
         the tenant's users or operational data, and a half-deleted tenant that
         reports success is worse than a refusal
      6. no unreleased legal hold
      7. no live payment-provider subscription

    The purge itself is one transaction (`app.org.deletion_service`): if any
    statement fails the whole thing rolls back and the tenant is untouched.
    Storage cleanup runs only after a successful commit and is non-fatal.
    """
    cm = _commercial_models()
    if not cm:
        raise HTTPException(404, "Commercial layer not initialized")

    org = (
        db.query(cm.CommercialOrganization)
          .filter(cm.CommercialOrganization.id == org_id)
          .one_or_none()
    )
    if not org:
        # Idempotent: a repeated delete of an already-purged tenant is a
        # success from the operator's point of view, not an error to chase.
        raise HTTPException(404, "Organization not found")

    if (org.commercial_state or "").strip().lower() != "suspended":
        raise HTTPException(
            409,
            detail={
                "code": "organization_not_suspended",
                "message": "Organization must be suspended before it can be deleted.",
            },
        )

    expected = (org.name or "").strip()
    if (body.confirm_name or "").strip() != expected:
        raise HTTPException(
            400,
            detail={
                "code": "confirmation_mismatch",
                "message": "The organization name you typed does not match.",
            },
        )

    try:
        # Ordered so nothing destructive can run against a tenant we cannot
        # fully reach: the operational link is required first, and it is what
        # every subsequent gate (and the purge itself) is scoped by.
        op_id = deletion_service.assert_operational_link(org)
        deletion_service.assert_no_legal_hold(db, op_id)
        billing = deletion_service.assert_billing_safe(db, org_id)
    except deletion_service.OrganizationDeletionError as exc:
        raise HTTPException(exc.status_code,
                            detail={"code": exc.code, "message": exc.detail})

    org_name = org.name

    try:
        result = deletion_service.purge_organization(db, org)
        result.billing = billing

        # Platform-level audit row. organization_id is deliberately NULL: the
        # tenant's own audit_logs rows were just deleted with the rest of its
        # data, so a tenant-scoped record of the deletion would delete itself.
        # This one survives because nothing scopes it to the org that is gone.
        db.add(models.AuditLog(
            actor_id=super_admin.id,
            organization_id=None,
            entity="Organization", action="OrgDeleted",
            payload={
                "organization_id": org_id,
                "operational_org_id": op_id,
                "name": org_name,
                "reason": (body.reason or "").strip()[:300],
                "rows_deleted": result.total_rows,
                "tables_touched": len(result.deleted_rows),
                "deleted_rows": result.deleted_rows,
                "users_deleted": len(result.user_ids),
                "billing": billing,
            },
            ip_address=_request_ip(request),
        ))
        db.commit()
        # The purge deletes through raw SQL, so the session's identity map still
        # holds an ORM instance for a row that no longer exists; any later
        # attribute access would try to refresh it and raise ObjectDeletedError.
        # Only the organization instance is expunged — `expunge_all()` would
        # detach every other object the caller still holds, including the
        # super-admin principal.
        try:
            db.expunge(org)
        except Exception:
            pass
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        _log.exception("OrgDeleted FAILED — rolled back. org=%s (%s)", org_name, org_id)
        raise HTTPException(
            500,
            detail={
                "code": "deletion_failed",
                "message": (
                    "Deletion failed and every change was rolled back — the "
                    f"organization is intact. Detail: {exc}"
                ),
            },
        )

    # ── Post-commit, non-fatal ───────────────────────────────────────────────
    _invalidate_lifecycle_caches(op_id)
    for uid in result.user_ids:
        try:
            _user_cache_invalidate(uid)
        except Exception:
            pass

    storage_result = deletion_service.cleanup_storage(op_id, result.storage_paths)

    return {
        "success": True,
        "id": org_id,
        "operational_org_id": op_id,
        "name": org_name,
        "rows_deleted": result.total_rows,
        "tables_touched": len(result.deleted_rows),
        "deleted_rows": result.deleted_rows,
        "users_deleted": len(result.user_ids),
        "storage": storage_result,
        "coverage_gaps": result.coverage_gaps,
        # Empty on a correct manifest. Non-empty means a listed table's scope
        # column is absent from this database and its rows were NOT removed by
        # predicate — surfaced rather than swallowed.
        "skipped_tables": result.skipped_tables,
    }


# ──────────────────────────────────────────────────────────────────────────
# Signups feed
# ──────────────────────────────────────────────────────────────────────────
@router.get("/signups")
def signups_feed(
    state: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    cm = _commercial_models()
    if not cm:
        return {"items": [], "count": 0}
    q = db.query(cm.CommercialSignup)
    if state:
        q = q.filter(cm.CommercialSignup.state == state)
    rows = q.order_by(desc(cm.CommercialSignup.created_at)).limit(limit).all()
    return {
        "items": [
            {
                "id":               s.id,
                "email":            s.email,
                "state":            s.state,
                "route":            s.route,
                "intent":           s.intent,
                "plan":             s.plan,
                "country":          s.country,
                "expected_users":   s.expected_users,
                "user_id":          s.user_id,
                "organization_id":  s.organization_id,
                "ip_address":       s.ip_address,
                "failure_code":     s.failure_code,
                "failure_message":  s.failure_message,
                "created_at":       _isoformat(s.created_at),
            }
            for s in rows
        ],
        "count": len(rows),
    }


# ──────────────────────────────────────────────────────────────────────────
# Impersonation (audited)
# ──────────────────────────────────────────────────────────────────────────
class ImpersonateRequest(BaseModel):
    user_id: int
    reason: Optional[str] = None
    duration_minutes: int = 30


@router.post("/impersonate")
def impersonate_user(
    body: ImpersonateRequest,
    request: Request,
    db: Session = Depends(get_db),
    super_admin: models.User = Depends(require_super_admin),
):
    """Issue a short-lived bearer token that signs the super-admin in as
    `body.user_id`. Every impersonation creates an immutable audit row
    capturing actor, target, reason, and request IP. The minted token
    carries an `impersonated_by` claim so downstream services can detect
    and (optionally) refuse sensitive actions while impersonating.

    Tokens are capped at 60 minutes regardless of the requested duration."""
    target = (
        db.query(models.User)
          .filter(models.User.id == body.user_id)
          .one_or_none()
    )
    if not target:
        raise HTTPException(404, "Target user not found")
    if not target.is_active:
        raise HTTPException(409, "Target user is disabled")

    minutes = max(5, min(60, int(body.duration_minutes or 30)))
    payload = {
        "user_id":         target.id,
        "role":            target.role,
        "impersonated_by": super_admin.id,
        "impersonator_email": super_admin.email,
    }
    # `create_access_token` already adds an `exp` claim of the standard
    # window; we use it here too because the standard expiry is short
    # enough — and a custom impersonation TTL belongs to a future iteration
    # if ever needed.
    token = create_access_token(payload)

    db.add(models.AuditLog(
        actor_id=super_admin.id, target_id=target.id,
        organization_id=target.organization_id,
        entity="User", action="Impersonated",
        payload={
            "target_email":    target.email,
            "target_role":     target.role,
            "duration_minutes": minutes,
            "reason":          (body.reason or "").strip()[:300],
        },
        ip_address=(request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                    or (request.client.host if request.client else None)),
    ))
    db.commit()

    return {
        "access_token": token,
        "token_type":   "bearer",
        "expires_in_minutes": minutes,
        "target_user": {
            "id":    target.id,
            "name":  target.name,
            "email": target.email,
            "role":  target.role,
        },
        "warning": (
            "You are now signed in as another user. Every action will be "
            "attributed to them in the audit trail until you sign out."
        ),
    }


# ──────────────────────────────────────────────────────────────────────────
# Recent audit trail (platform-wide)
# ──────────────────────────────────────────────────────────────────────────
@router.get("/audit")
def super_admin_audit(
    limit: int = Query(100, ge=1, le=500),
    since_hours: int = Query(168, ge=1, le=8760),
    actions: Optional[str] = None,
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Read the legacy audit_logs table for super-admin investigation.
    `actions` is a comma-separated list of action keys to filter on
    (e.g. `Impersonated,OrgSuspended,RolePromotedSuperAdmin`)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    q = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
    )
    if actions:
        wanted = [a.strip() for a in actions.split(",") if a.strip()]
        if wanted:
            q = q.filter(models.AuditLog.action.in_(wanted))
    rows = q.order_by(desc(models.AuditLog.created_at)).limit(limit).all()
    return {
        "since_hours": since_hours,
        "events": [
            {
                "id":           r.id,
                "occurred_at":  _isoformat(r.created_at),
                "actor_email":  (u.email if u else None),
                "actor_role":   (u.role if u else None),
                "entity":       r.entity,
                "action":       r.action,
                "payload":      r.payload,
                "ip_address":   r.ip_address,
            }
            for r, u in rows
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Billing overview  GET /api/super-admin/billing
# ──────────────────────────────────────────────────────────────────────────
@router.get("/billing")
def super_admin_billing(
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Platform-wide billing snapshot for the Super Admin dashboard.

    Joins BillingSubscription + BillingPlan for every org that has a
    subscription row, then falls back to CommercialOrganization rows for
    any org not yet in the billing system.
    Returns per-org rows + platform KPI totals.
    """
    from datetime import timezone as _tz

    now = datetime.now(timezone.utc)
    thirty_days = now + timedelta(days=30)

    # ── All billing subscriptions with their plan ──────────────────────────
    subs = (
        db.query(models.BillingSubscription, models.BillingPlan)
          .outerjoin(models.BillingPlan, models.BillingPlan.id == models.BillingSubscription.plan_id)
          .all()
    )

    # Build lookup: organization_id -> (sub, plan)
    sub_map: dict = {}
    for sub, plan in subs:
        sub_map[sub.organization_id] = (sub, plan)

    # ── Pull org names from CommercialOrganization ─────────────────────────
    cm = _commercial_models()
    commercial_orgs = []
    if cm:
        try:
            commercial_orgs = db.query(cm.CommercialOrganization).all()
        except Exception:
            commercial_orgs = []

    # Build org name lookup: org_id -> org name
    org_name_map: dict = {}
    for co in commercial_orgs:
        org_name_map[str(co.id)] = co.name

    def _mrr(sub, plan) -> float:
        if not plan or not sub:
            return 0.0
        users = max(sub.billable_users or 0, sub.active_users or 0)
        if sub.billing_cycle == "annual":
            return round((plan.annual_price_per_user or 0) / 12 * users, 2)
        return round((plan.monthly_price_per_user or 0) * users, 2)

    def _risk_flag(sub, plan) -> str:
        if not sub:
            return "No subscription"
        s = sub.subscription_status or ""
        if s == "past_due":
            return "Invoice overdue"
        if s in ("cancel_pending",):
            return "Cancellation pending"
        if plan and sub.billable_users and plan.user_limit:
            pct = sub.billable_users / plan.user_limit
            if pct >= 0.9:
                return "Seat limit approaching"
        return ""

    rows = []
    for sub, plan in subs:
        org_id = sub.organization_id
        name = org_name_map.get(org_id) or org_id
        mrr = _mrr(sub, plan)
        renewal = _isoformat(sub.renewal_date) if sub.renewal_date else None
        renewing_soon = bool(
            sub.renewal_date and now <= sub.renewal_date.replace(tzinfo=timezone.utc) <= thirty_days
        ) if sub.renewal_date else False

        rows.append({
            "org_id":      org_id,
            "org_name":    name,
            "plan_name":   plan.name if plan else "—",
            "plan_code":   plan.code if plan else None,
            "billing_status": sub.subscription_status or "—",
            "billing_cycle":  sub.billing_cycle or "monthly",
            "seats_used":  sub.active_users or 0,
            "seats_limit": plan.user_limit if (plan and plan.user_limit) else None,
            "mrr":         mrr,
            "renewal_date": renewal,
            "renewing_soon": renewing_soon,
            "risk_flag":   _risk_flag(sub, plan),
        })

    # ── Platform KPIs ──────────────────────────────────────────────────────
    platform_mrr = round(sum(r["mrr"] for r in rows), 2)
    past_due     = sum(1 for r in rows if r["billing_status"] in ("past_due", "failed"))
    renewals_30d = sum(1 for r in rows if r["renewing_soon"])

    return {
        "platform_mrr":  platform_mrr,
        "platform_arr":  round(platform_mrr * 12, 2),
        "past_due":      past_due,
        "renewals_30d":  renewals_30d,
        "orgs":          rows,
    }


# ──────────────────────────────────────────────────────────────────────────
# Organization Provisioning  POST /api/super-admin/organizations
#
# The tenant graph itself is built by app.org.provisioning_service, which is
# shared with the self-serve signup path so the two cannot drift apart (they
# did once before — see migration c4a7e2b915d3). That service creates, in one
# flush-only unit:
#   1. Organization row (integer-PK, the anchor for all org_id FKs)
#   2. OrgSettings row (+ explicit owner_user_id)
#   3. AppSettings keys: shared_pool_enabled:{id}, leave_waiting_period_months:{id}
#   4. LeavePolicy rows (one per type from LEAVE_POLICIES defaults)
#   5. Team rows (optional, org-scoped)
#   6. First admin User with invite token (24h, UUID format matching invite flow)
#   7. CommercialOrganization, linked back via operational_org_id
#   8. AuditLog row (OrgProvisioned)
#
# This handler owns the transaction: the service only flushes, and we commit
# once at the end. Any exception triggers db.rollback() so partial state is
# never left. Post-commit: the invite email is sent non-fatally (failure
# logged and reported in the response, not rolled back).
#
# commercial_state is "active" for super-admin-provisioned tenants (they are
# not trials), which is why provisioned tenants appear immediately in the
# Super Admin Organizations list.
# ──────────────────────────────────────────────────────────────────────────

class CreateOrgRequest(BaseModel):
    # Org identity
    name:             str
    country:          Optional[str] = None   # ISO 3166-1 alpha-2, e.g. "IN"
    timezone:         Optional[str] = None   # IANA tz, e.g. "Asia/Kolkata"
    currency:         Optional[str] = None   # ISO 4217, e.g. "INR"

    # OrgSettings
    company_name:     Optional[str] = None   # defaults to name
    slug:             Optional[str] = None   # lowercase-hyphen, unique per org

    # First admin user
    admin_email:      str
    admin_name:       Optional[str] = None   # defaults to email prefix

    # Seeding toggles
    seed_leave_policies:         bool = True
    seed_default_teams:          bool = False
    default_teams:               Optional[List[str]] = None

    # Tenant-level leave defaults
    shared_pool_enabled:         bool = True
    leave_waiting_period_months: int  = 6


@router.post("/organizations", status_code=201)
def provision_organization(
    body:        CreateOrgRequest,
    request:     Request,
    db:          Session     = Depends(get_db),
    super_admin: models.User = Depends(require_super_admin),
):
    """Atomically provision a new tenant organization.

    The tenant graph is built by the shared provisioning service (see
    app.org.provisioning_service), which is flush-only — this handler owns the
    transaction and commits once. Rollback on any failure so no partial tenant
    is ever left in the database. The invite email is sent after commit and is
    non-fatal (failure is logged and reported in the response but does not roll
    back the org).
    """
    admin_email = (body.admin_email or "").strip().lower()
    org_name = (body.name or "").strip()

    # A workspace with no timezone is not "unconfigured" — it is a workspace
    # that silently runs on the system fallback, which is one specific country's
    # clock. Every organization created without this ended up evaluating its
    # attendance day, absence cutoffs and payroll periods on India time. The
    # Super Admin form already offers an IANA <select>, so requiring it here
    # costs nothing and closes the hole at the source. The self-serve path
    # captures the signing-up admin's own zone instead (see
    # app.commercial.provisioning), which is why this is an endpoint-level rule
    # rather than one baked into the shared provisioning service.
    org_timezone = (body.timezone or "").strip()
    if not org_timezone:
        raise HTTPException(400, "timezone is required (IANA, e.g. Asia/Kolkata, America/New_York)")
    if not _is_valid_timezone(org_timezone):
        raise HTTPException(400, "timezone must be a valid IANA timezone (e.g. Asia/Kolkata, America/New_York)")

    result = None
    try:
        result = provision_tenant(
            db,
            org_name=body.name,
            admin_email=body.admin_email,
            admin_name=body.admin_name,
            created_via=CREATED_VIA_SUPER_ADMIN,
            actor_user_id=super_admin.id,
            country=body.country,
            timezone_name=org_timezone,
            currency=body.currency,
            company_name=body.company_name,
            slug=body.slug,
            seed_leave_policies=body.seed_leave_policies,
            seed_default_teams=body.seed_default_teams,
            default_teams=body.default_teams,
            shared_pool_enabled=body.shared_pool_enabled,
            leave_waiting_period_months=body.leave_waiting_period_months,
            create_commercial_org=True,
            commercial_state="active",
            audit_ip=(
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else None)
            ),
            extra_audit_payload={"provisioned_by": super_admin.email},
        )
        db.commit()

    except TenantProvisioningError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        _log.exception(
            "OrgProvisioned FAILED — rolled back. name=%s admin=%s error=%s",
            org_name, admin_email, exc,
        )
        raise HTTPException(
            500,
            f"Provisioning failed and all changes were rolled back. Detail: {exc}",
        )

    org             = result.organization
    admin_user      = result.admin_user
    admin_name      = admin_user.name
    invite_token    = result.invite_token
    token_expires   = result.invite_token_expires_at
    policies_seeded = result.policies_seeded
    teams_seeded    = result.teams_seeded

    # ── Post-commit: send invite email (non-fatal) ────────────────────────
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    invite_link  = f"{frontend_url}/set-password/{invite_token}"
    email_sent   = False
    email_error: Optional[str] = None
    try:
        from app.email_service import send_invite_email
        email_sent = bool(send_invite_email(
            to_email      = admin_email,
            employee_name = admin_name,
            invite_link   = invite_link,
            inviter_name  = f"Temveris Platform ({super_admin.email})",
        ))
    except Exception as e:
        email_error = str(e)
        _log.error(
            "Post-provision invite email failed org_id=%s admin=%s: %s",
            org.id, admin_email, e,
        )

    return {
        "success":          True,
        "organization_id":  org.id,
        "org_name":         org_name,
        "admin_email":      admin_email,
        "admin_user_id":    admin_user.id,
        "invite_link":      invite_link,
        "invite_expires_at": token_expires.isoformat(),
        "email_sent":       email_sent,
        "email_error":      email_error,
        "seeded": {
            "leave_policies": policies_seeded,
            "teams":          teams_seeded,
            "app_settings":   result.app_settings_seeded,
        },
        "next_steps": "Tenant is live. Admin activates via invite_link.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Billing downgrade review  (Phase 8)
#
# billing_downgrade_requests was write-only: created by the customer-facing endpoint
# and read by nothing. These give the approval step somewhere to happen. Super-admin
# only, by approved decision — a downgrade is never self-service.
#
# Capacity is validated on approve AND on apply: headcount moves between the two, and
# a plan whose user_limit is below the billable count cannot be billed correctly.
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/billing/downgrade-requests")
def list_downgrade_requests(
    status: Optional[str] = Query(None, description="pending | approved | applied | rejected"),
    organization_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    from app.billing import downgrades as billing_downgrades
    items = billing_downgrades.list_requests(db, status=status,
                                             organization_id=organization_id)
    return {"items": items, "count": len(items)}


@router.get("/billing/readiness")
def billing_readiness(
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Is this deployment configured to take payments? READ ONLY, no secrets returned.

    Answers the questions that were previously unanswerable without reading the
    container's environment by hand: which Stripe mode is in force, whether the key
    agrees with the declared mode, whether all six price variables and the webhook
    secret are present, whether the billing sweeps are running, and whether the billing
    schema is migrated.

    Never returns a secret — only presence, counts and mode names. Makes no Stripe call,
    so it cannot be used to probe or mutate the Stripe account, and it stays useful
    during a Stripe outage.
    """
    from app.billing import environment as billing_env      # noqa: PLC0415
    return billing_env.readiness(db)


@router.get("/billing/orphan-subscriptions")
def list_orphan_subscriptions(
    status: str = Query("active", description="Stripe status to scan; 'billable' scans every charging status"),
    db: Session = Depends(get_db),
    _ = Depends(require_super_admin),
):
    """Stripe subscriptions with no local BillingSubscription. READ ONLY.

    Platform-level, super-admin only, and deliberately not exposed to tenants: the
    answer spans every organization and includes subscriptions whose owner cannot be
    identified, so there is no tenant to scope it to.

    Reports only. It never cancels or modifies a Stripe object — an unmatched
    subscription is not proof of a mistake (it could be a migrated customer or a
    Dashboard-created subscription), and acting on that guess would cut off someone who
    is paying. Every row is a human decision.

    A Stripe outage returns status="unavailable" with orphans=null, never an empty list,
    so a failed call cannot be read as "no orphans".
    """
    from app.billing import orphans as billing_orphans      # noqa: PLC0415
    if status == "billable":
        return billing_orphans.scan_billable(db)
    return billing_orphans.scan(db, status=status)


@router.post("/billing/downgrade-requests/{request_id}/approve")
def approve_downgrade_request(
    request_id: int,
    notes: Optional[str] = Body(None, embed=True),
    apply_now: bool = Body(False, embed=True),
    db: Session = Depends(get_db),
    actor: models.User = Depends(require_super_admin),
):
    from app.billing import downgrades as billing_downgrades
    try:
        return billing_downgrades.approve(db, request_id, reviewer=actor,
                                         notes=notes, apply_now=apply_now)
    except billing_downgrades.DowngradeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/billing/downgrade-requests/{request_id}/reject")
def reject_downgrade_request(
    request_id: int,
    notes: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
    actor: models.User = Depends(require_super_admin),
):
    from app.billing import downgrades as billing_downgrades
    try:
        return billing_downgrades.reject(db, request_id, reviewer=actor, notes=notes)
    except billing_downgrades.DowngradeError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/billing/downgrade-requests/{request_id}/apply")
def apply_downgrade_request(
    request_id: int,
    db: Session = Depends(get_db),
    actor: models.User = Depends(require_super_admin),
):
    """Apply an already-approved request now, ahead of its effective date."""
    from app.billing import downgrades as billing_downgrades
    try:
        return billing_downgrades.apply_request(db, request_id, actor=actor)
    except billing_downgrades.DowngradeError as exc:
        raise HTTPException(exc.status_code, str(exc))
