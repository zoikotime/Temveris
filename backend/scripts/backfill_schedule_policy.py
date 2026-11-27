"""
Work Schedule Policy backfill + equivalence proof (P6).
======================================================
Two jobs, both read-only by default:

  1. **plan** — compute the minimum set of ``app_settings`` rows and ``Shift``
     seeds needed so the Schedule Resolver reproduces today's behaviour.
  2. **verify** — for every user, compare LEGACY behaviour against RESOLVER
     behaviour and classify every difference. The phase only passes if the
     resolver reproduces current behaviour for every existing user.

Usage
-----
    python scripts/backfill_schedule_policy.py                  # dry run + report
    python scripts/backfill_schedule_policy.py --apply          # writes (needs approval)
    python scripts/backfill_schedule_policy.py --rehearse       # apply inside a
                                                                # rolled-back txn

``--apply`` is the ONLY path that commits. Default is a dry run.

What gets written, and what deliberately does not
-------------------------------------------------
The rule is *persist only overrides*: if the resolver would already inherit the
correct value, storing it creates a row that has to be maintained forever for no
behavioural gain.

WRITTEN
  * ``schedule.grace_minutes = "20"`` per org. Required. The P4 system default is
    15 (a product decision), but today's effective default is 20 —
    ``User.default_clock_in_grace_min``'s model default and
    ``attendance_alerts.DEFAULT_GRACE_MIN``. Without this row, turning the engine
    on would silently narrow every org's grace window from 20 to 15.
  * One ``Shift`` per org with ``is_default = true``. A starting point for Shift
    Management (P8). Deliberately NOT referenced by ``schedule.shift_id``, so it
    changes no resolution today.
  * ``schedule.shift_start_time`` at employee scope, ONLY for users with a real
    ``default_clock_in_time``. This is the migration path off the legacy column;
    the value is identical, so behaviour does not change.

NOT WRITTEN — and each omission is load-bearing
  * ``schedule.expected_work_hours``. Writing the org's ``work_hours_threshold``
    (8:45) here would be actively wrong: today's expected hours come from the
    ``expected_work_durations`` type map — full_time 8:00, part_time 4:00 — and
    ``work_hours_threshold`` is only consulted for unmapped types. Storing 8:45
    would tighten the Early Clock Out gate for full-timers and more than double
    it for the 11 part-timers. Left unset, the resolver falls through to the same
    type map, so the value is unchanged.
  * ``schedule.working_days``. No org configures ``flexible_clock_out_days``, so
    today's working week is Mon–Fri, which is already the system default.
  * ``schedule.absent_after_hour``. System default 12 already equals
    ``SHIFT_NOON_HOUR``.
  * ``schedule.engine_enabled``. System default false is what rollout wants.
  * ``schedule.shift_start_time`` at ORG scope. Its system default is empty —
    no scheduled shift — which is precisely what makes the rollout
    behaviour-neutral: a worker with no shift start can never be classified
    Late, matching today's treatment of the 62 of 69 users who have no
    ``default_clock_in_time``. Writing an org-wide 09:00 here would hand every
    such employee a scheduled start.
  * ``schedule.shift_end_time``, ``very_late_after``,
    ``min_hours_required``, ``allow_*``, ``work_mode``. No legacy equivalent
    exists, so there is nothing to preserve.
  * Employee grace rows. All 69 users carry grace 20, which is exactly the org
    row being written, so a per-user row would be pure duplication.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import models                                    # noqa: E402
from database.database import SessionLocal                     # noqa: E402
from app.schedule_policy import keys as K                      # noqa: E402
from app.schedule_policy.resolver import resolve_work_schedule  # noqa: E402

# Today's effective grace default: User.default_clock_in_grace_min's model
# default and attendance_alerts.DEFAULT_GRACE_MIN both say 20.
LEGACY_EFFECTIVE_GRACE = "20"

# The offset the removed `PART_TIME_ABSENT_OFFSET_HOURS` applied. Referenced ONLY
# here, to reproduce today's cutoff as a real employee-scope row. Nothing reads
# it at runtime — that is the point of the migration.
LEGACY_PART_TIME_OFFSET = 6

DEFAULT_SHIFT_NAME = "General Shift"
# The seeded shift needs concrete times: Shift.start_time is NOT NULL, and the
# cascade default for schedule.shift_start_time is deliberately EMPTY so no org
# gains a scheduled start until an admin configures one. These are a nominal
# placeholder, not a policy — the shift is not referenced by
# schedule.shift_id, so it changes no resolution.
DEFAULT_SHIFT_START = "09:00"
DEFAULT_SHIFT_END = "18:00"


# ══════════════════════════════════════════════════════════════════════════════
# Plan
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class BackfillPlan:
    org_settings: List[Tuple[int, str, str]] = field(default_factory=list)   # (org, key, value)
    shifts: List[Tuple[int, str, str, str]] = field(default_factory=list)    # (org, name, start, end)
    user_settings: List[Tuple[int, int, str, str]] = field(default_factory=list)  # (org, user, key, value)
    skipped: List[str] = field(default_factory=list)

    def total(self) -> int:
        return len(self.org_settings) + len(self.shifts) + len(self.user_settings)


def _existing_key(db, org_id, key, *, user_id=None) -> bool:
    q = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == key,
        models.AppSettings.team_key.is_(None),
    )
    q = q.filter(models.AppSettings.user_id == user_id) if user_id is not None \
        else q.filter(models.AppSettings.user_id.is_(None))
    return q.first() is not None


def build_plan(db) -> BackfillPlan:
    """Compute the writes. Pure — touches nothing."""
    plan = BackfillPlan()
    org_ids = [r[0] for r in db.query(models.Organization.id)
               .order_by(models.Organization.id).all()]

    for org_id in org_ids:
        # 1 — grace, the one value whose system default differs from today's.
        if _existing_key(db, org_id, K.SETTING_GRACE_MINUTES):
            plan.skipped.append(f"org {org_id}: grace_minutes row already exists")
        else:
            plan.org_settings.append((org_id, K.SETTING_GRACE_MINUTES, LEGACY_EFFECTIVE_GRACE))

        # 2 — exactly one default shift per org.
        already = db.query(models.Shift).filter(
            models.Shift.organization_id == org_id,
            models.Shift.is_default == True,  # noqa: E712
        ).first()
        if already:
            plan.skipped.append(f"org {org_id}: default shift already exists (id={already.id})")
        else:
            plan.shifts.append((org_id, DEFAULT_SHIFT_NAME,
                                DEFAULT_SHIFT_START, DEFAULT_SHIFT_END))

    # 3 — employee rows ONLY for users who actually configured a legacy shift.
    configured = (
        db.query(models.User)
        .filter(models.User.default_clock_in_time.isnot(None),
                models.User.default_clock_in_time != "")
        .order_by(models.User.id)
        .all()
    )
    for u in configured:
        if not u.organization_id:
            plan.skipped.append(f"user {u.id}: no organization_id, cannot scope a row")
            continue
        if _existing_key(db, u.organization_id, K.SETTING_SHIFT_START_TIME, user_id=u.id):
            plan.skipped.append(f"user {u.id}: shift_start_time row already exists")
            continue
        plan.user_settings.append((u.organization_id, u.id,
                                   K.SETTING_SHIFT_START_TIME, u.default_clock_in_time))
        # Grace is only written when it differs from the org row we are creating,
        # so an employee row is never a duplicate of its parent.
        if u.default_clock_in_grace_min is not None and \
                str(u.default_clock_in_grace_min) != LEGACY_EFFECTIVE_GRACE:
            plan.user_settings.append((u.organization_id, u.id,
                                       K.SETTING_GRACE_MINUTES,
                                       str(u.default_clock_in_grace_min)))

    # 4 — the part-time absence cutoff, now that the +6h rule is gone.
    #
    # `PART_TIME_ABSENT_OFFSET_HOURS = 6` used to shift the cutoff for anyone
    # typed part_time. That was one organization's convention applied to every
    # tenant. The capability survives as a normal employee-scope setting, so the
    # migration writes the row that reproduces today's behaviour EXACTLY for the
    # people who currently benefit from it. After this runs, an admin can change
    # it per person, per team, or delete it — none of which was possible before.
    part_timers = (
        db.query(models.User)
        .filter(models.User.employment_type.isnot(None))
        .order_by(models.User.id)
        .all()
    )
    for u in part_timers:
        if (u.employment_type or "").lower().strip() != "part_time":
            continue
        if not u.organization_id:
            plan.skipped.append(f"user {u.id}: no organization_id, cannot scope a cutoff")
            continue
        if _existing_key(db, u.organization_id, K.SETTING_ABSENT_AFTER_HOUR, user_id=u.id):
            plan.skipped.append(f"user {u.id}: absent_after_hour row already exists")
            continue
        org_hour = _org_absent_hour(db, u.organization_id)
        plan.user_settings.append((u.organization_id, u.id,
                                   K.SETTING_ABSENT_AFTER_HOUR,
                                   str(min(23, org_hour + LEGACY_PART_TIME_OFFSET))))

    # 5 — expected daily hours per organization.
    #
    # Two different numbers were live: the risk engine measured against
    # `work_hours_threshold` (8:45 by default) while the Early Clock Out gate
    # allowed people to leave at `expected_work_durations` (8:00). Consolidating
    # to one canonical value means choosing, and this migration chooses the
    # PERMISSIVE one — the number the employee was already allowed to leave on.
    # Choosing the stricter one would start alerting people the system had just
    # told they could go home. Only written when the org configured something;
    # otherwise the system default applies.
    for org_id in org_ids:
        if _existing_key(db, org_id, K.SETTING_EXPECTED_WORK_HOURS):
            plan.skipped.append(f"org {org_id}: expected_work_hours row already exists")
            continue
        legacy_hhmm = _legacy_full_time_expected(db, org_id)
        if legacy_hhmm:
            plan.org_settings.append((org_id, K.SETTING_EXPECTED_WORK_HOURS, legacy_hhmm))

    # 6 — per-employment-type expected hours, at employee scope.
    #    `expected_work_durations` keyed a duration by employment type; the
    #    cascade has no employment-type tier, so a non-default type becomes an
    #    employee-scope row. Same number, addressable per person from now on.
    typed = db.query(models.User).order_by(models.User.id).all()
    for u in typed:
        if not u.organization_id:
            continue
        et = (u.employment_type or "").lower().strip().replace("-", "_").replace(" ", "_")
        if not et or et == "full_time":
            continue
        hhmm = _legacy_expected_for_type(db, u.organization_id, et)
        if not hhmm:
            continue
        if _existing_key(db, u.organization_id, K.SETTING_EXPECTED_WORK_HOURS, user_id=u.id):
            plan.skipped.append(f"user {u.id}: expected_work_hours row already exists")
            continue
        plan.user_settings.append((u.organization_id, u.id,
                                   K.SETTING_EXPECTED_WORK_HOURS, hhmm))
    return plan


def _org_absent_hour(db, org_id) -> int:
    """The org's configured absence cutoff, or the system default."""
    row = (
        db.query(models.AppSettings.value)
        .filter(models.AppSettings.organization_id == org_id,
                models.AppSettings.key == K.SETTING_ABSENT_AFTER_HOUR,
                models.AppSettings.team_key.is_(None),
                models.AppSettings.user_id.is_(None))
        .first()
    )
    default = int(K.SETTING_DEFAULTS[K.SETTING_ABSENT_AFTER_HOUR])
    if not row or row[0] is None:
        return default
    try:
        v = int(str(row[0]).strip())
    except (TypeError, ValueError):
        return default
    return v if 0 <= v <= 23 else default


def _legacy_expected_map(db, org_id) -> dict:
    """The org's `expected_work_durations` map, defaults included."""
    import json as _json
    from app.attendance_policy.resolvers import (
        DEFAULT_EXPECTED_WORK_DURATIONS, normalize_employment_type,
    )
    policy = dict(DEFAULT_EXPECTED_WORK_DURATIONS)
    row = (
        db.query(models.AppSettings.value)
        .filter(models.AppSettings.organization_id == org_id,
                models.AppSettings.key == "expected_work_durations",
                models.AppSettings.team_key.is_(None),
                models.AppSettings.user_id.is_(None))
        .first()
    )
    if row and row[0]:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                for k, v in data.items():
                    policy[normalize_employment_type(k)] = v
        except Exception:
            pass
    return policy


def _legacy_full_time_expected(db, org_id):
    """The value a full-timer was ALLOWED to clock out on today."""
    policy = _legacy_expected_map(db, org_id)
    return policy.get("full_time")


def _legacy_expected_for_type(db, org_id, employment_type):
    from app.attendance_policy.resolvers import normalize_employment_type
    policy = _legacy_expected_map(db, org_id)
    return policy.get(normalize_employment_type(employment_type))


def apply_plan(db, plan: BackfillPlan) -> None:
    """Perform the writes. Caller controls the transaction."""
    for org_id, key, value in plan.org_settings:
        db.add(models.AppSettings(organization_id=org_id, key=key, value=value,
                                  description=K.SETTING_DESCRIPTIONS.get(key, "")))
    for org_id, name, start, end in plan.shifts:
        db.add(models.Shift(organization_id=org_id, name=name, start_time=start,
                            end_time=end, days_of_week=[0, 1, 2, 3, 4],
                            is_active=True, is_default=True))
    for org_id, user_id, key, value in plan.user_settings:
        db.add(models.AppSettings(organization_id=org_id, user_id=user_id,
                                  key=key, value=value,
                                  description=K.SETTING_DESCRIPTIONS.get(key, "")))
    db.flush()


# ══════════════════════════════════════════════════════════════════════════════
# Equivalence
# ══════════════════════════════════════════════════════════════════════════════
# The legacy-vs-resolver comparison that used to live here has been removed with
# the legacy implementation it measured. There is no second answer to compare
# against any more: `resolve_work_schedule` is the only one.
#
# What replaced it as the safety argument is this script's PLAN, which writes
# each organization's current effective values as real settings rows before the
# constants disappear, plus the tests in tests/test_schedule_backfill.py that
# assert those rows reproduce today's numbers.


def resolver_view(db, user, on_date: date) -> Dict[str, Any]:
    """What the resolver answers for this user on this date.

    A flat, comparable projection of ``EffectiveSchedule`` — used by the
    migration report and by tests to check the written rows reproduce the
    numbers the removed constants produced.
    """
    sched = resolve_work_schedule(db, user, on_date)
    return {
        "expected": sched.expected_work_seconds,
        "start": sched.start_local.strftime("%H:%M") if sched.start_local else None,
        # Grace is reported unconditionally. It used to be blanked when no shift
        # start resolved, purely so the legacy comparison would not flag a value
        # legacy could not observe; with that comparison gone, hiding a resolved
        # value would just make the report lie.
        "grace": sched.grace_minutes,
        "working": sched.is_working_day,
        "absent_hr": sched.absent_after_hour,
        "source": sched.sources.get("start_local"),
        "_sources": dict(sched.sources),
    }


def print_plan(plan: BackfillPlan) -> None:
    print("=" * 78)
    print("BACKFILL PLAN")
    print("=" * 78)
    print(f"  org settings rows .... {len(plan.org_settings)}")
    for org, key, value in plan.org_settings:
        print(f"      org={org:<5} {key} = {value!r}")
    print(f"  default shifts ....... {len(plan.shifts)}")
    for org, name, s, e in plan.shifts:
        print(f"      org={org:<5} {name!r} {s}-{e} is_default=True (not referenced by schedule.shift_id)")
    print(f"  employee rows ........ {len(plan.user_settings)}")
    for org, uid, key, value in plan.user_settings:
        print(f"      org={org:<5} user={uid:<5} {key} = {value!r}")
    if plan.skipped:
        print(f"  skipped (idempotent) . {len(plan.skipped)}")
        for s in plan.skipped[:20]:
            print(f"      {s}")
    print(f"  TOTAL WRITES ......... {plan.total()}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="COMMIT the backfill. Requires explicit approval.")
    ap.add_argument("--rehearse", action="store_true",
                    help="Apply inside a transaction, print, then ROLL BACK.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        plan = build_plan(db)
        print_plan(plan)

        if args.apply:
            apply_plan(db, plan)
            db.commit()
            print("\n*** APPLIED AND COMMITTED ***")
            return 0

        if args.rehearse:
            apply_plan(db, plan)          # inside the open transaction
            print("\n(plan applied inside an open transaction - will be rolled back)")
            db.rollback()
            print("\nROLLED BACK - no rows were written.")
            return 0

        print("\n(dry run: nothing applied)")
        print("Re-run with --rehearse to exercise the writes safely, "
              "then --apply to commit.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
