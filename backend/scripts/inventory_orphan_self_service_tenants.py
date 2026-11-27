"""
Orphan self-service tenant inventory  —  STRICTLY READ-ONLY.

Purpose
=======
Self-service signup used to create a ``CommercialOrganization`` without an
operational ``Organization``, leaving ``users.organization_id`` NULL and
``commercial_organizations.operational_org_id`` NULL. Every such tenant landed in
the shared NULL-organization bucket, where an org-scoped read compiled to
``organization_id IS NULL`` and returned the *other* tenants' rows too.

The signup path is fixed. This script answers the question that has to be
answered BEFORE any repair migration is written:

    For each orphan tenant, can its operational rows be attributed to it
    DETERMINISTICALLY — or are they ambiguous because several orphan tenants
    shared the NULL bucket?

Where several orphan tenants coexisted, rows that are not reachable from a
specific user id (team rows, org-level settings, holidays, org-scoped audit
rows) genuinely cannot be attributed by inspection. This script says so
explicitly rather than guessing.

It performs NO writes, NO commits, NO DDL and creates NO rows.

How to run
==========
    cd backend
    python scripts/inventory_orphan_self_service_tenants.py
    python scripts/inventory_orphan_self_service_tenants.py --json
    DATABASE_URL=... python scripts/inventory_orphan_self_service_tenants.py

Exit codes
==========
    0  no orphan tenants — nothing to repair
    1  orphan tenants found, ALL deterministically attributable
    2  orphan tenants found, at least one AMBIGUOUS — do NOT auto-repair
    3  could not run (connection/schema problem)

Reading the output
==================
``DETERMINISTIC`` means exactly one orphan tenant exists, or the orphan's rows
are all reachable from its own user ids. A repair can then create one
``Organization`` per orphan and re-point those rows.

``AMBIGUOUS`` means shared-bucket rows exist that no rule can assign. Per the
implementation constraints these must be left alone and reviewed by a human;
the repair migration must skip them rather than merge organizations.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections import OrderedDict

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(override=False)

from sqlalchemy import func, inspect as sa_inspect  # noqa: E402

from database.database import SessionLocal, engine  # noqa: E402
from database import models  # noqa: E402


# Operational tables that are org-scoped AND reachable from a user id. Rows here
# can be attributed to whichever orphan tenant owns the user.
USER_SCOPED_TABLES = OrderedDict([
    ("clock_sessions", (models.ClockSession, "user_id")),
    ("break_sessions", (models.BreakSession, "user_id")),
    ("activities", (models.Activity, "user_id")),
    ("leave_requests", (models.LeaveRequest, "user_id")),
])

# Org-scoped tables with NO user column. If more than one orphan tenant exists,
# rows here cannot be attributed by inspection.
ORG_ONLY_TABLES = OrderedDict([
    ("teams", models.Team),
    ("org_settings", models.OrgSettings),
    ("app_settings", models.AppSettings),
    ("leave_policies", models.LeavePolicy),
    ("holidays", models.Holiday),
])


def _table_exists(name: str) -> bool:
    try:
        return name in sa_inspect(engine).get_table_names()
    except Exception:
        return False


def _count_null_org(db, model) -> int:
    return db.query(func.count(model.id)).filter(
        model.organization_id.is_(None)
    ).scalar() or 0


def collect(db) -> dict:
    cm = None
    try:
        from app.commercial import models as _cm
        cm = _cm
    except Exception:
        pass

    report: dict = {
        "orphan_tenants": [],
        "null_org_users_total": 0,
        "null_org_users_platform": 0,
        "null_org_users_non_platform": 0,
        "shared_bucket_rows": {},
        "user_scoped_null_rows": {},
        "verdict": "NO_ORPHANS",
        "notes": [],
    }

    # ── Orphan commercial organizations ──────────────────────────────────
    if cm is None or not _table_exists("commercial_organizations"):
        report["notes"].append(
            "commercial_organizations table not present — nothing to inventory."
        )
        return report

    orphans = (
        db.query(cm.CommercialOrganization)
          .filter(cm.CommercialOrganization.operational_org_id.is_(None))
          .order_by(cm.CommercialOrganization.created_at.asc())
          .all()
    )

    for o in orphans:
        owner = (
            db.query(models.User).filter(models.User.id == o.owner_user_id).first()
            if o.owner_user_id else None
        )
        report["orphan_tenants"].append({
            "commercial_org_id": o.id,
            "name": o.name,
            "commercial_state": o.commercial_state,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "owner_user_id": o.owner_user_id,
            "owner_email": owner.email if owner else None,
            "owner_exists": owner is not None,
            "owner_organization_id": owner.organization_id if owner else None,
            "owner_is_org_less": bool(owner and owner.organization_id is None),
        })

    # ── Org-less users ───────────────────────────────────────────────────
    null_users = db.query(models.User).filter(models.User.organization_id.is_(None)).all()
    report["null_org_users_total"] = len(null_users)
    platform = [u for u in null_users if (u.role or "") == "super_admin"]
    non_platform = [u for u in null_users if (u.role or "") != "super_admin"]
    report["null_org_users_platform"] = len(platform)
    report["null_org_users_non_platform"] = len(non_platform)
    report["null_org_user_sample"] = [
        {"id": u.id, "email": u.email, "role": u.role} for u in non_platform[:25]
    ]

    # ── NULL-org operational rows ────────────────────────────────────────
    for name, model in ORG_ONLY_TABLES.items():
        if not _table_exists(name):
            continue
        try:
            report["shared_bucket_rows"][name] = _count_null_org(db, model)
        except Exception as e:
            report["shared_bucket_rows"][name] = f"error: {e}"

    for name, (model, _user_col) in USER_SCOPED_TABLES.items():
        if not _table_exists(name):
            continue
        try:
            report["user_scoped_null_rows"][name] = _count_null_org(db, model)
        except Exception as e:
            report["user_scoped_null_rows"][name] = f"error: {e}"

    # ── Verdict ──────────────────────────────────────────────────────────
    orphan_count = len(orphans)
    shared_rows = sum(
        v for v in report["shared_bucket_rows"].values() if isinstance(v, int)
    )

    if orphan_count == 0 and report["null_org_users_non_platform"] == 0:
        report["verdict"] = "NO_ORPHANS"
        report["notes"].append("Nothing to repair.")
        return report

    if orphan_count <= 1 and shared_rows == 0:
        report["verdict"] = "DETERMINISTIC"
        report["notes"].append(
            "At most one orphan tenant and no org-only NULL rows — a repair can "
            "attribute everything without guessing."
        )
    elif orphan_count <= 1:
        report["verdict"] = "DETERMINISTIC"
        report["notes"].append(
            f"Exactly {orphan_count} orphan tenant, so the {shared_rows} org-only "
            "NULL row(s) can only belong to it. Attribution is unambiguous."
        )
    else:
        if shared_rows == 0:
            report["verdict"] = "DETERMINISTIC"
            report["notes"].append(
                f"{orphan_count} orphan tenants, but no org-only NULL rows. Each "
                "tenant's rows are reachable from its own user ids."
            )
        else:
            report["verdict"] = "AMBIGUOUS"
            report["notes"].append(
                f"{orphan_count} orphan tenants share {shared_rows} org-only NULL "
                "row(s) that carry no user reference. These CANNOT be attributed "
                "by inspection. Do NOT auto-repair them — leave them NULL for "
                "human review, and never merge organizations to make them fit."
            )

    if report["null_org_users_non_platform"]:
        report["notes"].append(
            f"{report['null_org_users_non_platform']} non-platform user(s) are "
            "org-less. Only super_admin identities are intentionally org-less."
        )
    return report


def render(report: dict) -> None:
    print()
    print("=" * 78)
    print(" ORPHAN SELF-SERVICE TENANT INVENTORY  (read-only)")
    print("=" * 78)

    orphans = report["orphan_tenants"]
    print(f"\nOrphan commercial organizations (operational_org_id IS NULL): {len(orphans)}")
    for o in orphans:
        print(f"  - {o['commercial_org_id']}  {o['name']!r}")
        print(f"      state={o['commercial_state']}  created={o['created_at']}")
        print(f"      owner_user_id={o['owner_user_id']}  email={o['owner_email']}")
        print(f"      owner_exists={o['owner_exists']}  owner_org_less={o['owner_is_org_less']}")

    print(f"\nOrg-less users: {report['null_org_users_total']}"
          f"  (platform/super_admin: {report['null_org_users_platform']},"
          f"  non-platform: {report['null_org_users_non_platform']})")
    for u in report.get("null_org_user_sample", []):
        print(f"  - id={u['id']} {u['email']} role={u['role']}")

    print("\nNULL-organization rows in ORG-ONLY tables (no user reference):")
    if not report["shared_bucket_rows"]:
        print("  (none inspected)")
    for k, v in report["shared_bucket_rows"].items():
        print(f"  {k:<20} {v}")

    print("\nNULL-organization rows in USER-SCOPED tables (attributable via user):")
    if not report["user_scoped_null_rows"]:
        print("  (none inspected)")
    for k, v in report["user_scoped_null_rows"].items():
        print(f"  {k:<20} {v}")

    print(f"\nVERDICT: {report['verdict']}")
    for n in report["notes"]:
        print(f"  * {n}")

    if report["verdict"] == "AMBIGUOUS":
        print("\n  ACTION: do NOT write an automated repair for the ambiguous rows.")
        print("  Repair only what is deterministically attributable, and escalate")
        print("  the remainder for human review.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    db = None
    try:
        db = SessionLocal()
        report = collect(db)
    except Exception as e:
        print(f"ERROR: could not run inventory: {e}", file=sys.stderr)
        return 3
    finally:
        if db is not None:
            # Read-only: rollback rather than commit, so nothing can escape even
            # if a future edit accidentally introduces a write.
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        render(report)

    return {
        "NO_ORPHANS": 0,
        "DETERMINISTIC": 1,
        "AMBIGUOUS": 2,
    }.get(report["verdict"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
