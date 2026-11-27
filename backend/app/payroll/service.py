"""
Temveris – Payroll Service
Handles payroll config CRUD and payroll run generation.
All collection-level functions accept organization_id so the router can
enforce tenant isolation without raw SQL in the endpoint handlers.
"""

from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime, timedelta, timezone
from typing import Optional
from database import models


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_config(db: Session, user_id: int) -> Optional[models.PayrollConfig]:
    return db.query(models.PayrollConfig).filter(
        models.PayrollConfig.user_id == user_id
    ).first()


def get_all_configs(db: Session, organization_id: Optional[int] = None):
    q = db.query(models.PayrollConfig).filter(models.PayrollConfig.is_active == True)
    if organization_id is not None:
        q = q.filter(models.PayrollConfig.organization_id == organization_id)
    return q.all()


def upsert_config(db: Session, user_id: int, data: dict, organization_id: Optional[int] = None):
    cfg = get_config(db, user_id)
    if cfg:
        for k, v in data.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        # Stamp org if it was never set (legacy row backfill edge-case).
        if cfg.organization_id is None and organization_id is not None:
            cfg.organization_id = organization_id
    else:
        cfg = models.PayrollConfig(
            user_id=user_id,
            organization_id=organization_id,
            **{k: v for k, v in data.items() if v is not None},
        )
        db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def delete_config(db: Session, user_id: int) -> bool:
    cfg = get_config(db, user_id)
    if cfg:
        db.delete(cfg)
        db.commit()
        return True
    return False


# ── Payroll math ───────────────────────────────────────────────────────────────

def compute_gross(cfg) -> float:
    return (cfg.basic_salary or 0) + (cfg.hra or 0) + (cfg.da or 0) + (cfg.ta or 0) + (cfg.bonus or 0)


def compute_deductions(cfg) -> float:
    return (cfg.pf or 0) + (cfg.tax or 0)


def compute_net(cfg) -> float:
    return compute_gross(cfg) - compute_deductions(cfg)


# ── LWP / worked-hours ─────────────────────────────────────────────────────────

def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Drop the tzinfo so a naive column value and an aware bound compare.

    Both sides represent UTC; only their tz-awareness differs.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def get_lwp_days(db: Session, user_id: int, start: datetime, end: datetime) -> int:
    """Count approved LWP (Unpaid Leave / auto-LWP) days overlapping [start, end].

    Whether the span consumes weekends and holidays is this organization's
    `payroll.lwp_counts_non_working_days` policy. The default ("true") counts
    every calendar day, matching the previous behaviour exactly; setting it
    false deducts only the days the employee was actually scheduled to work,
    which is what most contracts intend for a Friday-to-Monday absence.
    """
    reqs = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id == user_id,
        models.LeaveRequest.status.in_(["approved", "locked_for_payroll"]),
        models.LeaveRequest.from_date <= end,
        models.LeaveRequest.to_date   >= start,
    ).all()

    user = db.query(models.User).filter(models.User.id == user_id).first()
    count_all_days = True
    if user is not None:
        try:
            from app.payroll import policy as payroll_policy
            count_all_days = payroll_policy.counts_non_working_days(db, user)
        except Exception:
            count_all_days = True

    # This organization's effective leave catalogue, resolved once — the role
    # flags live on it (app/leave/identity.py).
    _pol = {}
    try:
        from app.leave.router import _get_effective_policies
        _pol = _get_effective_policies(
            db, getattr(user, "organization_id", None)) or {}
    except Exception:
        _pol = {}

    total = 0
    for r in reqs:
        # `is_lwp` is set by the leave module on AUTO-converted requests.
        # Everything else routes through leave.identity, which is the single
        # place the "which type is unpaid" question is asked — see R2 there.
        from app.leave import identity as leave_identity
        # Pass the ORGANIZATION's catalogue so a tenant whose unpaid bucket is
        # flagged (rather than named "Unpaid Leave") is deducted correctly —
        # and so the shipped name stops claiming the role once it is assigned.
        if not (getattr(r, "is_lwp", False)
                or leave_identity.is_unpaid(
                    r.leave_type, _pol.get(r.leave_type), _pol)):
            continue
        # LeaveRequest.from_date/to_date are Column(DateTime) — NAIVE, on
        # Postgres as well as SQLite. `start`/`end` arrive from
        # create_payroll_run, which builds them with
        # datetime.fromisoformat(value.replace("Z", "+00:00")) — so a client
        # that sends "2026-08-01T00:00:00Z" produces an AWARE bound and this
        # comparison raised TypeError, failing the whole payroll run. Which
        # format the client happens to use is not a safe thing for payroll to
        # depend on, so normalise both sides.
        lo = max(_naive(r.from_date), _naive(start))
        hi = min(_naive(r.to_date),   _naive(end))
        if hi < lo:
            continue
        if count_all_days or user is None:
            total += (hi - lo).days + 1
        else:
            from app.payroll import policy as payroll_policy
            total += payroll_policy.working_days_between(
                db, user, lo.date(), hi.date())
    return total


def compute_lwp_deduction(cfg, lwp_days: int, db: Session = None,
                          user=None, period_start=None, period_end=None) -> float:
    """Pro-rata daily salary x LWP days.

    The divisor is this organization's `payroll.lwp_divisor_mode` /
    `payroll.lwp_divisor_days` — commonly 30, 26 or 22, or the employee's
    actual working days in the period. It used to be a hard-coded 30.0, which
    silently applied one company's convention to every tenant's payslips.

    `db`/`user` are optional so the existing two-argument call signature keeps
    working; without them the documented system default (30) applies.
    """
    if lwp_days <= 0 or not cfg:
        return 0.0
    gross = compute_gross(cfg)
    if gross <= 0:
        return 0.0
    divisor = 30.0
    if db is not None and user is not None:
        try:
            from app.payroll import policy as payroll_policy
            divisor = payroll_policy.divisor_days(db, user, period_start, period_end)
        except Exception:
            divisor = 30.0
    return round((gross / divisor) * lwp_days, 2)


def get_worked_hours(db: Session, user_id: int, start: datetime, end: datetime) -> float:
    """Hours this employee actually worked in [start, end].

    Uses the same session arithmetic as the attendance report and the risk
    check (``app/_worked.py``) — this used to sum raw ``total_seconds``, so a
    payslip could disagree with the report the employee was shown.

    Admin-credited time adjustments are included: they exist precisely because
    the employee worked hours the tracker lost (power cut, crash), and paying
    them for the tracker's outage is the entire point of the feature.
    """
    from app._worked import session_worked_seconds
    from app._time_adjustments import adjustment_seconds_by_user

    sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user_id,
            models.ClockSession.clock_in_time  >= start,
            models.ClockSession.clock_in_time  <= end,
            models.ClockSession.clock_out_time != None,
        )
    ).all()
    total_secs = sum(session_worked_seconds(s) for s in sessions)

    # adjustment_seconds_by_user takes dates and is half-open on `end`.
    try:
        adj = adjustment_seconds_by_user(
            db, [user_id], start.date(), end.date() + timedelta(days=1),
        ).get(user_id, 0)
    except Exception:
        adj = 0
    return round((total_secs + adj) / 3600, 2)


# ── Payroll Run creation ───────────────────────────────────────────────────────

def create_payroll_run(
    db:             Session,
    period_start:   datetime,
    period_end:     datetime,
    admin_id:       int,
    notes:          Optional[str] = None,
    organization_id: Optional[int] = None,
) -> models.PayrollRun:
    # Only include configs belonging to this org.
    configs = get_all_configs(db, organization_id=organization_id)

    # The run is stamped with THIS organization's configured payroll currency.
    # It was hard-coded "USD" while the employee-facing page fell back to
    # "INR" — the same payslip was labelled two different currencies.
    _currency = ""
    try:
        from app.payroll.router import _latest_settings_row
        _row = _latest_settings_row(db, organization_id=organization_id)
        if _row and _row.payload:
            _currency = str(_row.payload.get("currency") or "").upper()
    except Exception:
        _currency = ""

    run = models.PayrollRun(
        period_start=period_start,
        period_end=period_end,
        status="draft",
        currency=_currency or None,
        notes=notes,
        created_by=admin_id,
        organization_id=organization_id,
    )
    db.add(run)
    db.flush()

    total = 0.0
    for cfg in configs:
        hours = get_worked_hours(db, cfg.user_id, period_start, period_end)
        gross = compute_gross(cfg)
        deductions = compute_deductions(cfg)

        if cfg.pay_type == "hourly":
            gross = round(cfg.hourly_rate * hours, 2)

        lwp_days = get_lwp_days(db, cfg.user_id, period_start, period_end)
        _pay_user = db.query(models.User).filter(models.User.id == cfg.user_id).first()
        lwp_deduction = (
            compute_lwp_deduction(cfg, lwp_days, db=db, user=_pay_user,
                                  period_start=period_start.date() if hasattr(period_start, "date") else period_start,
                                  period_end=period_end.date() if hasattr(period_end, "date") else period_end)
            if cfg.pay_type != "hourly" else 0.0
        )
        net = round(gross - deductions - lwp_deduction, 2)

        item = models.PayrollItem(
            payroll_run_id=run.id,
            user_id=cfg.user_id,
            organization_id=organization_id,
            worked_hours=hours,
            basic_salary=cfg.basic_salary or 0,
            hra=cfg.hra or 0,
            da=cfg.da or 0,
            ta=cfg.ta or 0,
            bonus=cfg.bonus or 0,
            gross_pay=gross,
            pf_deduction=cfg.pf or 0,
            tax_deduction=cfg.tax or 0,
            other_deduction=lwp_deduction,
            net_pay=max(0, net),
            status="pending",
        )
        db.add(item)
        total += max(0, net)

    run.total_amount = round(total, 2)
    db.commit()
    db.refresh(run)
    return run


# ── Run queries ────────────────────────────────────────────────────────────────

def get_payroll_runs(db: Session, limit: int = 20, organization_id: Optional[int] = None):
    q = db.query(models.PayrollRun).order_by(models.PayrollRun.created_at.desc())
    if organization_id is not None:
        q = q.filter(models.PayrollRun.organization_id == organization_id)
    return q.limit(limit).all()


def get_payroll_run(db: Session, run_id: int) -> Optional[models.PayrollRun]:
    return db.query(models.PayrollRun).filter(models.PayrollRun.id == run_id).first()


def get_run_items(db: Session, run_id: int):
    return db.query(models.PayrollItem).filter(
        models.PayrollItem.payroll_run_id == run_id
    ).all()


# ── Run state transitions ──────────────────────────────────────────────────────

def mark_run_processed(db: Session, run_id: int) -> Optional[models.PayrollRun]:
    run = get_payroll_run(db, run_id)
    if run:
        run.status = "processed"
        db.commit()
        db.refresh(run)
    return run


def mark_run_paid(db: Session, run_id: int) -> Optional[models.PayrollRun]:
    run = get_payroll_run(db, run_id)
    if run:
        run.status = "paid"
        now = datetime.now(timezone.utc)
        for item in run.items:
            item.status = "paid"
            item.paid_at = now
        db.commit()
        db.refresh(run)
    return run


# ── Summary ────────────────────────────────────────────────────────────────────

def get_payroll_summary(db: Session, organization_id: Optional[int] = None) -> dict:
    """Summary stats for the admin dashboard, scoped to the caller's org."""
    configs = get_all_configs(db, organization_id=organization_id)
    total_monthly  = sum(compute_net(c) for c in configs)
    total_employees = len(configs)

    runs = get_payroll_runs(db, limit=5, organization_id=organization_id)
    last_run = runs[0] if runs else None

    return {
        "total_employees":        total_employees,
        "total_monthly_payroll":  round(total_monthly, 2),
        "avg_salary":             round(total_monthly / total_employees, 2) if total_employees else 0,
        "last_run": {
            "id":           last_run.id,
            "period_start": last_run.period_start.isoformat() if last_run.period_start else None,
            "period_end":   last_run.period_end.isoformat()   if last_run.period_end   else None,
            "status":       last_run.status,
            "total_amount": last_run.total_amount,
        } if last_run else None,
        "pending_runs": sum(1 for r in runs if r.status == "draft"),
    }
