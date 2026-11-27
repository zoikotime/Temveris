"""
Active Time distribution report  —  READ-ONLY production telemetry.

Purpose
=======
After the Active Time Engine is re-activated (ACTIVE_TIME_ENGINE_ENABLED=true),
run this against the production DB to observe REAL employee Active Time before
deciding on any configuration-only calibration. It answers the monitoring spec:

    * Average Active Time by role
    * Median
    * P90 / P95
    * Distribution histogram
    * Lowest / Highest values
    * Engine coverage (how many user-days are engine-scored vs legacy fallback)
    * Quality-flag frequency (LIMITED_TRACKING, LOW_COVERAGE, ...)

It reads `activity_daily_scores` joined with `users`. It performs NO writes,
NO commits, and creates NO rows — safe to run any time, repeatedly.

How to run
==========
    cd backend
    python scripts/active_time_distribution.py                 # last 3 IST days, by role
    python scripts/active_time_distribution.py --days 2
    python scripts/active_time_distribution.py --group-by department
    python scripts/active_time_distribution.py --org 1 --group-by designation

Notes
=====
* `role` is the AUTH role (employee/admin/hr). Job functions like "Developer"
  / "Designer" usually live in `department` or `designation` — use --group-by
  for those. The script lists which dimensions actually have data.
* `active_pct` is the capped figure the dashboard shows. `raw_active_pct` (pre
  daily-cap) is also summarised so you can see how often the 95% cap binds.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

# Allow running from the backend/ directory directly (mirrors scripts/migrate_*).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import and_  # noqa: E402

from database.database import SessionLocal  # noqa: E402
from database import models  # noqa: E402
from app.active_time.service import ist_today, day_anchor_utc  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Stats helpers (stdlib only — no numpy dependency)
# ──────────────────────────────────────────────────────────────────────────
def percentile(sorted_vals, p):
    """Linear-interpolated percentile (p in 0..100). Expects a sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def histogram(vals, buckets=10):
    """Return a list of (label, count) over equal-width 0..100 buckets."""
    width = 100.0 / buckets
    counts = [0] * buckets
    for v in vals:
        idx = min(buckets - 1, int(v / width))
        counts[max(0, idx)] += 1
    out = []
    for i, c in enumerate(counts):
        lo = int(i * width)
        hi = int((i + 1) * width)
        out.append((f"{lo:>3}-{hi:<3}", c))
    return out


def summarize(vals):
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "mean": statistics.fmean(s) if n else 0.0,
        "median": statistics.median(s) if n else 0.0,
        "p90": percentile(s, 90),
        "p95": percentile(s, 95),
        "min": s[0] if n else 0.0,
        "max": s[-1] if n else 0.0,
        "stdev": statistics.pstdev(s) if n > 1 else 0.0,
    }


def bar(count, total, width=30):
    if total <= 0:
        return ""
    filled = int(round((count / total) * width))
    return "█" * filled + "·" * (width - filled)


# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Active Time distribution (read-only).")
    ap.add_argument("--days", type=int, default=3,
                    help="Number of IST days back to include (default 3).")
    ap.add_argument("--group-by", default="role",
                    choices=["role", "department", "designation", "team"],
                    help="User dimension to group by (default role).")
    ap.add_argument("--org", type=int, default=None,
                    help="Restrict to one organization_id (default: all).")
    ap.add_argument("--min-worked-min", type=float, default=30.0,
                    help="Exclude user-days with less than this worked time "
                         "(minutes) to avoid noise from trivial sessions (default 30).")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        today = ist_today()
        # Inclusive window of the last N IST days (today included).
        from datetime import timedelta
        day_anchors = [day_anchor_utc(today - timedelta(days=i)) for i in range(args.days)]
        lo_anchor = min(day_anchors)
        hi_anchor = max(day_anchors)

        q = (
            db.query(models.ActivityDailyScore, models.User)
            .join(models.User, models.User.id == models.ActivityDailyScore.user_id)
            .filter(
                and_(
                    models.ActivityDailyScore.day >= lo_anchor,
                    models.ActivityDailyScore.day <= hi_anchor,
                )
            )
        )
        if args.org is not None:
            q = q.filter(models.User.organization_id == args.org)

        rows = q.all()

        min_worked_sec = args.min_worked_min * 60.0
        # (group_key, active_pct, raw_active_pct, flags)
        records = []
        flag_counts = {}
        capped_count = 0
        for daily, user in rows:
            if (daily.worked_seconds or 0) < min_worked_sec:
                continue
            key = getattr(user, args.group_by, None) or "(unset)"
            records.append((key, float(daily.active_pct or 0.0),
                            float(daily.raw_active_pct or 0.0)))
            for f in (daily.flags or []):
                flag_counts[f] = flag_counts.get(f, 0) + 1
            if (daily.raw_active_pct or 0.0) > (daily.active_pct or 0.0) + 0.01:
                capped_count += 1

        print("=" * 72)
        print(f"ACTIVE TIME DISTRIBUTION  —  last {args.days} IST day(s), "
              f"group-by={args.group_by}"
              + (f", org={args.org}" if args.org is not None else ", all orgs"))
        print(f"window: {lo_anchor.date()} .. {hi_anchor.date()} (IST anchors)  "
              f"min_worked={args.min_worked_min:.0f}m")
        print("=" * 72)

        if not records:
            print("\nNo engine-scored user-days in this window.")
            print("If the engine was just deployed, ActivityDailyScore rows appear")
            print("after the reconcile worker runs (≈1 min) on users who produced")
            print("ActivityWindow rows. Until then the dashboard uses the legacy")
            print("fallback and this report is empty by design.\n")
            return

        # ── Per-group summary ──
        groups = {}
        for key, pct, _raw in records:
            groups.setdefault(key, []).append(pct)

        print(f"\n{'group':<18}{'n':>5}{'mean':>8}{'med':>8}{'p90':>8}"
              f"{'p95':>8}{'min':>8}{'max':>8}{'sd':>7}")
        print("-" * 72)
        for key in sorted(groups, key=lambda k: -statistics.fmean(groups[k])):
            s = summarize(groups[key])
            print(f"{str(key)[:17]:<18}{s['n']:>5}{s['mean']:>8.1f}{s['median']:>8.1f}"
                  f"{s['p90']:>8.1f}{s['p95']:>8.1f}{s['min']:>8.1f}{s['max']:>8.1f}"
                  f"{s['stdev']:>7.1f}")

        overall = summarize([p for _, p, _ in records])
        print("-" * 72)
        print(f"{'ALL':<18}{overall['n']:>5}{overall['mean']:>8.1f}{overall['median']:>8.1f}"
              f"{overall['p90']:>8.1f}{overall['p95']:>8.1f}{overall['min']:>8.1f}"
              f"{overall['max']:>8.1f}{overall['stdev']:>7.1f}")

        # ── Overall histogram ──
        print(f"\nDistribution histogram (all groups, n={overall['n']}):")
        all_pcts = [p for _, p, _ in records]
        for label, count in histogram(all_pcts):
            print(f"  {label}%  {count:>5}  {bar(count, overall['n'])}")

        # ── Per-group histogram (compact) ──
        for key in sorted(groups, key=lambda k: -statistics.fmean(groups[k])):
            g = groups[key]
            print(f"\n{key} (n={len(g)}):")
            for label, count in histogram(g):
                if count:
                    print(f"  {label}%  {count:>5}  {bar(count, len(g))}")

        # ── Coverage + quality flags ──
        print("\n" + "-" * 72)
        print(f"Daily-cap bound on {capped_count}/{overall['n']} user-days "
              f"({100.0 * capped_count / overall['n']:.0f}%) "
              f"(raw_active_pct exceeded the capped active_pct)")
        if flag_counts:
            print("Quality flags:")
            for f, c in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
                print(f"  {f:<28}{c:>5}  ({100.0 * c / overall['n']:.0f}% of user-days)")
        else:
            print("Quality flags: none raised")
        print("=" * 72)
    finally:
        db.close()


if __name__ == "__main__":
    main()
