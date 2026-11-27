"""
Temveris – Payroll Router
============================
Admin endpoints for managing employee payroll configs and running payroll.
All endpoints are tenant-scoped: every query is filtered by the caller's
organization_id and cross-org ownership is rejected with HTTP 404 so
existence of another tenant's data is never revealed.

Endpoints:
  GET    /payroll/configs                    – list all employee payroll configs
  GET    /payroll/configs/{user_id}          – get single config
  POST   /payroll/configs                    – create/update payroll config
  DELETE /payroll/configs/{user_id}          – remove config
  GET    /payroll/summary                    – dashboard summary stats
  GET    /payroll/runs                       – list payroll runs
  GET    /payroll/runs/{run_id}              – run detail with items
  POST   /payroll/runs                       – create new payroll run
  POST   /payroll/runs/{run_id}/calculate    – recompute totals
  POST   /payroll/runs/{run_id}/submit-review – mark needs_review
  POST   /payroll/runs/{run_id}/approve      – mark approved
  POST   /payroll/runs/{run_id}/lock         – mark locked
  POST   /payroll/runs/{run_id}/export       – produce export record
  PUT    /payroll/runs/{run_id}/process      – legacy: mark processed
  PUT    /payroll/runs/{run_id}/pay          – mark paid
  GET    /payroll/adjustments                – list adjustments (stub)
  POST   /payroll/adjustments                – create adjustment (stub)
  POST   /payroll/adjustments/{id}/approve   – approve adjustment (stub)
  POST   /payroll/adjustments/{id}/reject    – reject adjustment (stub)
  GET    /payroll/exceptions                 – list exceptions (stub)
  POST   /payroll/exceptions/{id}/resolve    – resolve exception (stub)
  GET    /payroll/exports                    – list exports (stub)
  GET    /payroll/settings                   – module settings
  PATCH  /payroll/settings                   – save module settings
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, Any

from database.database import get_db
from app.commercial.enforcement import (
    require_capability,
    require_writable_capability,
)
from database import models
from app import _tz
from app._worked import session_worked_seconds
from app.auth.service import get_current_user
from app.payroll import service as payroll_service
from app.payroll.schemas import PayrollConfigCreate, PayrollConfigUpdate, PayrollRunCreate

router = APIRouter(prefix="/payroll", tags=["Payroll"])


def _require_admin(user: models.User):
    if user.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin/HR can manage payroll")


def _assert_run_org(run: models.PayrollRun, user: models.User):
    """Return 404 if run belongs to a different org."""
    if run.organization_id is not None and run.organization_id != user.organization_id:
        raise HTTPException(404, "Payroll run not found")


# ── Employee self-serve earnings ───────────────────────────────────────────────

@router.get("/my-config")
def my_payroll_config(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Returns the current employee's own payroll config, computed earnings,
    and period-level worked-hours summary. Worker-scoped — no admin role needed."""
    from sqlalchemy import and_

    cfg = payroll_service.get_config(db, user.id)

    # ── Period boundaries (IST) ────────────────────────────────────────────────
    now_ist = _tz.local_now(db, getattr(user, "organization_id", None))
    today   = now_ist.date()
    freq    = (cfg.pay_frequency if cfg else None) or "monthly"

    if freq == "bi_weekly":
        # Which fortnight today falls in depends entirely on the anchor. It was
        # a fixed date(2024, 1, 1) — one employer's convention applied to all.
        from app.payroll.policy import biweekly_anchor
        days_since_epoch = (today - biweekly_anchor(db, user)).days
        offset       = days_since_epoch % 14
        period_start = today - timedelta(days=offset)
        period_end   = period_start + timedelta(days=13)
    elif freq == "weekly":
        # The organization's week start, not a hard-coded Monday — a weekly pay
        # period that splits the working week pays half of it in each run.
        from app.schedule_policy import week_start_date
        period_start = week_start_date(db, _org_id_of(user), today)
        period_end   = period_start + timedelta(days=6)
    else:
        period_start = today.replace(day=1)
        next_month   = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        period_end   = next_month - timedelta(days=1)

    # A payroll period runs from local midnight to local midnight for THIS
    # organization. A fixed +05:30 boundary shifted every other tenant's period
    # by hours, moving sessions between pay periods.
    _org_id = getattr(user, "organization_id", None)
    period_start_utc, _ = _tz.local_day_bounds(db, _org_id, period_start)
    _, period_end_utc   = _tz.local_day_bounds(db, _org_id, period_end)

    sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_in_time  >= period_start_utc,
            # The upper bound was missing, so every session from the period
            # start onward was counted — including future periods.
            models.ClockSession.clock_in_time  <  period_end_utc,
            models.ClockSession.clock_out_time != None,  # noqa: E711
        )
    ).all()

    # Elapsed-minus-nothing was this page's own arithmetic: it counted breaks
    # and connectivity gaps as paid time, so an employee's self-serve earnings
    # estimate exceeded what the payroll run computed for the same period.
    worked_secs_approved = sum(session_worked_seconds(s) for s in sessions)

    open_session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_out_time == None,  # noqa: E711
        )
    ).order_by(models.ClockSession.id.desc()).first()

    worked_secs_today = 0
    if open_session:
        worked_secs_today = session_worked_seconds(open_session)

    worked_hours_approved = round(worked_secs_approved / 3600, 2)
    worked_hours_today    = round(worked_secs_today    / 3600, 2)

    if cfg:
        gross      = payroll_service.compute_gross(cfg)
        deductions = payroll_service.compute_deductions(cfg)
        net        = payroll_service.compute_net(cfg)

        if cfg.pay_type == "hourly" and cfg.hourly_rate:
            rate            = float(cfg.hourly_rate or 0)
            gross           = round(rate * worked_hours_approved, 2)
            # The hourly path used to apply a flat 10% deduction to every
            # tenant. Deduction rates are jurisdiction- and contract-specific,
            # so we use THIS employee's configured pf + tax, exactly as the
            # salaried path does, and deduct nothing when nothing is configured.
            deductions      = round(float(cfg.pf or 0) + float(cfg.tax or 0), 2)
            net             = round(gross - deductions, 2)
            estimated_gross = gross
        else:
            period_days     = max(1, (period_end - period_start).days + 1)
            elapsed         = min((today - period_start).days + 1, period_days)
            prorate         = elapsed / period_days
            estimated_gross = round(gross * prorate, 2)
    else:
        gross = deductions = net = estimated_gross = 0

    return {
        "config": {
            "pay_type":              cfg.pay_type        if cfg else None,
            "hourly_rate":           float(cfg.hourly_rate) if cfg and cfg.hourly_rate else None,
            "basic_salary":          float(cfg.basic_salary) if cfg and cfg.basic_salary else 0,
            "hra":                   float(cfg.hra)   if cfg and cfg.hra   else 0,
            "da":                    float(cfg.da)    if cfg and cfg.da    else 0,
            "ta":                    float(cfg.ta)    if cfg and cfg.ta    else 0,
            "pf":                    float(cfg.pf)    if cfg and cfg.pf    else 0,
            "tax":                   float(cfg.tax)   if cfg and cfg.tax   else 0,
            "bonus":                 float(cfg.bonus) if cfg and cfg.bonus else 0,
            # Falls back to the organization's payroll currency setting, not
            # to one country's. Empty when neither is configured — an unlabelled
            # number is better than a number labelled in the wrong currency.
            "currency":              (cfg.currency if cfg else None) or _org_currency(db, user),
            "pay_frequency":         freq,
            "gross":                 float(gross),
            "deductions":            float(deductions),
            "net":                   float(net),
            "estimated_gross":       float(estimated_gross),
            "period_start":          period_start.isoformat(),
            "period_end":            period_end.isoformat(),
            "worked_hours_approved": worked_hours_approved,
            "worked_hours_today":    worked_hours_today,
            "has_config":            cfg is not None,
        }
    }


# ── Configs ────────────────────────────────────────────────────────────────────

@router.get("/configs",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def list_configs(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """List all active payroll configs for the caller's org."""
    # Observation is provided by the rollout-gated require_capability dependency
    # (Phase 4.3 self-observing gate); the manual payroll.calculator shadow hook
    # that used to sit here has been consolidated away to avoid double-logging.
    # `_require_admin` remains the separate ACTOR (RBAC) check.
    _require_admin(user)
    configs = (
        db.query(models.PayrollConfig)
        .options(joinedload(models.PayrollConfig.user))
        .filter(
            models.PayrollConfig.is_active == True,
            models.PayrollConfig.organization_id == user.organization_id,
        )
        .all()
    )
    result = []
    for c in configs:
        u = c.user
        result.append({
            "id":           c.id,
            "user_id":      c.user_id,
            "user_name":    u.name  if u else "Unknown",
            "user_email":   u.email if u else "",
            "department":   u.department  if u else "",
            "designation":  u.designation if u else "",
            "pay_type":     c.pay_type,
            "hourly_rate":  c.hourly_rate,
            "basic_salary": c.basic_salary,
            "hra":          c.hra,
            "da":           c.da,
            "ta":           c.ta,
            "pf":           c.pf,
            "tax":          c.tax,
            "bonus":        c.bonus,
            "currency":     c.currency,
            "pay_frequency": c.pay_frequency,
            "bank_account": c.bank_account,
            "notes":        c.notes,
            "gross":        payroll_service.compute_gross(c),
            "deductions":   payroll_service.compute_deductions(c),
            "net":          payroll_service.compute_net(c),
        })
    return {"configs": result}


@router.get("/configs/{user_id}",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def get_config(
    user_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    cfg = payroll_service.get_config(db, user_id)
    if not cfg:
        return {"config": None}
    # Org isolation: admins must not see another org's salary/bank data.
    if cfg.organization_id is not None and cfg.organization_id != user.organization_id:
        return {"config": None}
    u = db.query(models.User).filter(models.User.id == user_id).first()
    return {"config": {
        "id":           cfg.id,
        "user_id":      cfg.user_id,
        "user_name":    u.name if u else "Unknown",
        "pay_type":     cfg.pay_type,
        "hourly_rate":  cfg.hourly_rate,
        "basic_salary": cfg.basic_salary,
        "hra":          cfg.hra,
        "da":           cfg.da,
        "ta":           cfg.ta,
        "pf":           cfg.pf,
        "tax":          cfg.tax,
        "bonus":        cfg.bonus,
        "currency":     cfg.currency,
        "pay_frequency": cfg.pay_frequency,
        "bank_account": cfg.bank_account,
        "notes":        cfg.notes,
        "gross":        payroll_service.compute_gross(cfg),
        "deductions":   payroll_service.compute_deductions(cfg),
        "net":          payroll_service.compute_net(cfg),
    }}


@router.post("/configs",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def upsert_config(
    body: PayrollConfigCreate,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    # The target employee must belong to the same org.
    target = db.query(models.User).filter(models.User.id == body.user_id).first()
    if not target:
        raise HTTPException(404, "Employee not found")
    if target.organization_id is not None and target.organization_id != user.organization_id:
        raise HTTPException(404, "Employee not found")
    data = body.dict(exclude={"user_id"})
    cfg = payroll_service.upsert_config(db, body.user_id, data, organization_id=user.organization_id)
    return {
        "success": True,
        "config": {
            "id":           cfg.id,
            "user_id":      cfg.user_id,
            "basic_salary": cfg.basic_salary,
            "gross":        payroll_service.compute_gross(cfg),
            "net":          payroll_service.compute_net(cfg),
        },
    }


@router.delete("/configs/{user_id}",
               dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def delete_config(
    user_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    cfg = payroll_service.get_config(db, user_id)
    if not cfg or (cfg.organization_id is not None and cfg.organization_id != user.organization_id):
        raise HTTPException(404, "No payroll config found for this user")
    payroll_service.delete_config(db, user_id)
    return {"success": True}


# ── Summary ────────────────────────────────────────────────────────────────────

@router.get("/summary",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def payroll_summary(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    return payroll_service.get_payroll_summary(db, organization_id=user.organization_id)


# ── Payroll Runs ───────────────────────────────────────────────────────────────

@router.get("/runs",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def list_runs(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    runs = payroll_service.get_payroll_runs(db, organization_id=user.organization_id)
    return {"runs": [{
        "id":           r.id,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end":   r.period_end.isoformat()   if r.period_end   else None,
        "status":       r.status,
        "total_amount": r.total_amount,
        "currency":     r.currency,
        "notes":        r.notes,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
        "item_count":   len(r.items) if r.items else 0,
    } for r in runs]}


@router.get("/runs/{run_id}",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def get_run(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Run detail with line items."""
    _require_admin(user)
    run = payroll_service.get_payroll_run(db, run_id)
    if not run:
        raise HTTPException(404, "Payroll run not found")
    _assert_run_org(run, user)
    items = (
        db.query(models.PayrollItem)
        .options(joinedload(models.PayrollItem.user))
        .filter(models.PayrollItem.payroll_run_id == run_id)
        .all()
    )
    return {
        "run": {
            "id":           run.id,
            "period_start": run.period_start.isoformat() if run.period_start else None,
            "period_end":   run.period_end.isoformat()   if run.period_end   else None,
            "status":       run.status,
            "total_amount": run.total_amount,
            "currency":     run.currency,
            "notes":        run.notes,
            "item_count":   len(items),
        },
        "items": [{
            "id":             i.id,
            "user_id":        i.user_id,
            "user_name":      i.user.name  if i.user else "Unknown",
            "user_email":     i.user.email if i.user else "",
            "worked_hours":   i.worked_hours,
            "basic_salary":   i.basic_salary,
            "hra":            i.hra,
            "da":             i.da,
            "ta":             i.ta,
            "bonus":          i.bonus,
            "gross_pay":      i.gross_pay,
            "pf_deduction":   i.pf_deduction,
            "tax_deduction":  i.tax_deduction,
            "net_pay":        i.net_pay,
            "status":         i.status,
            "paid_at":        i.paid_at.isoformat() if i.paid_at else None,
        } for i in items],
    }


@router.post("/runs",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def create_run(
    body: PayrollRunCreate,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    try:
        ps = datetime.fromisoformat(body.period_start.replace("Z", "+00:00"))
        pe = datetime.fromisoformat(body.period_end.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "Invalid date format")
    run = payroll_service.create_payroll_run(
        db, ps, pe, user.id, body.notes,
        organization_id=user.organization_id,
    )
    return {
        "success":      True,
        "run_id":       run.id,
        "total_amount": run.total_amount,
        "status":       run.status,
    }


@router.put("/runs/{run_id}/process",
            dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def process_run(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    run = payroll_service.get_payroll_run(db, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    _assert_run_org(run, user)
    run = payroll_service.mark_run_processed(db, run_id)
    return {"success": True, "status": run.status}


@router.put("/runs/{run_id}/pay",
            dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def pay_run(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    run = payroll_service.get_payroll_run(db, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    _assert_run_org(run, user)
    run = payroll_service.mark_run_paid(db, run_id)
    return {"success": True, "status": run.status}


# ── Workflow transitions ───────────────────────────────────────────────────────

def _set_run_status(
    db:              Session,
    run_id:          int,
    status:          str,
    organization_id: Optional[int] = None,
) -> models.PayrollRun:
    run = payroll_service.get_payroll_run(db, run_id)
    if not run or (
        organization_id is not None
        and run.organization_id is not None
        and run.organization_id != organization_id
    ):
        raise HTTPException(404, "Run not found")
    run.status = status
    db.commit()
    db.refresh(run)
    return run


@router.post("/runs/{run_id}/calculate",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def calculate_run(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Recompute totals for an existing run."""
    _require_admin(user)
    run = payroll_service.get_payroll_run(db, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    _assert_run_org(run, user)
    items = payroll_service.get_run_items(db, run_id)
    run.total_amount = round(sum((i.net_pay or 0) for i in items), 2)
    if run.status == "draft":
        run.status = "calculating"
    db.commit()
    db.refresh(run)
    return {"success": True, "status": run.status, "total_amount": run.total_amount}


@router.post("/runs/{run_id}/submit-review",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def submit_review(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    run = _set_run_status(db, run_id, "needs_review", organization_id=user.organization_id)
    return {"success": True, "status": run.status}


@router.post("/runs/{run_id}/approve",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def approve_run(
    run_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    run = _set_run_status(db, run_id, "approved", organization_id=user.organization_id)
    return {"success": True, "status": run.status}


@router.post("/runs/{run_id}/lock",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def lock_run(
    run_id: int,
    body:   Dict[str, Any] = None,
    db:     Session        = Depends(get_db),
    user:   models.User    = Depends(get_current_user),
):
    _require_admin(user)
    run = _set_run_status(db, run_id, "locked", organization_id=user.organization_id)
    if body and isinstance(body, dict) and body.get("reason"):
        existing  = run.notes or ""
        run.notes = (existing + "\n[LOCK] " + str(body["reason"])).strip()
        db.commit()
        db.refresh(run)
    return {"success": True, "status": run.status}


@router.post("/runs/{run_id}/export",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def export_run(
    run_id: int,
    body:   Dict[str, Any] = None,
    db:     Session        = Depends(get_db),
    user:   models.User    = Depends(get_current_user),
):
    _require_admin(user)
    run = payroll_service.get_payroll_run(db, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    _assert_run_org(run, user)
    fmt  = (body or {}).get("format", "csv")
    kind = (body or {}).get("type",   "payroll")
    if run.status not in ("paid", "locked", "exported"):
        run.status = "exported"
        db.commit()
        db.refresh(run)
    return {
        "success": True,
        "status":  run.status,
        "export": {
            "id":         f"exp_{run.id}_{int(datetime.now(timezone.utc).timestamp())}",
            "run_id":     run.id,
            "format":     fmt,
            "type":       kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": user.id,
            "file_url":   None,
        },
    }


# ── Adjustments / Exceptions / Exports — pre-launch stubs ─────────────────────

@router.get("/adjustments",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def list_adjustments(
    run_id: Optional[int] = Query(None),
    db:     Session       = Depends(get_db),
    user:   models.User   = Depends(get_current_user),
):
    _require_admin(user)
    return {"adjustments": [], "run_id": run_id}


@router.post("/adjustments",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def create_adjustment(
    body: Dict[str, Any],
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    return {
        "success": True,
        "adjustment": {
            "id":         int(datetime.now(timezone.utc).timestamp() * 1000),
            "status":     "pending",
            "created_by": user.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **(body or {}),
        },
    }


@router.post("/adjustments/{adj_id}/approve",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def approve_adjustment(
    adj_id: int,
    db:     Session     = Depends(get_db),
    user:   models.User = Depends(get_current_user),
):
    _require_admin(user)
    return {"success": True, "id": adj_id, "status": "approved"}


@router.post("/adjustments/{adj_id}/reject",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def reject_adjustment(
    adj_id: int,
    db:     Session     = Depends(get_db),
    user:   models.User = Depends(get_current_user),
):
    _require_admin(user)
    return {"success": True, "id": adj_id, "status": "rejected"}


@router.get("/exceptions",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def list_exceptions(
    run_id: Optional[int] = Query(None),
    db:     Session       = Depends(get_db),
    user:   models.User   = Depends(get_current_user),
):
    _require_admin(user)
    return {"exceptions": [], "run_id": run_id}


@router.post("/exceptions/{exc_id}/resolve",
             dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def resolve_exception(
    exc_id: int,
    body:   Dict[str, Any] = None,
    db:     Session        = Depends(get_db),
    user:   models.User    = Depends(get_current_user),
):
    _require_admin(user)
    return {"success": True, "id": exc_id, "status": "resolved", "note": (body or {}).get("note")}


@router.get("/exports",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def list_exports(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    return {"exports": []}


# ── Module settings ────────────────────────────────────────────────────────────
# Settings are stored as AuditLog snapshots. To scope them per-org we filter
# to rows whose actor belongs to the same organization, so Org A's currency
# choice never leaks into Org B's payroll dashboard.

_PAYROLL_SETTINGS_DEFAULTS: Dict[str, Any] = {
    "currency":               "",
    "country":                "",
    "default_frequency":      "monthly",
    "default_overtime_rule":  "1.5x_after_40",
    "default_rounding":       "nearest_5_min",
    "default_break_rule":     "deduct_unpaid",
    "default_holiday_rule":   "paid_at_1x",
    "default_policy_set":     "standard",
    "lock_after_export":      True,
    "require_two_approvers":  False,
}

def _org_id_of(user):
    return getattr(user, "organization_id", None)


def _org_currency(db: Session, user) -> str:
    """This organization's configured payroll currency, or "" if unset."""
    try:
        row = _latest_settings_row(db, organization_id=user.organization_id)
        if row and row.payload:
            return str(row.payload.get("currency") or "").upper()
    except Exception:
        pass
    return ""


_VALID_CURRENCIES = {
    "USD", "INR", "GBP", "EUR", "AED", "SGD", "AUD", "CAD",
    "JPY", "CNY", "ZAR", "NZD", "HKD", "CHF", "SEK",
}


def _latest_settings_row(db: Session, organization_id: Optional[int] = None) -> Optional[models.AuditLog]:
    """Return the most recent PayrollSettingsSet log row for this org."""
    q = db.query(models.AuditLog).filter(
        models.AuditLog.entity == "PayrollSettings",
        models.AuditLog.action == "PayrollSettingsSet",
    )
    if organization_id is not None:
        # Scope to actors who belong to the same org.
        org_user_ids = db.query(models.User.id).filter(
            models.User.organization_id == organization_id
        ).scalar_subquery()
        q = q.filter(models.AuditLog.actor_id.in_(org_user_ids))
    return q.order_by(models.AuditLog.created_at.desc()).first()


@router.get("/settings",
            dependencies=[Depends(require_capability("payroll.calculator"))])
def get_payroll_settings(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    base = dict(_PAYROLL_SETTINGS_DEFAULTS)
    row  = _latest_settings_row(db, organization_id=user.organization_id)
    if row and row.payload:
        base.update({k: v for k, v in row.payload.items() if k in _PAYROLL_SETTINGS_DEFAULTS})
    return {"settings": base}


@router.patch("/settings",
              dependencies=[Depends(require_writable_capability("payroll.calculator"))])
def patch_payroll_settings(
    body: Dict[str, Any],
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    base = dict(_PAYROLL_SETTINGS_DEFAULTS)
    row  = _latest_settings_row(db, organization_id=user.organization_id)
    if row and row.payload:
        base.update({k: v for k, v in row.payload.items() if k in _PAYROLL_SETTINGS_DEFAULTS})

    incoming: Dict[str, Any] = {}
    for k, v in (body or {}).items():
        if k not in _PAYROLL_SETTINGS_DEFAULTS:
            continue
        if k == "currency" and v not in ("", None) and v not in _VALID_CURRENCIES:
            raise HTTPException(400, detail=f"currency must be one of {sorted(_VALID_CURRENCIES)}")
        incoming[k] = v
    next_state = {**base, **incoming}

    log = models.AuditLog(
        actor_id=user.id,
        target_id=None,
        organization_id=user.organization_id,
        entity="PayrollSettings",
        action="PayrollSettingsSet",
        payload=next_state,
    )
    db.add(log)
    db.commit()
    return {"success": True, "settings": next_state}
