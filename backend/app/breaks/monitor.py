"""
Temveris — Break Overrun Monitor (Part 2 of the Break Governance spec)
=======================================================================
Continuous monitoring of every active break. On each tick it finds breaks
whose elapsed time has crossed the allowance (Short 15m / Lunch 60m) and:

  * marks the break OVERDUE and flags the overrun (timer keeps counting up —
    breaks are NEVER auto-ended);
  * appends immutable provenance — BREAK_ALLOWANCE_REACHED + BREAK_OVERRUN on
    the first crossing, then BREAK_OVERRUN_NOTIFICATION_SENT for each alert;
  * reminds ONLY the worker at their own email (HR/admin escalation disabled by
    request) — the first alert at the threshold, then a reminder every 30
    minutes while the break stays active (spam guard via ``last_alert_at`` /
    ``alert_count``);
  * broadcasts a websocket ``break_overtime`` so the admin dashboard + worker
    UI light up RED without waiting for the next poll.

``process_break_overruns`` is a pure ``run_once`` over a passed-in session so
it can be driven by the FastAPI lifespan scheduler or a unit test with no
shared state. ``run_forever`` is the lifespan loop, matching the contract of
the commercial / offboarding schedulers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database import models
from app.breaks import governance as gov

_log = logging.getLogger("zoiko.breaks.monitor")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt(dt: Optional[datetime]) -> str:
    dt = _as_utc(dt)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _broadcast_overtime(user, brk, elapsed: int, allowed_min: int, grace_min: int) -> None:
    """Push a break-overrun event to THIS EMPLOYEE'S ORGANIZATION ONLY.

    This used the global `broadcast_sync`, so every tenant's dashboard received
    another tenant's employee name and break behaviour — and the payload
    carried no organization_id, so a client could not have filtered it even if
    it tried. The HR email in this same module is correctly scoped by
    `recipients_for_user`; only the live push leaked.
    """
    org_id = getattr(user, "organization_id", None)
    if org_id is None:
        return
    try:
        from websocket_manager import manager
        import json
        manager.broadcast_to_org_sync(org_id, json.dumps({
            "type":              "break_overtime",
            "organization_id":   org_id,
            "user_id":           user.id,
            "user_name":         user.name,
            "break_id":          brk.id,
            "break_type":        brk.break_type or "break",
            "elapsed_seconds":   elapsed,
            "threshold_minutes": allowed_min,
            "overrun_seconds":   max(0, elapsed - allowed_min * 60),
            # Alias the admin dashboard reads (break_overtime handler).
            "overtime_seconds":  max(0, elapsed - allowed_min * 60),
            "timestamp":         _now().isoformat(),
            "notification_delay_minutes": grace_min,
        }))
    except Exception:
        pass


def _first_notification_due(db: Session, elapsed: int, allowed_min: int, break_type: str,
                            org_id=None) -> bool:
    """First HR alert fires only after the break EXCEEDS allowance + grace — the
    per-break-type grace window (Short 15+5=20m, Lunch 60+5=65m by default)."""
    return elapsed >= (allowed_min + gov.grace_minutes(db, break_type, org_id)) * 60


def _send_hr_alert(db: Session, user, brk, *, allowed_min: int, elapsed: int,
                   is_reminder: bool) -> bool:
    """Best-effort HR escalation email. Never raises — a mail failure must not
    stall the monitor or block the provenance write."""
    try:
        from app.email_service import send_break_overrun_alert_to_hr
        from app.notifications.recipients import recipients_for_user
        exceeded_min = max(0, elapsed - allowed_min * 60) / 60
        return bool(send_break_overrun_alert_to_hr(
            recipients=recipients_for_user(db, user, purpose="break.overrun"),
            worker_name=user.name,
            worker_id=user.id,
            worker_type=brk.worker_type or gov.normalize_worker_type(user),
            break_type=brk.break_type or "break",
            allowed_minutes=allowed_min,
            current_minutes=round(elapsed / 60, 1),
            exceeded_minutes=round(exceeded_min, 1),
            department=user.department or "",
            manager=user.team or "",
            start_time=_fmt(brk.break_start),
            current_time=_fmt(_now()),
            tenant=gov.tenant_id(db, getattr(user, "organization_id", None)),
            policy_version=gov.policy_version_id(db, getattr(user, "organization_id", None)),
            is_reminder=is_reminder,
        ))
    except Exception as e:  # pragma: no cover - defensive
        _log.warning("break overrun HR email failed: %r", e)
        return False


def _send_employee_alert(db: Session, user, brk, *, allowed_min: int, elapsed: int,
                         is_reminder: bool) -> bool:
    """Best-effort reminder to the WORKER that their own break is over the limit
    so they can resume work. Sent at the same triggers as the HR escalation.
    Never raises — a mail failure must not stall the monitor or the provenance
    write. No-ops (returns False) when the worker has no email on file."""
    email = getattr(user, "email", None)
    if not email:
        return False
    try:
        from app.email_service import send_break_overrun_alert_to_employee
        exceeded_min = max(0, elapsed - allowed_min * 60) / 60
        return bool(send_break_overrun_alert_to_employee(
            employee_email=email,
            employee_name=user.name,
            break_type=brk.break_type or "break",
            allowed_minutes=allowed_min,
            current_minutes=round(elapsed / 60, 1),
            exceeded_minutes=round(exceeded_min, 1),
            start_time=_fmt(brk.break_start),
            current_time=_fmt(_now()),
            is_reminder=is_reminder,
        ))
    except Exception as e:  # pragma: no cover - defensive
        _log.warning("break overrun employee email failed: %r", e)
        return False


def _alert_recipients(user) -> list:
    """Recipients recorded in the provenance for an overrun alert. Break-overrun
    alerts go ONLY to the worker (HR/admin disabled by request)."""
    email = getattr(user, "email", None)
    return [email] if email else []


def _claim_first_alert(db: Session, brk_id, now: datetime) -> bool:
    """Atomic compare-and-swap for the FIRST overrun alert.

    A single conditional UPDATE flips ``overrun_notification_sent`` False→True.
    Postgres row-locking serializes concurrent callers, so with N gunicorn
    workers / Cloud Run instances only ONE sees rowcount==1 and therefore sends
    the email — this closes the duplicate-notification race. Returns True if
    THIS caller won the claim. In a single process (unit tests) it simply wins
    on the first call, so behaviour is unchanged.
    """
    rows = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.id == brk_id,
            or_(models.BreakSession.overrun_notification_sent.is_(None),
                models.BreakSession.overrun_notification_sent == False),  # noqa: E712
        )
        .update(
            {
                "overrun_notification_sent": True,
                "overrun_notified_at": now,
                "last_alert_at": now,
                "overtime_notified": True,
                "alert_count": func.coalesce(models.BreakSession.alert_count, 0) + 1,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return rows == 1


def _claim_reminder(db: Session, brk_id, now: datetime, *, is_first_alert: bool) -> bool:
    """Atomic compare-and-swap for a reminder (or a first alert reached via the
    already-overdue path). Advances ``last_alert_at`` to ``now`` only when the
    reminder is actually due (NULL or older than the reminder window) AND only
    for one caller — the same cross-worker/instance guard as the first alert.
    Returns True if THIS caller won the slot. The WHERE clause mirrors the
    previous ``due`` check exactly, so single-process behaviour is unchanged.
    """
    cutoff = now - timedelta(minutes=gov.OVERRUN_REMINDER_MINUTES)
    vals = {
        "last_alert_at": now,
        "overrun_notification_sent": True,
        "overtime_notified": True,
        "alert_count": func.coalesce(models.BreakSession.alert_count, 0) + 1,
    }
    if is_first_alert:
        vals["overrun_notified_at"] = now
    rows = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.id == brk_id,
            or_(models.BreakSession.last_alert_at.is_(None),
                models.BreakSession.last_alert_at <= cutoff),
        )
        .update(vals, synchronize_session=False)
    )
    db.commit()
    return rows == 1


def _process_one(db: Session, brk: models.BreakSession, user: models.User,
                 *, now: datetime) -> bool:
    """Handle a single active, over-limit break. Returns True if an alert was
    sent this tick. Commits its own changes; isolates failure per break.

    The email send is gated by an atomic DB claim (_claim_first_alert /
    _claim_reminder) so that when the monitor runs in more than one process
    (gunicorn --workers 2, or multiple Cloud Run instances) exactly ONE process
    sends each alert instead of one-per-worker."""
    btype = brk.break_type or "break"
    _org = getattr(user, "organization_id", None)
    allowed_min = brk.allowed_minutes or gov.allowance_minutes(db, btype, _org)
    grace_min = gov.grace_minutes(db, btype, _org)
    start = _as_utc(brk.break_start)
    elapsed = max(0, int((now - start).total_seconds()))

    # Not yet over the allowance — nothing to do.
    if elapsed < allowed_min * 60:
        return False

    alerted = False

    if not brk.overrun_flag:
        # ── First crossing: allowance reached → overrun provenance ───────────
        brk.status = "overdue"
        brk.overrun_flag = True
        if brk.allowance_reached_at is None:
            brk.allowance_reached_at = start + timedelta(minutes=allowed_min)

        gov.record_break_event(
            db, user=user, event_type="BREAK_ALLOWANCE_REACHED",
            session_id=brk.session_id, break_id=brk.id,
            worker_type=brk.worker_type, break_type=btype, allowed_minutes=allowed_min,
            extra={"elapsedSeconds": elapsed, "allowanceReachedAt": _as_utc(brk.allowance_reached_at).isoformat()},
            idempotency_key=f"break:{brk.id}:allowance_reached",
        )
        gov.record_break_event(
            db, user=user, event_type="BREAK_OVERRUN",
            session_id=brk.session_id, break_id=brk.id,
            worker_type=brk.worker_type, break_type=btype, allowed_minutes=allowed_min,
            extra={"elapsedSeconds": elapsed, "overrunSeconds": elapsed - allowed_min * 60},
            idempotency_key=f"break:{brk.id}:overrun",
        )
        db.commit()

        if not _first_notification_due(db, elapsed, allowed_min, btype, _org):
            return False

        should_broadcast = not getattr(brk, "overtime_notified", False)
        # Atomic claim — only ONE worker/instance may send the first alert.
        if not _claim_first_alert(db, brk.id, now):
            return False
        db.refresh(brk)
        # HR/admin break-overrun email disabled by request — notify ONLY the
        # employee. (_send_hr_alert kept for easy re-enable, but not called.)
        sent = False
        emp_sent = _send_employee_alert(db, user, brk, allowed_min=allowed_min, elapsed=elapsed, is_reminder=False)
        gov.record_break_event(
            db, user=user, event_type="BREAK_OVERRUN_NOTIFICATION_SENT",
            session_id=brk.session_id, break_id=brk.id,
            worker_type=brk.worker_type, break_type=btype, allowed_minutes=allowed_min,
            extra={"alertCount": brk.alert_count, "isReminder": False, "emailDispatched": bool(sent),
                   "employeeEmailDispatched": bool(emp_sent), "recipients": _alert_recipients(user)},
            idempotency_key=f"break:{brk.id}:notify:{brk.alert_count}",
        )
        db.commit()
        if should_broadcast:
            _broadcast_overtime(user, brk, elapsed, allowed_min, grace_min)
        alerted = True

    else:
        # ── Already overdue: reminder every OVERRUN_REMINDER_MINUTES ─────────
        if not _first_notification_due(db, elapsed, allowed_min, btype, _org):
            return False

        is_first_alert = (brk.alert_count or 0) <= 0 or not brk.overrun_notification_sent
        should_broadcast = (not is_first_alert) or not getattr(brk, "overtime_notified", False)
        # Atomic claim — advance the reminder slot for exactly one worker, and
        # only when actually due (WHERE mirrors the previous `due` check).
        if not _claim_reminder(db, brk.id, now, is_first_alert=is_first_alert):
            return False
        db.refresh(brk)
        # HR/admin break-overrun email disabled by request — employee only.
        sent = False
        emp_sent = _send_employee_alert(db, user, brk, allowed_min=allowed_min, elapsed=elapsed, is_reminder=not is_first_alert)
        gov.record_break_event(
            db, user=user, event_type="BREAK_OVERRUN_NOTIFICATION_SENT",
            session_id=brk.session_id, break_id=brk.id,
            worker_type=brk.worker_type, break_type=btype, allowed_minutes=allowed_min,
            extra={"alertCount": brk.alert_count, "isReminder": not is_first_alert, "emailDispatched": bool(sent),
                   "employeeEmailDispatched": bool(emp_sent), "recipients": _alert_recipients(user)},
            idempotency_key=f"break:{brk.id}:notify:{brk.alert_count}",
        )
        db.commit()
        if should_broadcast:
            _broadcast_overtime(user, brk, elapsed, allowed_min, grace_min)
        alerted = True

    return alerted


def process_break_overruns(db: Session, *, now: Optional[datetime] = None) -> dict:
    """One monitoring pass. Returns a small summary for logs / tests."""
    now = _as_utc(now) or _now()
    active = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.break_end == None)  # noqa: E711
        .all()
    )
    summary = {"active": len(active), "overdue": 0, "alerts": 0}
    if not active:
        return summary

    users = {
        u.id: u
        for u in db.query(models.User).filter(models.User.id.in_(list({b.user_id for b in active}))).all()
    }

    for brk in active:
        user = users.get(brk.user_id)
        if user is None:
            continue
        try:
            allowed_min = brk.allowed_minutes or gov.allowance_minutes(
                db, brk.break_type or "break", getattr(user, "organization_id", None))
            elapsed = max(0, int((now - _as_utc(brk.break_start)).total_seconds()))
            if elapsed >= allowed_min * 60:
                summary["overdue"] += 1
            if _process_one(db, brk, user, now=now):
                summary["alerts"] += 1
        except Exception as e:  # pragma: no cover - one bad row can't stall the tick
            _log.warning("break overrun monitor: break id=%s failed: %r", getattr(brk, "id", "?"), e)
            try:
                db.rollback()
            except Exception:
                pass
    return summary


def run_once_sync() -> dict:
    """Run a single monitoring pass over a fresh DB session."""
    db = SessionLocal()
    try:
        return process_break_overruns(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = 60) -> None:
    """Lifespan loop — runs forever until cancelled at shutdown. Each tick is
    isolated; a failed pass is logged and retried next interval."""
    _log.info("break overrun monitor started (interval=%ss)", interval_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except asyncio.CancelledError:
            _log.info("break overrun monitor cancelled, exiting")
            raise
        except Exception as e:  # pragma: no cover - never let a bad tick kill the loop
            _log.warning("break overrun monitor tick failed (will retry): %r", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            _log.info("break overrun monitor cancelled, exiting")
            raise
