"""Commercial tenant resolution + entitlement enforcement.

Two related changes are covered here.

**Resolution** (app.commercial.tenant_resolver) — the commercial layer used to
answer "which organization is this user's?" by matching
``CommercialOrganization.owner_user_id == user.id``. That only ever worked for
the person who signed up; an invited employee owned nothing, so they resolved to
``None`` and fell through to a synthetic ``"default"`` entitlement profile. Any
seat or feature enforcement built on it would have applied to exactly one user
per organization.

**Enforcement** (app.commercial.enforcement) — the resolver has always computed
correct limits, but nothing called ``can()`` / ``within_limit()``. The critical
constraint when switching enforcement on is backward compatibility:
super-admin-provisioned organizations have no ``CommercialSubscription``, so
``entitlements.compute()`` returns an all-zero-limits fallback for them. Naive
enforcement would have told every existing customer they were out of seats.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import os
import sys
import pathlib

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db          # noqa: E402
from database import models                   # noqa: E402
from app.commercial import models as cm       # noqa: E402
from app.commercial.tenant_resolver import (  # noqa: E402
    resolve_commercial_org, resolve_commercial_org_id,
)
from app.commercial.enforcement import (      # noqa: E402
    check_seat_limit, check_team_limit, seat_limit_status,
)


@pytest.fixture(scope="module")
def eng():
    """Focused engine, with the global handles restored on teardown.

    `database.database.engine` / `SessionLocal` are resolved at call time by the
    app code, so they must point here for the duration — and be put back
    afterwards, or later modules inherit an engine that lacks most tables.
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
        cm.CommercialOrganization, cm.CommercialSubscription, cm.EntitlementsCache,
        cm.EmailVerificationToken,
    ):
        t.__table__.create(e, checkfirst=True)
    try:
        yield e
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def db(eng):
    s = _db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _tenant(db, *, name, plan=None, with_subscription=True, seat_limit_plan="verified"):
    """Build an operational org + commercial org (+ optional subscription)."""
    org = models.Organization(name=name)
    db.add(org)
    db.flush()

    owner = models.User(
        name=f"{name} Owner", email=f"owner-{org.id}@ex.com",
        role="admin", organization_id=org.id, is_active=True, is_password_set=True,
    )
    db.add(owner)
    db.flush()

    comm = cm.CommercialOrganization(
        id=f"org_{name.lower().replace(' ', '')}{org.id}",
        name=name, owner_user_id=owner.id, operational_org_id=org.id,
        commercial_state="trial_active" if with_subscription else "active",
    )
    db.add(comm)
    db.flush()

    if with_subscription:
        db.add(cm.CommercialSubscription(
            id=f"sub_{org.id}", organization_id=comm.id,
            plan_code=plan or seat_limit_plan, billing_cycle="monthly",
            seat_count=1, state="trial_active", provider="stub",
        ))
        db.flush()

    db.commit()
    return org, owner, comm


# ═════════════════════════════════════════════════════════════════════════════
# Resolution
# ═════════════════════════════════════════════════════════════════════════════

class TestTenantResolution:

    def test_owner_resolves_via_operational_link(self, db):
        org, owner, comm = _tenant(db, name="Resolve Owner")
        assert resolve_commercial_org(db, owner).id == comm.id
        assert resolve_commercial_org_id(db, owner) == comm.id

    def test_invited_employee_resolves_to_the_same_org(self, db):
        """The regression: an employee owns nothing and used to resolve to None."""
        org, owner, comm = _tenant(db, name="Resolve Employee")
        employee = models.User(
            name="Emp", email=f"emp-{org.id}@ex.com", role="employee",
            organization_id=org.id, is_active=True, is_password_set=True,
        )
        db.add(employee)
        db.commit()

        assert employee.id != comm.owner_user_id
        resolved = resolve_commercial_org(db, employee)
        assert resolved is not None, "invited employee resolved to no organization"
        assert resolved.id == comm.id

    def test_members_of_different_tenants_resolve_differently(self, db):
        org_a, owner_a, comm_a = _tenant(db, name="Resolve A")
        org_b, owner_b, comm_b = _tenant(db, name="Resolve B")
        assert resolve_commercial_org(db, owner_a).id == comm_a.id
        assert resolve_commercial_org(db, owner_b).id == comm_b.id
        assert comm_a.id != comm_b.id

    def test_org_less_user_resolves_to_nothing(self, db):
        """A platform identity must not inherit some arbitrary tenant."""
        sa = models.User(
            name="Platform", email="platform-res@ex.com", role="super_admin",
            organization_id=None, is_active=True,
        )
        db.add(sa)
        db.commit()
        assert resolve_commercial_org(db, sa) is None

    def test_legacy_unlinked_org_still_resolves_for_its_owner(self, db):
        """Self-serve orgs provisioned before operational_org_id existed."""
        legacy_owner = models.User(
            name="Legacy", email="legacy@ex.com", role="admin",
            organization_id=None, is_active=True, is_password_set=True,
        )
        db.add(legacy_owner)
        db.flush()
        comm = cm.CommercialOrganization(
            id="org_legacyunlinked", name="Legacy Ltd",
            owner_user_id=legacy_owner.id, operational_org_id=None,
            commercial_state="trial_active",
        )
        db.add(comm)
        db.commit()

        assert resolve_commercial_org(db, legacy_owner).id == "org_legacyunlinked"
        # …and the fallback can be switched off explicitly.
        assert resolve_commercial_org(
            db, legacy_owner, allow_owner_fallback=False
        ) is None

    def test_fallback_cannot_widen_access_to_a_non_owner(self, db):
        """The ownership fallback matches only the owner's own row."""
        legacy_owner = models.User(
            name="LegacyOwner2", email="legacy2@ex.com", role="admin",
            organization_id=None, is_active=True, is_password_set=True,
        )
        stranger = models.User(
            name="Stranger", email="stranger@ex.com", role="admin",
            organization_id=None, is_active=True, is_password_set=True,
        )
        db.add_all([legacy_owner, stranger])
        db.flush()
        db.add(cm.CommercialOrganization(
            id="org_legacy2", name="Legacy Two",
            owner_user_id=legacy_owner.id, operational_org_id=None,
            commercial_state="trial_active",
        ))
        db.commit()

        assert resolve_commercial_org(db, stranger) is None


# ═════════════════════════════════════════════════════════════════════════════
# Seat enforcement — backward compatibility is the critical property
# ═════════════════════════════════════════════════════════════════════════════

class TestSeatEnforcementBackwardCompatibility:

    def test_super_admin_provisioned_tenant_is_never_blocked(self, db):
        """The regression naive enforcement would have caused.

        Super-admin-provisioned orgs have no CommercialSubscription, so
        entitlements.compute() returns its fallback profile in which every limit
        is 0. Enforcing that literally would block all invitations for every
        existing customer.
        """
        org, owner, comm = _tenant(
            db, name="Contract Customer", with_subscription=False
        )
        # Plenty of members — far past any plan's seat limit.
        for i in range(40):
            db.add(models.User(
                name=f"E{i}", email=f"contract-{org.id}-{i}@ex.com",
                role="employee", organization_id=org.id, is_active=True,
            ))
        db.commit()

        # Must not raise.
        check_seat_limit(db, owner, adding=1)

        cap, used = seat_limit_status(db, owner)
        assert cap is None, "an unresolvable tenant must report no enforceable cap"
        assert used >= 40

    def test_org_less_user_is_not_blocked_by_seat_logic(self, db):
        sa = models.User(
            name="Platform2", email="platform-seat@ex.com", role="super_admin",
            organization_id=None, is_active=True,
        )
        db.add(sa)
        db.commit()
        check_seat_limit(db, sa, adding=1)   # must not raise

    def test_unlimited_plan_is_not_blocked(self, db):
        org, owner, comm = _tenant(db, name="Sovereign Co", plan="sovereign")
        for i in range(30):
            db.add(models.User(
                name=f"S{i}", email=f"sov-{org.id}-{i}@ex.com",
                role="employee", organization_id=org.id, is_active=True,
            ))
        db.commit()
        check_seat_limit(db, owner, adding=1)   # seat_limit is None


class TestSeatEnforcementIsReal:

    def test_within_limit_is_allowed(self, db):
        org, owner, comm = _tenant(db, name="Verified Small", plan="verified")
        check_seat_limit(db, owner, adding=1)   # 1 used, cap 25

        cap, used = seat_limit_status(db, owner)
        assert cap == 25
        assert used == 1

    def test_exceeding_the_limit_is_rejected(self, db):
        org, owner, comm = _tenant(db, name="Verified Full", plan="verified")
        # Fill to the cap of 25 (owner already counts as 1).
        for i in range(24):
            db.add(models.User(
                name=f"F{i}", email=f"full-{org.id}-{i}@ex.com",
                role="employee", organization_id=org.id, is_active=True,
            ))
        db.commit()

        cap, used = seat_limit_status(db, owner)
        assert (cap, used) == (25, 25)

        with pytest.raises(HTTPException) as exc:
            check_seat_limit(db, owner, adding=1)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "SEAT_OVER_ALLOCATION"
        assert exc.value.detail["seat_limit"] == 25
        assert exc.value.detail["seats_used"] == 25

    def test_inactive_members_do_not_consume_seats(self, db):
        org, owner, comm = _tenant(db, name="Verified Offboarded", plan="verified")
        for i in range(30):
            db.add(models.User(
                name=f"X{i}", email=f"off-{org.id}-{i}@ex.com",
                role="employee", organization_id=org.id, is_active=False,
            ))
        db.commit()
        cap, used = seat_limit_status(db, owner)
        assert cap == 25
        assert used == 1        # only the active owner
        check_seat_limit(db, owner, adding=1)

    def test_seat_usage_is_tenant_scoped(self, db):
        """One tenant's headcount must not consume another's seats."""
        org_a, owner_a, _ = _tenant(db, name="Seats A", plan="verified")
        org_b, owner_b, _ = _tenant(db, name="Seats B", plan="verified")
        for i in range(20):
            db.add(models.User(
                name=f"B{i}", email=f"seatsb-{org_b.id}-{i}@ex.com",
                role="employee", organization_id=org_b.id, is_active=True,
            ))
        db.commit()

        cap_a, used_a = seat_limit_status(db, owner_a)
        cap_b, used_b = seat_limit_status(db, owner_b)
        assert used_a == 1
        assert used_b == 21
        check_seat_limit(db, owner_a, adding=1)


# ═════════════════════════════════════════════════════════════════════════════
# Team limit — wired, unlimited today
# ═════════════════════════════════════════════════════════════════════════════

class TestTeamEnforcement:

    def test_no_cap_is_configured_so_creation_is_never_blocked(self, db):
        org, owner, comm = _tenant(db, name="Teams Free", plan="verified")
        for i in range(12):
            db.add(models.Team(organization_id=org.id, name=f"Team {i}"))
        db.commit()
        check_team_limit(db, owner, adding=1)   # must not raise

    def test_every_plan_reports_team_limit_as_unlimited(self):
        """No invented business numbers — a cap is a product decision."""
        from app.commercial.entitlements import _PLAN_LIMITS
        for plan, limits in _PLAN_LIMITS.items():
            assert "team_limit" in limits, f"{plan} is missing team_limit"
            assert limits["team_limit"] is None, (
                f"{plan} declares a team cap; the onboarding wizard must then "
                "render it from the server and this test should assert the cap"
            )

    def test_a_configured_cap_is_enforced(self, db, monkeypatch):
        """Proves enabling a cap needs config only, not new enforcement code."""
        from app.commercial import entitlements

        org, owner, comm = _tenant(db, name="Teams Capped", plan="verified")
        db.add_all([
            models.Team(organization_id=org.id, name="One"),
            models.Team(organization_id=org.id, name="Two"),
        ])
        db.commit()

        capped = {
            k: ({**v, "team_limit": 2} if k == "verified" else v)
            for k, v in entitlements._PLAN_LIMITS.items()
        }
        monkeypatch.setattr(entitlements, "_PLAN_LIMITS", capped)
        entitlements.invalidate(db, comm.id)

        with pytest.raises(HTTPException) as exc:
            check_team_limit(db, owner, adding=1)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "TEAM_LIMIT_REACHED"
        assert exc.value.detail["team_limit"] == 2
        assert exc.value.detail["teams_used"] == 2

    def test_team_counting_is_tenant_scoped(self, db, monkeypatch):
        from app.commercial import entitlements

        org_a, owner_a, comm_a = _tenant(db, name="TeamScope A", plan="verified")
        org_b, owner_b, comm_b = _tenant(db, name="TeamScope B", plan="verified")
        db.add_all([
            models.Team(organization_id=org_b.id, name="B One"),
            models.Team(organization_id=org_b.id, name="B Two"),
            models.Team(organization_id=org_b.id, name="B Three"),
        ])
        db.commit()

        capped = {
            k: ({**v, "team_limit": 2} if k == "verified" else v)
            for k, v in entitlements._PLAN_LIMITS.items()
        }
        monkeypatch.setattr(entitlements, "_PLAN_LIMITS", capped)
        entitlements.invalidate(db, comm_a.id)
        entitlements.invalidate(db, comm_b.id)

        # A has no teams — B's three must not count against it.
        check_team_limit(db, owner_a, adding=1)
        with pytest.raises(HTTPException):
            check_team_limit(db, owner_b, adding=1)
