"""Read-only diagnosis of who the absence sweep would alert, and WHY.

Answers the two questions a wrong "Absent Alert" email always raises:

  1. Which employees would this sweep mark Absent right now, in their own
     organization's local time?
  2. For each one, WHERE did their shift start and their absence cutoff come
     from — an employee-scope setting, a team setting, an assigned shift, the
     legacy ``users.default_clock_in_time`` column, or nothing at all?

Question 2 is the one that matters. An employee whose shift start resolves to
``None`` falls back to the ``schedule.absent_after_hour`` rule (default 12:00)
no matter what time an admin thinks they typed into the dashboard, because the
"Clock Time Override" modal only writes a recurring schedule when its
"Make permanent" toggle is on — otherwise it writes a one-off ClockSession,
which the absence cutoff never reads. This script makes that visible instead of
leaving it to be inferred from email timestamps.

TOUCHES NOTHING. Opens one session, runs SELECTs, prints, closes. There is no
commit anywhere in this file, so it is safe to point at production.

Usage
-----
    python -m scripts.diagnose_absent_alerts                  # every org
    python -m scripts.diagnose_absent_alerts --org 1          # one org
    python -m scripts.diagnose_absent_alerts --org 1 --name mukul
    python -m scripts.diagnose_absent_alerts --all-roles      # include admin/hr
    python -m scripts.diagnose_absent_alerts --at 13:05       # "what if it were 13:05"

``--at HH:MM`` re-evaluates the classification at a hypothetical local wall
clock without waiting for that time to come round — the way to check "would
Mukul still be alerted at 1 PM?" before deploying a change.

Run it from ``backend/`` with the same environment the API uses, so it reads
the same DATABASE_URL.
"""
from __future__ import annotations

import argparse
import sys
from datetime import time as _time

from sqlalchemy.orm import Session

from database.database import SessionLocal
from database import models

from app import _tz
from app._system_admin import exclude_system_admin, excluded_names
from app.schedule_policy import keys as K
from app.schedule_policy.classify import ArrivalStatus, absence_cutoff, classify_arrival
from app.schedule_policy.resolver import resolve_work_schedules


# Mirrors attendance_alerts._SKIP_ROLES so the report reflects what the live
# sweep actually does. Imported rather than copied where possible.
def _skip_roles() -> set:
    try:
        from app.attendance_alerts import _SKIP_ROLES
        return set(_SKIP_ROLES)
    except Exception:
        return {"admin", "hr"}


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, _time):
        return v.strftime("%H:%M")
    return str(v)


def _org_rows(db: Session, only_org):
    q = db.query(models.Organization)
    if only_org is not None:
        q = q.filter(models.Organization.id == only_org)
    return q.order_by(models.Organization.id).all()


def _clocked_in_ids(db: Session, org_id, day_start_utc, day_end_utc) -> set:
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


def _on_leave_ids(db: Session, user_ids, on_date) -> set:
    if not user_ids:
        return set()
    from datetime import datetime, timezone
    day_start = datetime.combine(on_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = datetime.combine(on_date, datetime.max.time(), tzinfo=timezone.utc)
    rows = (
        db.query(models.LeaveRequest.user_id)
        .filter(
            models.LeaveRequest.user_id.in_(list(user_ids)),
            models.LeaveRequest.status == "approved",
            models.LeaveRequest.from_date <= day_end,
            models.LeaveRequest.to_date >= day_start,
        )
        .all()
    )
    return {r[0] for r in rows}


def _alerted_today(db: Session, user_ids, action, day_start_utc) -> set:
    if not user_ids:
        return set()
    rows = (
        db.query(models.AuditLog.target_id)
        .filter(
            models.AuditLog.target_id.in_(list(user_ids)),
            models.AuditLog.entity == "User",
            models.AuditLog.action == action,
            models.AuditLog.created_at >= day_start_utc,
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _employee_scope_rows(db: Session, org_id, user_ids) -> dict:
    """Employee-scope app_settings rows for the schedule keys, per user."""
    if not user_ids:
        return {}
    rows = (
        db.query(models.AppSettings)
        .filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.user_id.in_(list(user_ids)),
            models.AppSettings.key.like("schedule.%"),
        )
        .all()
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r.user_id, {})[r.key] = r.value
    return out


def run(only_org=None, name_filter=None, all_roles=False, at_hhmm=None) -> int:
    db = SessionLocal()
    flagged_total = 0
    try:
        skip_roles = _skip_roles()
        print(f"absence-sweep diagnosis  (skip_roles={sorted(skip_roles)})")
        if at_hhmm:
            print(f"HYPOTHETICAL local time: {at_hhmm}  (real clock ignored)")
        print("=" * 100)

        for org in _org_rows(db, only_org):
            now_local = _tz.local_now(db, org.id)
            if at_hhmm:
                hh, mm = (int(x) for x in at_hhmm.split(":"))
                now_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now_local.date()
            tz_name = _tz.org_timezone_name(db, org.id)
            day_start_utc, day_end_utc = _tz.local_day_bounds(db, org.id, today)

            users = [
                u for u in exclude_system_admin(
                    db.query(models.User)
                    .filter(models.User.organization_id == org.id)
                    .filter(models.User.is_active == True)  # noqa: E712
                    .all(),
                    db,
                )
                if all_roles or (u.role or "").lower() not in skip_roles
            ]
            if name_filter:
                nf = name_filter.lower()
                users = [u for u in users if nf in (u.name or "").lower()
                         or nf in (u.email or "").lower()]
            if not users:
                continue

            user_ids = [u.id for u in users]
            clocked_in = _clocked_in_ids(db, org.id, day_start_utc, day_end_utc)
            on_leave = _on_leave_ids(db, user_ids, today)
            already_absent = _alerted_today(db, user_ids, "EmployeeMarkedAbsent", day_start_utc)
            already_late = _alerted_today(db, user_ids, "EmployeeMarkedLate", day_start_utc)
            emp_rows = _employee_scope_rows(db, org.id, user_ids)
            scheds = resolve_work_schedules(db, users, today)

            print(f"\nORG {org.id} — {org.name}")
            print(f"  timezone {tz_name}   local now {now_local:%Y-%m-%d %H:%M}   "
                  f"non-working names {sorted(excluded_names(db, org.id))}")
            print(f"  {'id':>5}  {'name':<22} {'role':<14} {'start':<6} {'src':<13} "
                  f"{'cutoff':<7} {'aft_hr':>6} {'aft_min':>7} {'verdict':<13} note")
            print("  " + "-" * 118)

            for u in sorted(users, key=lambda x: x.id):
                sched = scheds.get(u.id)
                if sched is None:
                    print(f"  {u.id:>5}  {(u.name or '')[:22]:<22} {(u.role or '')[:14]:<14} "
                          f"{'?':<6} {'RESOLVE FAILED':<13}")
                    continue

                src = (sched.sources or {}).get("start_local", "?")
                note = []
                if u.id in clocked_in:
                    note.append("clocked-in")
                if u.id in on_leave:
                    note.append("on-leave")
                if u.id in already_absent:
                    note.append("absent-mail-sent")
                if u.id in already_late:
                    note.append("late-mail-sent")
                if not sched.is_working_day:
                    note.append("holiday" if sched.is_holiday else "non-working-day")
                if sched.start_local is None:
                    note.append("NO SHIFT START -> falls back to absent_after_hour")
                if u.id in emp_rows:
                    note.append("emp-scope:" + ",".join(
                        k.replace("schedule.", "") for k in sorted(emp_rows[u.id])))
                if u.default_clock_in_time:
                    note.append(f"users.default_clock_in_time={u.default_clock_in_time}")

                # The same call the live sweep makes.
                verdict = classify_arrival(None, sched, now_local)
                start_dt = None
                if sched.start_local is not None:
                    start_dt = now_local.replace(hour=sched.start_local.hour,
                                                 minute=sched.start_local.minute,
                                                 second=0, microsecond=0)
                cutoff = absence_cutoff(sched, start_dt, now_local)

                would_alert = (
                    u.id not in clocked_in
                    and u.id not in on_leave
                    and sched.is_working_day
                    and (
                        (verdict.status == ArrivalStatus.ABSENT and u.id not in already_absent)
                        or (verdict.status == ArrivalStatus.LATE_PENDING and u.id not in already_late)
                    )
                )
                if would_alert:
                    flagged_total += 1
                    note.insert(0, ">>> WOULD EMAIL NOW <<<")

                print(f"  {u.id:>5}  {(u.name or '')[:22]:<22} {(u.role or '')[:14]:<14} "
                      f"{_fmt(sched.start_local):<6} {str(src)[:13]:<13} "
                      f"{cutoff:%H:%M}   {sched.absent_after_hour:>6} "
                      f"{_fmt(sched.absent_after_minutes):>7} "
                      f"{verdict.status.value:<13} {'; '.join(note)}")

        print("\n" + "=" * 100)
        print(f"{flagged_total} employee(s) would receive an alert email on the next sweep.")
        print("Read the 'src' column: 'system' with start '—' means NO shift start is stored, so the\n"
              "noon absent_after_hour rule applies regardless of what was typed into the dashboard.")
        return 0
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--org", type=int, default=None, help="limit to one organization id")
    p.add_argument("--name", default=None, help="substring match on name or email")
    p.add_argument("--all-roles", action="store_true",
                   help="include roles the sweep currently skips (admin/hr)")
    p.add_argument("--at", dest="at_hhmm", default=None, metavar="HH:MM",
                   help="evaluate at a hypothetical local wall clock instead of now")
    a = p.parse_args()
    if a.at_hhmm:
        try:
            hh, mm = (int(x) for x in a.at_hhmm.split(":"))
            assert 0 <= hh <= 23 and 0 <= mm <= 59
        except Exception:
            print("--at must be HH:MM (24-hour)", file=sys.stderr)
            return 2
    return run(a.org, a.name, a.all_roles, a.at_hhmm)


if __name__ == "__main__":
    raise SystemExit(main())
