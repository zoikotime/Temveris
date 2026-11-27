"""
backfill_activity_windows.py  —  Stage 3: backfill missing ActivityWindow rows
==============================================================================

Why this exists
---------------
Active Time Engine window staging was commented out 2026-06-17 (commit af5bec8)
during the Tokyo-region incident and restored 2026-06-23 (commit 887cca4). As a
result ~76% of `activities` rows have no `activity_windows` row (all pre-engine
history + the 06-18..06-22 gap + sporadic best-effort misses). This one-off,
re-runnable job backfills those windows and recomputes the affected daily scores
so the engine read-path (and the upcoming hybrid Activity-Log summary) has data.

Safety / idempotency
--------------------
  * Idempotency key = (user_id, window_start). A window is created ONLY if no
    window already exists for that user at that instant — so re-runs never
    duplicate, and we never collide with the 797 orphan windows (activity_id IS
    NULL) that already cover some instants.
  * Keyset pagination by activities.id → forward progress regardless of
    per-row success/failure (no infinite loop on a failing row).
  * Batched (default 1000); commit after each batch; on a batch-commit error we
    roll back and retry that batch row-by-row so one bad row can't lose a batch.
  * materialize_window() never raises (returns None on error) → individual
    failures are counted and skipped, the run continues.

Reconstruction note (intentional approximation)
-----------------------------------------------
The `activities` table does NOT persist mouse_moves, mouse_scrolls,
active_seconds, or hook_active (only the live ingest payload had them). We
reconstruct:
    interval        = activities.interval_seconds OR org screenshot_interval
    active_seconds  = clamp(interval - idle_seconds, 0, interval)   # inverse of ingest
    mouse_moves     = 0      (not persisted)
    mouse_scrolls   = 0      (not persisted)
    hook_active     = True   (not persisted; matches inline legacy default)
So backfilled windows may score marginally lower on the mouse component than a
live-staged window would have. This is acceptable (and far better than no
window, which forces the broken legacy fallback) and is reported in the summary.

This script does NOT modify API logic or the frontend, and does NOT commit git.

Usage (run from the backend/ directory, with the venv):
    python scripts/backfill_activity_windows.py --dry-run
    python scripts/backfill_activity_windows.py --batch-size 1000
    python scripts/backfill_activity_windows.py --no-recompute      # windows only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Make the backend/ package root importable when run as a plain script file.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_BACKEND_ROOT, ".env"))

from sqlalchemy import text  # noqa: E402
from database.database import SessionLocal  # noqa: E402
from database import models  # noqa: E402
from app.active_time import service as at  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

IST = timezone(timedelta(hours=5, minutes=30))
_DEFAULT_INTERVAL = 30  # fallback when an org has no screenshot_interval setting


def _ist_date(ts: datetime):
    """IST calendar date for a (tz-aware) UTC timestamp."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(IST).date()


def _load_org_intervals(db) -> dict:
    """{organization_id: screenshot_interval_seconds} from app_settings."""
    rows = db.execute(text(
        "SELECT organization_id, value FROM app_settings WHERE key = 'screenshot_interval'"
    )).all()
    out = {}
    for org_id, value in rows:
        try:
            n = int(str(value).strip())
            if 1 <= n <= 3600:
                out[org_id] = n
        except Exception:
            pass
    return out


def _load_open_breaks(db) -> dict:
    """{user_id: earliest break_start among currently-open breaks}. Mirrors
    service._has_open_break (filter is_active == True, break_start <= ts)."""
    rows = db.execute(text(
        "SELECT user_id, min(break_start) FROM break_sessions "
        "WHERE is_active = true AND user_id IS NOT NULL GROUP BY user_id"
    )).all()
    out = {}
    for uid, bstart in rows:
        if bstart is not None:
            out[uid] = bstart if bstart.tzinfo else bstart.replace(tzinfo=timezone.utc)
    return out


def _counts(db) -> dict:
    total_act = db.execute(text("SELECT count(*) FROM activities")).scalar()
    total_win = db.execute(text("SELECT count(*) FROM activity_windows")).scalar()
    missing = db.execute(text(
        "SELECT count(*) FROM activities a "
        "WHERE NOT EXISTS (SELECT 1 FROM activity_windows w WHERE w.activity_id = a.id)"
    )).scalar()
    return {"activities": total_act, "windows": total_win, "missing_by_activity_id": missing}


def _print_counts(label: str, c: dict) -> None:
    log.info("%s:", label)
    log.info("    total activities        = %s", f"{c['activities']:,}")
    log.info("    total activity_windows  = %s", f"{c['windows']:,}")
    log.info("    missing windows         = %s  (%.2f%%)",
             f"{c['missing_by_activity_id']:,}",
             100.0 * c["missing_by_activity_id"] / c["activities"] if c["activities"] else 0.0)


def _materialize_from_activity(db, a, *, org_intervals, open_breaks, config) -> bool:
    """Reconstruct materialize_window args from an Activity row. Returns True if
    a window row was added to the session, False otherwise."""
    iv = a.interval_seconds if (a.interval_seconds and a.interval_seconds > 0) else None
    if iv is None:
        iv = org_intervals.get(a.organization_id, _DEFAULT_INTERVAL)
    iv = max(1, int(iv))
    idle = max(0, int(a.idle_seconds or 0))
    active_seconds = max(0, min(iv, iv - idle))  # inverse of ingest's stored_idle
    ts = a.timestamp if a.timestamp.tzinfo else a.timestamp.replace(tzinfo=timezone.utc)
    ob = a.user_id in open_breaks and open_breaks[a.user_id] <= ts
    row = at.materialize_window(
        db,
        user_id=a.user_id,
        session_id=a.session_id,
        activity_id=a.id,
        timestamp=ts,
        interval_seconds=iv,
        key_presses=int(a.key_presses or 0),
        mouse_clicks=int(a.mouse_clicks or 0),
        mouse_moves=0,          # not persisted on activities
        mouse_scrolls=0,        # not persisted on activities
        active_seconds=active_seconds,
        app_name=a.app_name,
        has_screenshot=bool(a.file_path or a.thumbnail),
        hook_active=True,       # not persisted; matches inline legacy default
        on_break=ob,
        config=config,
    )
    return row is not None


def backfill_windows(db, *, batch_size: int, dry_run: bool):
    org_intervals = _load_org_intervals(db)
    open_breaks = _load_open_breaks(db)
    config = at.load_config(db)
    log.info("loaded %d org interval settings, %d users with open breaks",
             len(org_intervals), len(open_breaks))

    processed = created = skipped = failed = 0
    affected: set = set()           # {(user_id, ist_date)}
    seen_keys: set = set()          # (user_id, ts) created this run — intra-run dedupe
    last_id = 0
    batch_no = 0

    # Candidate query: activities lacking ANY window at the same (user, instant).
    # Keyset by id so we always make forward progress.
    sql = text("""
        SELECT a.id, a.user_id, a.organization_id, a.session_id, a.timestamp,
               a.app_name, a.mouse_clicks, a.key_presses, a.idle_seconds,
               a.interval_seconds, a.file_path, a.thumbnail
        FROM activities a
        WHERE a.id > :last_id
          AND a.user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM activity_windows w
              WHERE w.user_id = a.user_id AND w.window_start = a.timestamp
          )
        ORDER BY a.id
        LIMIT :lim
    """)

    while True:
        rows = db.execute(sql, {"last_id": last_id, "lim": batch_size}).all()
        if not rows:
            break
        batch_no += 1
        batch_created = 0
        staged = []
        for a in rows:
            last_id = a.id
            processed += 1
            ts = a.timestamp if a.timestamp.tzinfo else a.timestamp.replace(tzinfo=timezone.utc)
            key = (a.user_id, ts)
            if key in seen_keys:        # duplicate tick within this run
                skipped += 1
                continue
            seen_keys.add(key)
            if dry_run:
                created += 1
                affected.add((a.user_id, _ist_date(ts)))
                continue
            try:
                ok = _materialize_from_activity(
                    db, a, org_intervals=org_intervals, open_breaks=open_breaks, config=config)
            except Exception:
                ok = False
            if ok:
                staged.append(a)
                batch_created += 1
            else:
                failed += 1

        if not dry_run:
            try:
                db.commit()
                created += batch_created
                for a in staged:
                    ts = a.timestamp if a.timestamp.tzinfo else a.timestamp.replace(tzinfo=timezone.utc)
                    affected.add((a.user_id, _ist_date(ts)))
            except Exception as e:
                db.rollback()
                log.warning("batch %d commit failed (%s) — retrying row-by-row", batch_no, e)
                # Re-stage each row in its own transaction so one bad row can't
                # take down the rest of the batch.
                for a in staged:
                    try:
                        if _materialize_from_activity(
                                db, a, org_intervals=org_intervals,
                                open_breaks=open_breaks, config=config):
                            db.commit()
                            created += 1
                            ts = a.timestamp if a.timestamp.tzinfo else a.timestamp.replace(tzinfo=timezone.utc)
                            affected.add((a.user_id, _ist_date(ts)))
                        else:
                            db.rollback()
                            failed += 1
                    except Exception:
                        db.rollback()
                        failed += 1

        log.info("batch %d: processed=%d created=%d skipped=%d failed=%d (last_id=%d)",
                 batch_no, processed, created, skipped, failed, last_id)

    return {"processed": processed, "created": created, "skipped": skipped,
            "failed": failed, "affected": affected}


def recompute_daily_scores(db, affected: set, *, dry_run: bool):
    recomputed = none = failed = 0
    total = len(affected)
    log.info("recomputing daily scores for %d affected (user, IST-day) pairs", total)
    for i, (user_id, day) in enumerate(sorted(affected, key=lambda x: (x[0], str(x[1]))), 1):
        if dry_run:
            recomputed += 1
            continue
        try:
            res = at.recompute_daily(db, user_id, day)  # commits internally
            if res is None:
                none += 1
            else:
                recomputed += 1
        except Exception:
            db.rollback()
            failed += 1
        if i % 100 == 0:
            log.info("  recompute progress %d/%d", i, total)
    return {"recomputed": recomputed, "no_windows": none, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="Backfill ActivityWindow rows + recompute daily scores.")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true", help="count only; no writes")
    ap.add_argument("--no-recompute", action="store_true", help="skip the daily-score recompute phase")
    ap.add_argument("--force", action="store_true",
                    help="run even when an organization is not on +05:30 (writes their rows on the wrong local day)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # This script buckets days on a hardcoded +05:30 (see IST above).
        # Stop rather than write India-bucketed rows for another tenant.
        from scripts._tz_guard import require_ist_only
        require_ist_only(db, log, force=args.force)
        try:
            db.execute(text("SET statement_timeout = 60000"))
            db.commit()
        except Exception:
            db.rollback()

        before = _counts(db)
        log.info("=" * 60)
        _print_counts("BEFORE", before)
        log.info("=" * 60)
        if args.dry_run:
            log.info("DRY RUN — no rows will be written")

        wstats = backfill_windows(db, batch_size=args.batch_size, dry_run=args.dry_run)
        log.info("-" * 60)
        log.info("WINDOW BACKFILL: processed=%d created=%d skipped=%d failed=%d  affected_user_days=%d",
                 wstats["processed"], wstats["created"], wstats["skipped"],
                 wstats["failed"], len(wstats["affected"]))

        if not args.no_recompute:
            rstats = recompute_daily_scores(db, wstats["affected"], dry_run=args.dry_run)
            log.info("DAILY RECOMPUTE: recomputed=%d no_windows=%d failed=%d",
                     rstats["recomputed"], rstats["no_windows"], rstats["failed"])

        after = _counts(db)
        log.info("=" * 60)
        _print_counts("AFTER", after)
        log.info("    created this run        = %s", f"{wstats['created']:,}")
        log.info("=" * 60)
    finally:
        db.close()


if __name__ == "__main__":
    main()
