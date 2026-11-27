"""Verdict logic of the orphan self-service tenant inventory.

The inventory decides whether historical NULL-organization data can be repaired
automatically. Getting that decision wrong in the permissive direction would
merge two customers' data, so the AMBIGUOUS branch is the important test here.

The script itself is read-only; these tests exercise ``collect()`` against a
throwaway in-memory database.
"""

import os
import sys
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db      # noqa: E402
from database import models               # noqa: E402
from app.commercial import models as cm   # noqa: E402


@pytest.fixture
def inv_env(monkeypatch):
    """Fresh engine per test — the verdict depends on whole-table counts."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    for t in (
        models.Organization, models.User, models.Team, models.OrgSettings,
        models.AppSettings, models.LeavePolicy, models.Holiday,
        models.ClockSession, models.BreakSession, models.Activity,
        models.LeaveRequest,
        cm.CommercialOrganization,
    ):
        t.__table__.create(eng, checkfirst=True)

    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False)

    import scripts.inventory_orphan_self_service_tenants as inv
    # The script resolves table existence against the module-level engine.
    monkeypatch.setattr(inv, "engine", eng)

    s = Session()
    try:
        yield inv, s
    finally:
        s.rollback()
        s.close()


def _orphan(db, *, cid, name, owner_email):
    owner = models.User(
        name=name, email=owner_email, role="admin",
        organization_id=None, is_active=True, is_password_set=True,
    )
    db.add(owner)
    db.flush()
    db.add(cm.CommercialOrganization(
        id=cid, name=name, owner_user_id=owner.id,
        operational_org_id=None, commercial_state="trial_active",
    ))
    db.flush()
    return owner


def test_clean_database_reports_no_orphans(inv_env):
    inv, db = inv_env
    report = inv.collect(db)
    assert report["verdict"] == "NO_ORPHANS"
    assert report["orphan_tenants"] == []


def test_single_orphan_is_deterministic_even_with_shared_rows(inv_env):
    """One orphan means org-only NULL rows can only belong to it."""
    inv, db = inv_env
    _orphan(db, cid="org_solo", name="Solo Ltd", owner_email="solo@ex.com")
    db.add(models.Team(organization_id=None, name="Engineering"))
    db.add(models.OrgSettings(organization_id=None, company_name="Solo Ltd"))
    db.commit()

    report = inv.collect(db)
    assert report["verdict"] == "DETERMINISTIC"
    assert len(report["orphan_tenants"]) == 1
    assert report["shared_bucket_rows"]["teams"] == 1


def test_multiple_orphans_without_shared_rows_is_deterministic(inv_env):
    inv, db = inv_env
    _orphan(db, cid="org_a", name="A Ltd", owner_email="a@ex.com")
    _orphan(db, cid="org_b", name="B Ltd", owner_email="b@ex.com")
    db.commit()

    report = inv.collect(db)
    assert report["verdict"] == "DETERMINISTIC"
    assert len(report["orphan_tenants"]) == 2
    assert sum(
        v for v in report["shared_bucket_rows"].values() if isinstance(v, int)
    ) == 0


def test_multiple_orphans_with_shared_rows_is_ambiguous(inv_env):
    """The case that must never be auto-repaired.

    Two orphan tenants both wrote into the NULL bucket. A team row carries no
    user reference, so nothing in the data says which customer it belongs to.
    """
    inv, db = inv_env
    _orphan(db, cid="org_x", name="X Ltd", owner_email="x@ex.com")
    _orphan(db, cid="org_y", name="Y Ltd", owner_email="y@ex.com")
    db.add(models.Team(organization_id=None, name="Shared Team"))
    db.commit()

    report = inv.collect(db)
    assert report["verdict"] == "AMBIGUOUS"
    joined = " ".join(report["notes"]).lower()
    assert "cannot be attributed" in joined
    assert "do not auto-repair" in joined or "not auto-repair" in joined


def test_platform_super_admins_are_not_counted_as_orphan_users(inv_env):
    """Super-admin accounts are intentionally org-less."""
    inv, db = inv_env
    db.add(models.User(
        name="Platform", email="platform@ex.com", role="super_admin",
        organization_id=None, is_active=True,
    ))
    db.commit()

    report = inv.collect(db)
    assert report["null_org_users_platform"] == 1
    assert report["null_org_users_non_platform"] == 0
    assert report["verdict"] == "NO_ORPHANS"


def test_non_platform_org_less_users_are_flagged(inv_env):
    inv, db = inv_env
    db.add(models.User(
        name="Stray", email="stray@ex.com", role="employee",
        organization_id=None, is_active=True,
    ))
    db.commit()

    report = inv.collect(db)
    assert report["null_org_users_non_platform"] == 1
    assert any("org-less" in n for n in report["notes"])


def test_user_scoped_rows_are_reported_separately(inv_env):
    """Rows reachable from a user id are attributable; org-only rows are not.

    The distinction is what the verdict turns on, so the report must keep them
    in separate buckets.
    """
    from datetime import datetime, timezone

    inv, db = inv_env
    owner = _orphan(db, cid="org_z", name="Z Ltd", owner_email="z@ex.com")
    db.add(models.ClockSession(
        user_id=owner.id, organization_id=None,
        clock_in_time=datetime.now(timezone.utc),
    ))
    db.commit()

    report = inv.collect(db)
    assert report["user_scoped_null_rows"]["clock_sessions"] == 1
    assert report["shared_bucket_rows"].get("teams", 0) == 0


def test_collect_performs_no_writes(inv_env):
    """A read-only tool must stay read-only."""
    inv, db = inv_env
    _orphan(db, cid="org_ro", name="RO Ltd", owner_email="ro@ex.com")
    db.commit()

    before = (
        db.query(models.User).count(),
        db.query(models.Organization).count(),
        db.query(cm.CommercialOrganization).count(),
        db.query(models.Team).count(),
    )
    inv.collect(db)
    db.rollback()
    after = (
        db.query(models.User).count(),
        db.query(models.Organization).count(),
        db.query(cm.CommercialOrganization).count(),
        db.query(models.Team).count(),
    )
    assert before == after
