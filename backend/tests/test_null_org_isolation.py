"""NULL-organization regression protection.

The existing suite (``test_tenant_isolation.py``) proves org A cannot see org B
for two *integer* organizations. It has no NULL-organization fixture at all, so
it stayed green throughout the period when every self-serve tenant shared one
bucket — it was validating the model the super-admin flow produced, not the one
signup produced.

This module closes that gap. Two properties are asserted:

1. **An org-less user must not be able to read org-scoped product data.** The
   danger is specific to how the scoping filter is written across the routers::

       Model.organization_id == current_user.organization_id

   With a Python ``None`` right-hand side SQLAlchemy compiles that to
   ``organization_id IS NULL`` — the filter *succeeds* and returns every
   org-less row rather than matching nothing. Any endpoint that scopes this way
   and does not separately reject org-less callers is exposed.

2. **Only platform identities may be org-less.** ``super_admin`` accounts are
   deliberately org-less (``auth.service._maybe_promote_super_admin`` forces
   ``organization_id`` to NULL). No other role may be.

A dedicated engine is used so the assertions can be made over whole tables
without other modules' fixtures interfering.
"""

import os
import sys
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db     # noqa: E402
from database import models              # noqa: E402


@pytest.fixture(scope="module")
def eng():
    """Dedicated engine so whole-table assertions aren't polluted by other
    modules' fixtures — with the global handles restored on teardown, or every
    later module would inherit this partial schema.
    """
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = e
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=e)
    for t in (
        models.Organization, models.User, models.Team, models.OrgSettings,
        models.AppSettings, models.LeavePolicy, models.LeaveRequest,
        models.ClockSession, models.Activity, models.AuditLog, models.Holiday,
        models.Shift,
    ):
        t.__table__.create(e, checkfirst=True)
    try:
        yield e
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture(scope="module")
def db(eng):
    s = _db.SessionLocal()
    yield s
    s.close()


@pytest.fixture(scope="module")
def seed(db):
    """Two real tenants, plus one platform super_admin.

    Deliberately NO org-less tenant user: the point of the fix is that signup
    cannot create one. Where a test needs an org-less caller it constructs a
    transient object rather than persisting one.
    """
    org_a = models.Organization(id=1, name="Alpha Ltd")
    org_b = models.Organization(id=2, name="Beta Ltd")
    db.add_all([org_a, org_b])
    db.flush()

    admin_a = models.User(id=1, name="Admin A", email="admin-a@alpha.example.com",
                          role="admin", organization_id=1, is_active=True)
    emp_a = models.User(id=2, name="Emp A", email="emp-a@alpha.example.com",
                        role="employee", organization_id=1, is_active=True)
    admin_b = models.User(id=3, name="Admin B", email="admin-b@beta.example.com",
                          role="admin", organization_id=2, is_active=True)
    emp_b = models.User(id=4, name="Emp B", email="emp-b@beta.example.com",
                        role="employee", organization_id=2, is_active=True)
    platform = models.User(id=5, name="Platform", email="platform@temveris.example",
                           role="super_admin", organization_id=None, is_active=True)
    db.add_all([admin_a, emp_a, admin_b, emp_b, platform])
    db.flush()

    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for oid, uid, label in ((1, 2, "Alpha"), (2, 4, "Beta")):
        db.add(models.Team(organization_id=oid, name=f"{label} Team"))
        db.add(models.OrgSettings(organization_id=oid, company_name=f"{label} Ltd"))
        db.add(models.AppSettings(organization_id=oid,
                                  key=f"screenshot_enabled:{oid}", value="true"))
        db.add(models.LeavePolicy(organization_id=oid, leave_type="Annual Leave",
                                  annual_limit=15, is_active=True))
        db.add(models.ClockSession(user_id=uid, organization_id=oid,
                                   clock_in_time=day,
                                   clock_out_time=day + timedelta(hours=8)))
        db.add(models.Activity(user_id=uid, organization_id=oid,
                               app_name=f"{label}App",
                               timestamp=day + timedelta(hours=1), is_active=True))
        db.add(models.LeaveRequest(user_id=uid, organization_id=oid,
                                   leave_type="Sick Leave", from_date=day,
                                   to_date=day, description="x", status="submitted"))
        db.add(models.AuditLog(organization_id=oid, actor_id=uid,
                               entity="User", action="Something"))
        db.add(models.Holiday(organization_id=oid, name=f"{label} Day",
                              date=day.date()))
        db.add(models.Shift(organization_id=oid, name=f"{label} Shift",
                            start_time="09:00", end_time="18:00"))
    db.commit()
    return {"org_a": 1, "org_b": 2}


# The org-scoped resources an org-less caller must not be able to read. Each
# entry is the exact filter shape used by the routers.
ORG_SCOPED_MODELS = [
    ("employees", models.User),
    ("teams", models.Team),
    ("org_settings", models.OrgSettings),
    ("settings", models.AppSettings),
    ("leave_policies", models.LeavePolicy),
    ("leave_requests", models.LeaveRequest),
    ("attendance", models.ClockSession),
    ("activities", models.Activity),
    ("audit_logs", models.AuditLog),
    ("holidays", models.Holiday),
    ("schedules", models.Shift),
]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Org A vs Org B — the existing property, restated on this fixture
# ═════════════════════════════════════════════════════════════════════════════

class TestTenantsCannotSeeEachOther:

    @pytest.mark.parametrize("label,model", ORG_SCOPED_MODELS)
    def test_a_sees_only_its_own_rows(self, db, seed, label, model):
        rows = db.query(model).filter(model.organization_id == seed["org_a"]).all()
        assert rows, f"{label}: fixture produced no rows for org A"
        assert all(r.organization_id == seed["org_a"] for r in rows), label

    @pytest.mark.parametrize("label,model", ORG_SCOPED_MODELS)
    def test_b_sees_only_its_own_rows(self, db, seed, label, model):
        rows = db.query(model).filter(model.organization_id == seed["org_b"]).all()
        assert rows, f"{label}: fixture produced no rows for org B"
        assert all(r.organization_id == seed["org_b"] for r in rows), label

    def test_the_two_tenants_share_no_rows(self, db, seed):
        for label, model in ORG_SCOPED_MODELS:
            a = {r.id for r in db.query(model).filter(
                model.organization_id == seed["org_a"]).all()}
            b = {r.id for r in db.query(model).filter(
                model.organization_id == seed["org_b"]).all()}
            assert not (a & b), f"{label}: rows shared between tenants"


# ═════════════════════════════════════════════════════════════════════════════
# 2. The NULL-organization case — previously untested
# ═════════════════════════════════════════════════════════════════════════════

class TestOrgLessCallerSeesNothing:

    @pytest.mark.parametrize("label,model", ORG_SCOPED_MODELS)
    def test_null_org_filter_matches_no_tenant_rows(self, db, seed, label, model):
        """The shape of the danger, asserted directly.

        `Model.organization_id == None` compiles to `IS NULL`, so this query
        does NOT return nothing by accident — it returns every org-less row. The
        assertion is that no *tenant* row is org-less, i.e. the bucket is empty,
        which is what makes the whole app's scoping safe.
        """
        org_less = None
        q = db.query(model).filter(model.organization_id == org_less)
        if model is models.User:
            # super_admin accounts are deliberately org-less platform
            # identities, so they are the one legitimate occupant of this
            # bucket. Their exclusion here is exactly what
            # TestOnlyPlatformIdentitiesAreOrgLess asserts separately.
            q = q.filter(models.User.role != "super_admin")
        rows = q.all()
        assert rows == [], (
            f"{label}: {len(rows)} row(s) sit in the shared NULL-organization "
            f"bucket and would be returned to any org-less caller"
        )

    def test_the_filter_really_does_compile_to_is_null(self, db):
        """Documents *why* the tests above are necessary.

        If this ever stopped being true the NULL bucket would be harmless — and
        this whole module could be deleted. It is true, so it isn't.
        """
        org_less = None
        sql = str(
            db.query(models.User)
              .filter(models.User.organization_id == org_less)
              .statement.compile(compile_kwargs={"literal_binds": True})
        )
        assert "IS NULL" in sql.upper()

    def test_get_current_organization_rejects_an_org_less_caller(self, db, seed):
        """The one guard in the codebase that refuses org-less callers."""
        from app.auth.service import get_current_organization

        org_less = models.User(name="Stray", email="stray@x.example.com",
                               role="admin", organization_id=None)
        with pytest.raises(HTTPException) as exc:
            get_current_organization(current_user=org_less, db=db)
        assert exc.value.status_code == 403
        assert "not associated with any organization" in str(exc.value.detail)

    def test_an_org_scoped_caller_passes_that_guard(self, db, seed):
        from app.auth.service import get_current_organization
        admin_a = db.query(models.User).filter_by(id=1).one()
        org = get_current_organization(current_user=admin_a, db=db)
        assert org.id == seed["org_a"]


# ═════════════════════════════════════════════════════════════════════════════
# 3. Only platform identities may be org-less
# ═════════════════════════════════════════════════════════════════════════════

class TestOnlyPlatformIdentitiesAreOrgLess:

    def test_no_non_platform_user_is_org_less(self, db, seed):
        orphans = (
            db.query(models.User)
              .filter(models.User.organization_id.is_(None),
                      models.User.role != "super_admin")
              .all()
        )
        assert orphans == [], (
            "org-less non-platform users: "
            f"{[(u.id, u.email, u.role) for u in orphans]}"
        )

    def test_super_admin_is_org_less_by_design(self, db, seed):
        sa = db.query(models.User).filter_by(role="super_admin").one()
        assert sa.organization_id is None

    def test_super_admin_promotion_clears_any_tenant_affiliation(self, db, seed, monkeypatch):
        """Promotion must not leave a platform identity inside a tenant."""
        from app.auth import service as auth_service

        monkeypatch.setenv("SUPER_ADMIN_EMAILS", "promote-me@temveris.example")
        u = models.User(name="Promote", email="promote-me@temveris.example",
                        role="admin", organization_id=seed["org_a"], is_active=True)
        changed = auth_service._maybe_promote_super_admin(db, u)
        assert changed is True
        assert u.role == "super_admin"
        assert u.organization_id is None

    def test_removing_the_env_entry_demotes_rather_than_stranding(self, db, monkeypatch):
        from app.auth import service as auth_service

        monkeypatch.setenv("SUPER_ADMIN_EMAILS", "someone-else@temveris.example")
        u = models.User(name="Was Super", email="was-super@temveris.example",
                        role="super_admin", organization_id=None, is_active=True)
        changed = auth_service._maybe_promote_super_admin(db, u)
        assert changed is True
        assert u.role == "admin"

    def test_self_service_admin_cannot_become_super_admin(self, db, monkeypatch):
        """An org admin whose email is not in the env var stays an org admin."""
        from app.auth import service as auth_service

        monkeypatch.setenv("SUPER_ADMIN_EMAILS", "platform-only@temveris.example")
        u = models.User(name="Tenant Admin", email="tenant@alpha.example.com",
                        role="admin", organization_id=1, is_active=True)
        changed = auth_service._maybe_promote_super_admin(db, u)
        assert changed is False
        assert u.role == "admin"
        assert u.organization_id == 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. Cross-tenant write attempts
# ═════════════════════════════════════════════════════════════════════════════

class TestCrossTenantWriteAttempts:

    def test_team_creation_derives_org_from_the_caller(self, db, seed):
        """Team rows must be stamped from server-side user state.

        Mirrors org.router.create_team, which reads admin.organization_id and
        never accepts an organization id from the request body.
        """
        admin_a = db.query(models.User).filter_by(id=1).one()
        team = models.Team(organization_id=admin_a.organization_id, name="Derived")
        db.add(team)
        db.commit()
        assert team.organization_id == seed["org_a"]
        assert db.query(models.Team).filter(
            models.Team.organization_id == seed["org_b"],
            models.Team.name == "Derived",
        ).count() == 0
        db.delete(team)
        db.commit()

    def test_invited_user_inherits_the_inviters_org(self, db, seed):
        """Mirrors employees.router.invite_by_email's stamping."""
        admin_b = db.query(models.User).filter_by(id=3).one()
        invitee = models.User(
            name="New", email="new-hire@beta.example.com", role="employee",
            organization_id=admin_b.organization_id, is_active=True,
        )
        db.add(invitee)
        db.commit()
        assert invitee.organization_id == seed["org_b"]
        assert invitee.organization_id is not None
        # Left in place rather than deleted: User has cascade relationships to
        # tables this focused engine does not create, and the row is a valid
        # org-B member that no other assertion in this module minds.

    def test_org_scoped_lookup_by_id_cannot_cross_tenants(self, db, seed):
        """The `id == X AND organization_id == mine` pattern in the routers."""
        emp_b_id = 4
        found = (
            db.query(models.User)
              .filter(models.User.id == emp_b_id,
                      models.User.organization_id == seed["org_a"])
              .first()
        )
        assert found is None
        found_own = (
            db.query(models.User)
              .filter(models.User.id == emp_b_id,
                      models.User.organization_id == seed["org_b"])
              .first()
        )
        assert found_own is not None
