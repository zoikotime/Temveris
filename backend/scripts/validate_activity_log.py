"""
validate_activity_log.py  —  Stage-3 validation harness (READ-MOSTLY)
=====================================================================
Compares the engine-authoritative Dashboard numbers against what
/activity-log/summary currently returns, for one employee and the org-wide
(admin) view, on the IST "today".

It calls the REAL endpoint functions directly (no logic replication):
  * Dashboard value      -> get_day_summary()            (engine via get_daily)
  * Activity Log value   -> get_activity_summary()        (current endpoint)
Engine productivity target is bucketed from ActivityWindow.active_credit_seconds.
Counters (screenshots/clicks/keys/browser/sessions/breaks) are checked against
raw DB ground truth.

NOTE: calling get_day_summary for "today" may trigger get_daily's on-demand
recompute (a write) — run ONLY after the backfill recompute has finished.

Usage (from backend/, with the venv):
    python scripts/validate_activity_log.py --user 142 --admin 115
"""
from __future__ import annotations
import argparse, os, sys
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_ROOT, ".env"))

from sqlalchemy import text
from database.database import SessionLocal
from database import models
from app.activity_log.router import (
    get_activity_summary, _normalise_app_category, _get_overrides,
)
from app.app_review.productivity import categorise as categorise_app
from app.employees.router import get_day_summary

IST = timezone(timedelta(hours=5, minutes=30))


def ist_bounds_utc(d):
    start = datetime.combine(d, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def engine_productivity(db, org_id, user_id, s, e):
    """Bucket ActivityWindow.active_credit_seconds by app category — the T3
    productivity target (single source = windows)."""
    app_ov, _ = _get_overrides(db, org_id)
    q = db.query(models.ActivityWindow).filter(
        models.ActivityWindow.window_start >= s,
        models.ActivityWindow.window_start < e,
    )
    if user_id is not None:
        q = q.filter(models.ActivityWindow.user_id == user_id)
    else:
        # Mirror the endpoint: scope org-wide windows via the owning user so
        # NULL/foreign user_id orphan windows are excluded on both sides.
        q = q.join(models.User, models.User.id == models.ActivityWindow.user_id).filter(
            models.User.organization_id == org_id)
    buckets = {"productive": 0, "neutral": 0, "distracting": 0}
    for w in q.all():
        b = _normalise_app_category(categorise_app(w.app_name, app_ov))
        buckets[b] += int(w.active_credit_seconds or 0)
    return buckets


def engine_daily(db, user_id, anchor, org_id):
    """Sum ActivityDailyScore active/idle/worked for the EXACT IST-day anchor."""
    q = db.query(
        models.ActivityDailyScore.active_seconds,
        models.ActivityDailyScore.idle_seconds,
        models.ActivityDailyScore.worked_seconds,
    ).filter(
        models.ActivityDailyScore.day == anchor,
    )
    if user_id is not None:
        q = q.filter(models.ActivityDailyScore.user_id == user_id)
    else:
        q = q.join(models.User, models.User.id == models.ActivityDailyScore.user_id).filter(
            models.User.organization_id == org_id)
    a = i = w = 0
    for row in q.all():
        a += int(row.active_seconds or 0); i += int(row.idle_seconds or 0); w += int(row.worked_seconds or 0)
    return {"active": a, "idle": i, "worked": w}


def raw_counters(db, org_id, user_id, s, e):
    def cnt(sql, **extra):
        p = {"o": org_id, "s": s, "e": e, **extra}
        return db.execute(text(sql), p).scalar() or 0
    uf_a = "AND user_id=:u" if user_id else ""
    p = {"u": user_id} if user_id else {}
    shots = cnt(f"SELECT count(*) FROM activities WHERE organization_id=:o AND timestamp>=:s AND timestamp<=:e AND (file_path IS NOT NULL OR thumbnail IS NOT NULL) {uf_a}", **p)
    clicks = cnt(f"SELECT COALESCE(sum(mouse_clicks),0) FROM activities WHERE organization_id=:o AND timestamp>=:s AND timestamp<=:e {uf_a}", **p)
    keys = cnt(f"SELECT COALESCE(sum(key_presses),0) FROM activities WHERE organization_id=:o AND timestamp>=:s AND timestamp<=:e {uf_a}", **p)
    brow = cnt(f"SELECT count(*) FROM browser_activities WHERE organization_id=:o AND started_at>=:s AND started_at<=:e {uf_a}", **p)
    sess = cnt(f"SELECT count(*) FROM clock_sessions WHERE clock_in_time>=:s AND clock_in_time<=:e AND (organization_id=:o OR organization_id IS NULL) {uf_a}", **p)
    brk = cnt(f"SELECT count(*) FROM break_sessions WHERE break_start>=:s AND break_start<=:e AND (organization_id=:o OR organization_id IS NULL) {uf_a}", **p)
    return dict(screenshots=shots, mouse_clicks=clicks, key_presses=keys, browser_visits=brow, sessions=sess, breaks=brk)


def fs(x):
    x = int(x); h = x // 3600; m = (x % 3600) // 60; s = x % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def pct(p):
    tot = p["productive"] + p["neutral"] + p["distracting"]
    return round(100.0 * p["productive"] / tot, 1) if tot else 0.0


def run_scope(db, label, *, org, user_obj, scope, user_id_for_engine, target_date):
    s, e = ist_bounds_utc(target_date)
    now = datetime.now(timezone.utc)
    e_eff = min(e, now)
    anchor = s  # IST midnight (UTC) == ActivityDailyScore.day

    # Activity Log (current endpoint) — exact output
    alog = get_activity_summary(
        start=s.isoformat(), end=e_eff.isoformat(), user_id=None, scope=scope,
        db=db, user=user_obj, org=org,
    )
    al_active = alog.totals["active_seconds"]; al_idle = alog.totals["idle_seconds"]
    al_prod = {k: alog.productivity[k] for k in ("productive", "neutral", "distracting")}
    al_hourly_active = sum(h["productive"] + h["neutral"] + h["distracting"] for h in alog.hourly)

    # Engine-authoritative targets
    eng = engine_daily(db, user_id_for_engine, anchor, org.id)
    eng_prod = engine_productivity(db, org.id, user_id_for_engine, s, e_eff)
    eng_hourly_active = eng_prod["productive"] + eng_prod["neutral"] + eng_prod["distracting"]

    # Dashboard value (employee only — get_day_summary is per-user)
    dash = None
    if user_id_for_engine is not None:
        dash = get_day_summary(date=target_date.strftime("%Y-%m-%d"), db=db, user=user_obj)

    ctr = raw_counters(db, org.id, user_id_for_engine, s, e_eff)

    print("\n" + "=" * 78 + f"\nSCOPE: {label}   (IST {target_date})")
    rows = []
    def add(metric, dash_v, al_v, ok):
        rows.append((metric, dash_v, al_v, "PASS" if ok else "FAIL"))

    # Active
    dv = dash["active_seconds"] if dash else eng["active"]
    add("Active Time", fs(dv), fs(al_active), abs(dv - al_active) <= 60)
    # Idle (= worked - active)
    div = (dash["worked_seconds"] - dash["active_seconds"]) if dash else eng["idle"]
    add("Idle Time", fs(div), fs(al_idle), abs(div - al_idle) <= 60)
    # Productive time + %
    add("Productive (sec)", fs(eng_prod["productive"]), fs(al_prod["productive"]),
        abs(eng_prod["productive"] - al_prod["productive"]) <= 60)
    add("Productivity %", f"{pct(eng_prod)}%", f"{pct(al_prod)}%", abs(pct(eng_prod) - pct(al_prod)) <= 1.0)
    # Hourly (compare total active across the chart)
    add("Hourly Σactive", fs(eng_hourly_active), fs(al_hourly_active),
        abs(eng_hourly_active - al_hourly_active) <= 60)
    # Counters (ground truth vs Activity Log)
    add("Screenshots", ctr["screenshots"], alog.totals["screenshots"], ctr["screenshots"] == alog.totals["screenshots"])
    add("Mouse Clicks", ctr["mouse_clicks"], alog.totals["mouse_clicks"], ctr["mouse_clicks"] == alog.totals["mouse_clicks"])
    add("Keystrokes", ctr["key_presses"], alog.totals["key_presses"], ctr["key_presses"] == alog.totals["key_presses"])
    add("Browser Visits", ctr["browser_visits"], alog.totals["browser_visits"], ctr["browser_visits"] == alog.totals["browser_visits"])
    add("Sessions", ctr["sessions"], alog.totals["sessions"], ctr["sessions"] == alog.totals["sessions"])
    add("Breaks", ctr["breaks"], alog.totals["breaks"], ctr["breaks"] == alog.totals["breaks"])

    print(f"{'Metric':<18}{'Dashboard/Engine':>20}{'Activity Log':>18}{'Result':>8}")
    print("-" * 78)
    for m, d, a, r in rows:
        print(f"{m:<18}{str(d):>20}{str(a):>18}{r:>8}")
    fails = [m for m, *_ , r in rows if r == "FAIL"]
    print(f"\n  -> {'ALL PASS' if not fails else 'FAIL: ' + ', '.join(fails)}")
    return not fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=0, help="0 = auto-pick top user for --date")
    ap.add_argument("--admin", type=int, default=115)
    ap.add_argument("--date", type=str, default="2026-06-23", help="IST date YYYY-MM-DD")
    args = ap.parse_args()
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    s, e = ist_bounds_utc(target_date)
    db = SessionLocal()
    try:
        org = db.query(models.Organization).filter(models.Organization.id == 1).first()
        uid = args.user
        if not uid:
            uid = db.execute(text(
                "SELECT user_id FROM activities WHERE organization_id=1 AND timestamp>=:s AND timestamp<:e "
                "AND user_id IS NOT NULL GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
            ), {"s": s, "e": e}).scalar()
        print(f"target IST date={target_date}  employee u={uid}  admin u={args.admin}")
        emp = db.query(models.User).filter(models.User.id == uid).first()
        adm = db.query(models.User).filter(models.User.id == args.admin).first()
        ok1 = run_scope(db, f"Employee self (u={uid})", org=org, user_obj=emp,
                        scope="self", user_id_for_engine=uid, target_date=target_date)
        ok2 = run_scope(db, f"Admin / All employees (admin u={args.admin})", org=org, user_obj=adm,
                        scope=None, user_id_for_engine=None, target_date=target_date)
        print("\n" + "=" * 78)
        print("OVERALL:", "ALL PASS" if (ok1 and ok2) else "FAILURES PRESENT")
    finally:
        db.close()


if __name__ == "__main__":
    main()
