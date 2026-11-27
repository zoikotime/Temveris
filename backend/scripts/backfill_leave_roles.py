"""
Backfill the leave ROLE FLAGS for existing organizations.  (Phase 5, R1/R2/R3)

Before Phase 5 three leave rules identified their type by display name:

    "Annual Leave" -> shared-pool master
    "Unpaid Leave" -> loss of pay
    "Sick Leave"   -> HR-review frequency heuristic

Phase 5 made the role explicit metadata (``pool_master`` / ``is_unpaid`` /
``is_sick``). This script stamps the flags so every existing tenant keeps the
behaviour it has TODAY. It is the whole migration: the flags live in the
existing ``leave_policies:{org_id}`` AppSettings JSON, so there is no schema
change, no ALTER TABLE and nothing to roll back at the database level.

RULES
-----
* A role is stamped ONLY if that organization's effective catalogue actually
  contains the legacy type and the type is not disabled. A tenant with no
  "Unpaid Leave" is REPORTED as needing configuration — never given a
  substitute. Inventing one is exactly the guess this migration exists to
  avoid.
* If a role is already assigned (an admin got there first), it is left alone.
* Idempotent: running it twice changes nothing the second time.
* Reversible: ``--revert`` removes only the flags this script would set, and
  only where they still match the legacy name — an admin's later choice is
  never undone.

USAGE
-----
    python scripts/backfill_leave_roles.py --rehearse   # default, writes nothing
    python scripts/backfill_leave_roles.py --apply
    python scripts/backfill_leave_roles.py --revert
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.database import SessionLocal            # noqa: E402
from database import models                            # noqa: E402
from app.leave import identity as ident                # noqa: E402

LEGACY_FOR_ROLE = {
    "pool_master": ident.LEGACY_POOL_MASTER,
    "is_unpaid":   ident.LEGACY_UNPAID,
    "is_sick":     ident.LEGACY_SICK,
}


def _policy_key(org_id) -> str:
    return f"leave_policies:{org_id}" if org_id is not None else "leave_policies"


def _load_overrides(db, org_id) -> dict:
    row = (
        db.query(models.AppSettings)
        .filter(models.AppSettings.organization_id == org_id,
                models.AppSettings.key == _policy_key(org_id))
        .first()
    )
    if not row or not row.value:
        return {}
    try:
        return json.loads(row.value) or {}
    except Exception:
        return {}


def _save_overrides(db, org_id, data: dict) -> None:
    key = _policy_key(org_id)
    row = (
        db.query(models.AppSettings)
        .filter(models.AppSettings.organization_id == org_id,
                models.AppSettings.key == key)
        .first()
    )
    payload = json.dumps(data)
    if row:
        row.value = payload
    else:
        db.add(models.AppSettings(
            organization_id=org_id, key=key, value=payload,
            description="Leave policy overrides (JSON)",
        ))


def _effective(db, org_id) -> dict:
    from app.leave.router import _get_effective_policies
    return _get_effective_policies(db, org_id) or {}


def _disabled(db, org_id) -> set:
    from app.leave.router import _disabled_leave_types
    try:
        return _disabled_leave_types(db, org_id) or set()
    except Exception:
        return set()


def plan(db, org_id):
    """(assignments, already_set, needs_configuration) for one organization."""
    eff = _effective(db, org_id)
    off = _disabled(db, org_id)
    assignments, already, missing = {}, {}, []
    for role, legacy_name in LEGACY_FOR_ROLE.items():
        holder = ident._role_holder(eff, role)
        if holder is not None:
            already[role] = holder
            continue
        if legacy_name in eff and legacy_name not in off:
            assignments[role] = legacy_name
        else:
            missing.append(role)
    return assignments, already, missing


def run(apply: bool, revert: bool) -> int:
    db = SessionLocal()
    try:
        org_ids = [r[0] for r in db.query(models.Organization.id).all()]
        print(f"{len(org_ids)} organization(s)\n")
        needs_config = []
        changed = 0

        for org_id in org_ids:
            if revert:
                data = _load_overrides(db, org_id)
                removed = []
                for lt, pol in list(data.items()):
                    for role, legacy_name in LEGACY_FOR_ROLE.items():
                        # Only unset a flag this script would have set: same
                        # role, same legacy type. An admin's own choice stays.
                        if pol.get(role) and lt == legacy_name:
                            pol.pop(role, None)
                            removed.append(f"{lt}.{role}")
                if removed:
                    changed += 1
                    print(f"org {org_id}: revert {', '.join(removed)}")
                    if apply:
                        _save_overrides(db, org_id, data)
                continue

            assignments, already, missing = plan(db, org_id)
            if already:
                print(f"org {org_id}: already assigned "
                      + ", ".join(f"{r}={n}" for r, n in already.items()))
            if assignments:
                changed += 1
                print(f"org {org_id}: stamp "
                      + ", ".join(f"{n}.{r}=True" for r, n in assignments.items()))
                if apply:
                    data = _load_overrides(db, org_id)
                    for role, name in assignments.items():
                        entry = dict(data.get(name) or {})
                        entry[role] = True
                        data[name] = entry
                    _save_overrides(db, org_id, data)
            if missing:
                needs_config.append((org_id, missing))
                print(f"org {org_id}: NEEDS CONFIGURATION -> {', '.join(missing)} "
                      f"(no matching legacy type in this catalogue)")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nREHEARSAL — nothing written. Re-run with --apply.")

        print(f"\norganizations changed: {changed}")
        if needs_config:
            print(f"organizations needing an admin decision: {len(needs_config)}")
            for org_id, roles in needs_config:
                print(f"  org {org_id}: {', '.join(roles)}")
            print("\nThese tenants keep their CURRENT behaviour (the legacy name "
                  "still resolves the role) until an admin assigns it in "
                  "Leave Settings. Nothing was invented for them.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the changes")
    g.add_argument("--rehearse", action="store_true", help="default: write nothing")
    ap.add_argument("--revert", action="store_true",
                    help="remove flags this script set (legacy names only)")
    a = ap.parse_args()
    raise SystemExit(run(apply=a.apply, revert=a.revert))
