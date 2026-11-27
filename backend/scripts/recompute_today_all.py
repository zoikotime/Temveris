"""
recompute_today_all.py — force a fresh Active Time band recompute for EVERY user
who produced a tracking window TODAY (IST), refreshing STALE ActivityDailyScore
rows (unlike recompute_missing_daily.py, which only fills rows that don't exist).

Run this ONCE right after deploying an Active Time scoring change so the
dashboard immediately shows the new band values instead of stale numbers lifted
to the floor. Idempotent (recompute_daily upserts) and safe to re-run.

READ/WRITE to activity_daily_scores + activity_minute_scores only; does NOT
touch activities/windows, the API, the tracker, or the frontend.

Usage:  python backend/scripts/recompute_today_all.py
"""
from __future__ import annotations
import os, sys, logging
from datetime import timedelta, timezone

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_ROOT, ".env"))

from sqlalchemy import text
from database.database import SessionLocal
from app.active_time import service as at

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("recompute-today")


def main():
    db = SessionLocal()
    try:
        try:
            db.execute(text("SET statement_timeout = 120000")); db.commit()
        except Exception:
            db.rollback()

        today = at.ist_today()
        day_start, day_end = at.ist_day_bounds_utc(today)
        rows = db.execute(
            text("""
                SELECT DISTINCT user_id FROM activity_windows
                WHERE user_id IS NOT NULL
                  AND window_start >= :start AND window_start < :end
                ORDER BY user_id
            """),
            {"start": day_start, "end": day_end},
        ).all()
        uids = [r[0] for r in rows]
        log.info("users with windows today (IST %s): %d", today, len(uids))

        done = none = failed = 0
        for i, uid in enumerate(uids, 1):
            try:
                res = at.recompute_daily(db, uid, today)  # commits internally
                if res is None:
                    none += 1
                else:
                    done += 1
                    log.info("  user=%s -> active_pct=%s (windows=%s)",
                             uid, res.active_pct, res.window_count)
            except Exception:
                db.rollback(); failed += 1
            if i % 50 == 0:
                log.info("  progress %d/%d (recomputed=%d none=%d failed=%d)",
                         i, len(uids), done, none, failed)
        log.info("DONE: recomputed=%d none=%d failed=%d", done, none, failed)
    finally:
        db.close()


if __name__ == "__main__":
    main()
