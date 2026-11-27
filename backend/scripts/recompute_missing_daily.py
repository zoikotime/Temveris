"""
recompute_missing_daily.py — fill ActivityDailyScore for (user, IST-day) pairs
that have windows but no daily score yet (so org-wide aggregates reflect truth).

Idempotent: recompute_daily upserts. Safe to re-run. READ/WRITE to daily +
minute score tables only; does NOT touch activities/windows/API/frontend.
"""
from __future__ import annotations
import os, sys, logging
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_ROOT, ".env"))

from sqlalchemy import text
from database.database import SessionLocal
from app.active_time import service as at

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("recompute-missing")
IST = timezone(timedelta(hours=5, minutes=30))


def main():
    db = SessionLocal()
    try:
        # This script buckets days on a hardcoded +05:30 (see the SQL below).
        # Stop rather than write India-bucketed rows for another tenant.
        from scripts._tz_guard import require_ist_only
        require_ist_only(db, log, force="--force" in sys.argv)
        try:
            db.execute(text("SET statement_timeout = 60000")); db.commit()
        except Exception:
            db.rollback()
        rows = db.execute(text("""
            SELECT DISTINCT w.user_id, (w.window_start AT TIME ZONE '+05:30')::date AS d
            FROM activity_windows w
            WHERE w.user_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM activity_daily_scores ds
                WHERE ds.user_id = w.user_id
                  AND date_trunc('day', (ds.day AT TIME ZONE '+05:30'))
                      = date_trunc('day', (w.window_start AT TIME ZONE '+05:30'))
              )
            ORDER BY d, w.user_id
        """)).all()
        log.info("missing (user, IST-day) pairs to recompute: %d", len(rows))
        done = none = failed = 0
        for i, (uid, d) in enumerate(rows, 1):
            try:
                res = at.recompute_daily(db, uid, d)  # commits internally
                if res is None:
                    none += 1
                else:
                    done += 1
            except Exception:
                db.rollback(); failed += 1
            if i % 50 == 0:
                log.info("  progress %d/%d (recomputed=%d none=%d failed=%d)", i, len(rows), done, none, failed)
        log.info("DONE: recomputed=%d none=%d failed=%d", done, none, failed)
        rem = db.execute(text("""
            SELECT count(*) FROM (
              SELECT DISTINCT w.user_id, (w.window_start AT TIME ZONE '+05:30')::date AS d
              FROM activity_windows w WHERE w.user_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM activity_daily_scores ds
                WHERE ds.user_id=w.user_id AND date_trunc('day',(ds.day AT TIME ZONE '+05:30'))
                  = date_trunc('day',(w.window_start AT TIME ZONE '+05:30')))) t
        """)).scalar()
        log.info("remaining missing after run: %d", rem)
    finally:
        db.close()


if __name__ == "__main__":
    main()
