"""
Temveris – Intelligence Engine Router
Risk Users + Productivity Insights + Workforce Intelligence (workflows,
focus, burnout, context-switch, daily summary, team pulse).

Multitenancy isolation
----------------------
All risk computations are scoped to the caller's organization_id.
Org A employees are never included in org B's risk report.
"""

from datetime import datetime, timedelta, timezone, date
from typing import Optional


from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.database import get_db
from app.auth.service import get_current_user
from database import models
from .risk_engine import compute_risk_users
from .workflow import detect_workflows, materialise_transitions
from .focus import detect_focus_sessions, materialise_focus_sessions, DEEP_FOCUS_MINUTES
from .burnout import compute_burnout, context_switch_pulse
from .summarizer import generate_daily_summary
from app.commercial.enforcement import (
    require_capability,
    require_writable_capability,
)

router = APIRouter(prefix="/intelligence", tags=["Intelligence"])


# ─── helpers ──────────────────────────────────────────────────────────
def _day_bounds(date_str: Optional[str]):
    if date_str:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
    else:
        target = datetime.now(timezone.utc).date()
    start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return target, start, end


def _gate_user_access(viewer: models.User, target_user_id: int):
    # Employees can only see their own intelligence; admins/HR/managers all see anyone.
    if viewer.role == "employee" and viewer.id != target_user_id:
        raise HTTPException(403, "Access denied")


def _role_hint(user: models.User) -> str:
    return " ".join(filter(None, [
        getattr(user, "designation", None),
        getattr(user, "department", None),
        getattr(user, "team", None),
    ])).strip()


def _get_user_in_org(db: Session, user_id: int, org_id: int) -> models.User:
    """Fetch a user only if they belong to the caller's org. Returns 404 otherwise."""
    user = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == org_id,
    ).first()
    if not user:
        raise HTTPException(404, "User not found")
    return user


# ─── Existing risk endpoints (unchanged) ──────────────────────────────
@router.get("/risk-users",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def risk_users(
    period_days: int = Query(default=7, ge=1, le=90, description="Analysis window in days"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Intelligence Engine: returns employees flagged as at-risk.
    Includes confidence score, reason codes, and explainability payload.
    Only employees from the caller's organization are included.
    """
    # Observation is provided by the rollout-gated require_capability dependency
    # (Phase 4.3 self-observing gate), so the manual activity.anomaly shadow hook
    # that used to sit here has been consolidated away to avoid double-logging.
    results = compute_risk_users(db, period_days=period_days, organization_id=current_user.organization_id)
    return {
        "status":       "success",
        "count":        len(results),
        "period_days":  period_days,
        "risk_users":   results,
    }


@router.get("/risk-users/{user_id}",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def risk_user_detail(
    user_id: int,
    period_days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Risk detail for a single user. Returns 404 if the user does not belong
    to the caller's organization (avoids leaking cross-org existence).
    """
    results = compute_risk_users(db, period_days=period_days, organization_id=current_user.organization_id)
    user_result = next((r for r in results if r["user_id"] == user_id), None)
    if not user_result:
        return {"status": "ok", "message": "No risk signals detected for this user in the period"}
    return {"status": "success", "data": user_result}


# ─── Workforce Intelligence ───────────────────────────────────────────
@router.get("/workflows",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_workflows(
    user_id: int = Query(...),
    date_str: Optional[str] = Query(None, alias="date"),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Reconstructed workflow sequences for a user on a given date.

    Each workflow is a cluster of tab sessions tagged with a role-aware
    label (e.g. 'Backend engineering', 'Recruiting'). Drives the
    WorkflowRibbon component on the cinematic admin view.
    """
    _gate_user_access(viewer, user_id)
    user = _get_user_in_org(db, user_id, viewer.organization_id)

    _, day_start, day_end = _day_bounds(date_str)
    workflows = detect_workflows(
        db, user_id, day_start, day_end,
        user_role_hint=_role_hint(user),
    )
    return {
        "user_id":   user_id,
        "date":      day_start.date().isoformat(),
        "count":     len(workflows),
        "workflows": [w.as_dict() for w in workflows],
    }


@router.get("/focus-sessions",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_focus_sessions(
    user_id: int = Query(...),
    date_str: Optional[str] = Query(None, alias="date"),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Detected focus blocks for the FocusTimeline component."""
    _gate_user_access(viewer, user_id)
    _get_user_in_org(db, user_id, viewer.organization_id)
    _, day_start, day_end = _day_bounds(date_str)
    blocks = detect_focus_sessions(db, user_id, day_start, day_end)
    deep_min = sum(b.duration_sec for b in blocks if b.is_deep) // 60
    total_min = sum(b.duration_sec for b in blocks) // 60
    return {
        "user_id":            user_id,
        "date":               day_start.date().isoformat(),
        "deep_focus_min":     deep_min,
        "total_focus_min":    total_min,
        "deep_threshold_min": DEEP_FOCUS_MINUTES,
        "blocks":             [b.as_dict() for b in blocks],
    }


@router.get("/burnout",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_burnout(
    user_id: int = Query(...),
    period_days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Burnout risk report — explainable signals + 0..1 score."""
    _gate_user_access(viewer, user_id)
    _get_user_in_org(db, user_id, viewer.organization_id)
    report = compute_burnout(db, user_id, period_days=period_days)
    return report.as_dict()


@router.get("/context-switches",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_context_switches(
    user_id: int = Query(...),
    date_str: Optional[str] = Query(None, alias="date"),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Hourly tab-switch counts + overload flag."""
    _gate_user_access(viewer, user_id)
    _get_user_in_org(db, user_id, viewer.organization_id)
    _, day_start, day_end = _day_bounds(date_str)
    return {
        "user_id": user_id,
        "date":    day_start.date().isoformat(),
        **context_switch_pulse(db, user_id, day_start, day_end),
    }


@router.get("/daily-summary",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_daily_summary(
    user_id: int = Query(...),
    date_str: Optional[str] = Query(None, alias="date"),
    force: bool = Query(False, description="Bypass cache and recompute"),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """AI-generated daily summary (Claude if ANTHROPIC_API_KEY is set,
    rule-based fallback otherwise). Cached in `daily_intelligence_summaries`."""
    _gate_user_access(viewer, user_id)
    _get_user_in_org(db, user_id, viewer.organization_id)
    target, _, _ = _day_bounds(date_str)
    return generate_daily_summary(db, user_id, target, force=force)


@router.post("/materialise",
             dependencies=[Depends(require_writable_capability("activity.anomaly"))])
def materialise_intelligence(
    user_id: int = Query(...),
    date_str: Optional[str] = Query(None, alias="date"),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Recompute & persist focus_sessions + workflow_transitions +
    daily_intelligence_summaries for one user/date. Idempotent. Intended
    to be called by an end-of-shift cron or by an admin "Recompute"
    button on the cinematic view."""
    _gate_user_access(viewer, user_id)
    _get_user_in_org(db, user_id, viewer.organization_id)
    target, day_start, day_end = _day_bounds(date_str)
    fs_count = materialise_focus_sessions(db, user_id, day_start, day_end)
    tr_count = materialise_transitions(db, user_id, day_start, day_end)
    summary = generate_daily_summary(db, user_id, target, force=True)
    return {
        "user_id":          user_id,
        "date":             day_start.date().isoformat(),
        "focus_sessions":   fs_count,
        "transitions":      tr_count,
        "summary_generated": bool(summary.get("narrative")),
    }


# ─── Team-level aggregation (Phase 5) ─────────────────────────────────
@router.get("/team-pulse",
            dependencies=[Depends(require_capability("activity.anomaly"))])
def get_team_pulse(
    period_days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Org-wide rollup: burnout band distribution, top deep-focus performers,
    avg context-switching, productive % distribution. Admin/HR only."""
    if viewer.role not in ("admin", "hr", "manager"):
        raise HTTPException(403, "Admin or HR access required")

    users = (
        db.query(models.User)
        .filter(models.User.organization_id == viewer.organization_id)
        .filter(models.User.is_active.is_(True))
        .filter(models.User.role.in_(("employee", "manager", "hr")))
        .all()
    )

    bands = {"low": 0, "moderate": 0, "high": 0}
    per_user = []
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    today_end = today_start + timedelta(days=1)

    for u in users:
        burnout = compute_burnout(db, u.id, period_days=period_days, now=now)
        bands[burnout.risk_band] = bands.get(burnout.risk_band, 0) + 1
        focus_blocks = detect_focus_sessions(db, u.id, today_start, today_end)
        deep_min_today = sum(b.duration_sec for b in focus_blocks if b.is_deep) // 60
        per_user.append({
            "user_id":            u.id,
            "name":               u.name,
            "department":         u.department,
            "designation":        u.designation,
            "risk_band":          burnout.risk_band,
            "risk_score":         burnout.risk_score,
            "deep_min_today":     deep_min_today,
            "switch_rate_per_hr": burnout.metrics.get("switch_rate_per_hr", 0),
            "total_clocked_hours": burnout.metrics.get("total_clocked_hours", 0),
        })

    # Sort: highest risk first, then by deep work
    per_user.sort(key=lambda p: (-p["risk_score"], -p["deep_min_today"]))
    top_focus = sorted(per_user, key=lambda p: -p["deep_min_today"])[:5]

    return {
        "period_days": period_days,
        "team_size":   len(users),
        "bands":       bands,
        "elevated_count": bands["moderate"] + bands["high"],
        "top_deep_focus": top_focus,
        "people":      per_user,
    }
