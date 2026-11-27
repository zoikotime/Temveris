"""
One-shot: move every currently-Absent non-admin employee into "On Leave"
for TODAY (IST). Working / On Break / Idle employees are left untouched.

"Currently Absent" = active, non-admin user who has NOT clocked in today
and has NO approved leave covering today. These are the only users affected.

The fix inserts an approved LeaveRequest row for today so the /team endpoint
buckets them as On Leave. Future days are unaffected — new leaves still
require HR approval as per the standard workflow.

Usage (from backend/ dir):
    python put_all_on_leave_today.py            # dry-run (prints who would be moved)
    python put_all_on_leave_today.py --commit   # actually writes the rows

Re-runnable: skips users already on leave today.
"""
import os
import sys
from datetime import datetime, timezone, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import and_, create_engine
from sqlalchemy.orm import sessionmaker
from database import models

# PgBouncer on port 6543 rejects the statement_timeout option the shared engine
# uses, so build a minimal engine here just for this one-shot script.
_DB_URL = os.getenv("DATABASE_URL")
if not _DB_URL:
    print("[ERROR] DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

_engine = create_engine(_DB_URL, pool_pre_ping=True, pool_size=1, max_overflow=0)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


EXCLUDE_NAMES = {"admin"}  # only the primary "Admin" account stays Absent


def main(commit: bool):
    db = SessionLocal()
    try:
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).date()
        from_dt   = datetime.combine(today_ist, datetime.min.time(), tzinfo=timezone.utc)
        to_dt     = from_dt  # same-day leave
        day_end   = datetime.combine(today_ist, datetime.max.time(), tzinfo=timezone.utc)

        # Clock sessions where clock_in_time is anywhere today IST — we convert
        # the IST day window to naive UTC for the query since DB stores UTC naive.
        day_start_utc = datetime.combine(today_ist, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)
        day_end_utc   = datetime.combine(today_ist, datetime.max.time(), tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)

        clocked_in_today = {
            r.user_id for r in
            db.query(models.ClockSession.user_id)
              .filter(and_(
                  models.ClockSession.clock_in_time >= day_start_utc,
                  models.ClockSession.clock_in_time <= day_end_utc,
              ))
              .distinct()
              .all()
        }

        approved_leave_today = {
            r.user_id for r in
            db.query(models.LeaveRequest.user_id)
              .filter(and_(
                  models.LeaveRequest.status == "approved",
                  models.LeaveRequest.from_date <= day_end,
                  models.LeaveRequest.to_date   >= from_dt,
              ))
              .distinct()
              .all()
        }

        users = (
            db.query(models.User)
              .filter(models.User.is_active == True)  # noqa: E712
              .all()
        )

        eligible = []
        skipped_admin, skipped_working, skipped_on_leave = 0, 0, 0
        for u in users:
            if (u.name or "").strip().lower() in EXCLUDE_NAMES:
                skipped_admin += 1
                continue
            if u.id in clocked_in_today:
                skipped_working += 1
                continue
            if u.id in approved_leave_today:
                skipped_on_leave += 1
                continue
            eligible.append(u)

        print(f"\n==== Temveris - On-Leave Backfill ({'LIVE' if commit else 'DRY-RUN'}) ====")
        print(f"Date (IST):                  {today_ist.isoformat()}")
        print(f"Users scanned:               {len(users)}")
        print(f"'Admin' account skipped:     {skipped_admin}")
        print(f"Already clocked in today:    {skipped_working}")
        print(f"Already on approved leave:   {skipped_on_leave}")
        print(f"Will be moved to On Leave:   {len(eligible)}")
        if eligible:
            print("\nAffected users:")
            for u in eligible:
                print(f"  - #{u.id:<4} {u.name:<30} <{u.email}>  role={u.role}")
        print("==================================================================")

        if not commit:
            print("\nDry-run only — no rows inserted. Re-run with --commit to apply.\n")
            return 0

        for u in eligible:
            db.add(models.LeaveRequest(
                user_id         = u.id,
                leave_type      = "Company Leave",
                from_date       = from_dt,
                to_date         = to_dt,
                description     = "One-time backfill: placed on leave for today per admin request.",
                reason          = "One-time backfill: placed on leave for today per admin request.",
                status          = "approved",
                reviewed_by_id  = None,
                reviewed_at     = datetime.now(timezone.utc),
                review_comment  = "Auto-approved via put_all_on_leave_today.py backfill.",
                is_lwp          = False,
                is_awol         = False,
                hr_flag         = False,
                doc_status      = "not_required",
                leave_days      = 1,
            ))
        db.commit()
        print(f"\n[OK] Committed {len(eligible)} new approved-leave row(s) for {today_ist.isoformat()}.\n")
        return 0
    except Exception as e:
        db.rollback()
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(commit=("--commit" in sys.argv)))
