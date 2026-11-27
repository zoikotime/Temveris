"""
Tenant isolation security tests.

Validates that:
  - Org A admin can NEVER see Org B data (cross-tenant data leakage)
  - Org A employee can NEVER access Org B resources
  - Super Admin can access all orgs (explicit allow-list)

These tests are DB-layer tests that exercise the exact query logic used
in the fixed routers (ai, intelligence, leave) — they do not require
a running HTTP server.
"""

from __future__ import annotations

import os
import sys
import pathlib
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db  # noqa: E402
from database import models            # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def iso_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # This module points the PROCESS-WIDE session factory at a private database
    # holding six tables. It has to be put back afterwards: conftest's `db`
    # fixture reads `_db.SessionLocal` on every call, so leaving ours installed
    # silently moved every later test module onto this stripped schema — they
    # then failed with "no such table" for anything outside the six, purely
    # because of file ordering.
    _prev_engine, _prev_sessionmaker = _db.engine, _db.SessionLocal
    _db.engine = eng
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    # Create only the tables we need for isolation tests.
    for tbl in [
        models.Organization,
        models.User,
        models.ClockSession,
        models.Activity,
        models.LeaveRequest,
        models.AuditLog,
    ]:
        tbl.__table__.create(eng, checkfirst=True)

    try:
        yield eng
    finally:
        _db.engine, _db.SessionLocal = _prev_engine, _prev_sessionmaker


@pytest.fixture(scope="module")
def iso_db(iso_engine):
    SessionLocal = _db.SessionLocal
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture(scope="module")
def seed(iso_db):
    """
    Seed two completely separate tenants:
      - Org A  (id=1): admin_a (id=1), employee_a (id=2)
      - Org B  (id=2): admin_b (id=3), employee_b (id=4)

    Each tenant has its own ClockSession, Activity, and LeaveRequest rows.
    A super_admin user (id=5) has organization_id=None.
    """
    db = iso_db

    org_a = models.Organization(id=1, name="Acme Corp")
    org_b = models.Organization(id=2, name="Rival Inc")
    db.add_all([org_a, org_b])
    db.flush()

    admin_a    = models.User(id=1, name="Admin A",    email="admin_a@acme.com",   role="admin",    organization_id=1)
    employee_a = models.User(id=2, name="Employee A", email="emp_a@acme.com",     role="employee", organization_id=1)
    admin_b    = models.User(id=3, name="Admin B",    email="admin_b@rival.com",  role="admin",    organization_id=2)
    employee_b = models.User(id=4, name="Employee B", email="emp_b@rival.com",    role="employee", organization_id=2)
    super_adm  = models.User(id=5, name="Super",      email="super@temveris.example",    role="super_admin", organization_id=None)
    db.add_all([admin_a, employee_a, admin_b, employee_b, super_adm])
    db.flush()

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Org A data
    db.add(models.ClockSession(
        id=1, user_id=2, organization_id=1,
        clock_in_time=day_start, clock_out_time=day_start + timedelta(hours=8),
    ))
    db.add(models.Activity(
        id=1, user_id=2, organization_id=1, app_name="VSCode",
        timestamp=day_start + timedelta(hours=1), is_active=True,
    ))
    db.add(models.LeaveRequest(
        id=1, user_id=2, organization_id=1, leave_type="Sick Leave",
        from_date=day_start, to_date=day_start, description="Unwell",
        status="submitted",
    ))

    # Org B data
    db.add(models.ClockSession(
        id=2, user_id=4, organization_id=2,
        clock_in_time=day_start, clock_out_time=day_start + timedelta(hours=8),
    ))
    db.add(models.Activity(
        id=2, user_id=4, organization_id=2, app_name="Slack",
        timestamp=day_start + timedelta(hours=1), is_active=True,
    ))
    db.add(models.LeaveRequest(
        id=2, user_id=4, organization_id=2, leave_type="Annual Leave",
        from_date=day_start, to_date=day_start, description="Holiday",
        status="submitted",
    ))

    db.commit()
    return {"admin_a": admin_a, "employee_a": employee_a,
            "admin_b": admin_b, "employee_b": employee_b,
            "super_admin": super_adm, "org_a_id": 1, "org_b_id": 2}


# ─── Helpers (mirror the fixed router query logic) ────────────────────────────

def _user_in_org(db, user_id: int, org_id: int):
    """Mirrors the _get_user_in_org helper added to intelligence/router.py."""
    return db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == org_id,
    ).first()


def _clock_sessions(db, user_id: int, org_id: int, day_start, day_end):
    """Mirrors fixed ClockSession query in ai/router.py."""
    return db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id == user_id,
            models.ClockSession.organization_id == org_id,
            models.ClockSession.clock_in_time >= day_start,
            models.ClockSession.clock_in_time < day_end,
        )
    ).all()


def _activities(db, user_id: int, org_id: int, day_start, day_end):
    """Mirrors fixed Activity query in ai/router.py."""
    return db.query(models.Activity).filter(
        and_(
            models.Activity.user_id == user_id,
            models.Activity.organization_id == org_id,
            models.Activity.timestamp >= day_start,
            models.Activity.timestamp < day_end,
        )
    ).all()


def _leave_requests_for_org(db, admin_org_id: int):
    """Mirrors fixed GET /leave/requests query."""
    org_user_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == admin_org_id)
        .all()
    ]
    return db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id.in_(org_user_ids)
    ).all()


def _team_users(db, org_id: int):
    """Mirrors fixed get_team_pulse query in intelligence/router.py."""
    return db.query(models.User).filter(
        models.User.organization_id == org_id,
        models.User.is_active.is_(True),
        models.User.role.in_(("employee", "manager", "hr")),
    ).all()


def _teammates_in_coverage(db, user: models.User):
    """Mirrors fixed _assess_team_coverage query in leave/router.py."""
    return db.query(models.User).filter(
        models.User.organization_id == user.organization_id,
        models.User.department == user.department,
        models.User.id != user.id,
    ).all()


# ─── Cross-tenant isolation: AI routes ───────────────────────────────────────

class TestAIRoutesIsolation:
    """Org A admin must NOT see Org B employee data via AI summary."""

    def test_user_lookup_blocked_across_orgs(self, iso_db, seed):
        """_get_user_in_org returns None when user_id belongs to a different org."""
        org_b_employee_id = seed["employee_b"].id
        caller_org_id     = seed["org_a_id"]

        result = _user_in_org(iso_db, org_b_employee_id, caller_org_id)
        assert result is None, (
            "SECURITY: Org A admin can resolve Org B user — cross-tenant leak"
        )

    def test_clock_sessions_scoped_to_org(self, iso_db, seed):
        """ClockSession query for Org B employee returns empty when called with Org A org_id."""
        now       = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)

        # Org A admin requesting Org B employee's sessions
        sessions = _clock_sessions(
            iso_db,
            user_id=seed["employee_b"].id,
            org_id=seed["org_a_id"],
            day_start=day_start,
            day_end=day_end,
        )
        assert sessions == [], (
            "SECURITY: Org A received Org B clock sessions"
        )

    def test_clock_sessions_own_org_returns_data(self, iso_db, seed):
        """Sanity: Org A admin can see Org A employee sessions."""
        now       = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)

        sessions = _clock_sessions(
            iso_db,
            user_id=seed["employee_a"].id,
            org_id=seed["org_a_id"],
            day_start=day_start,
            day_end=day_end,
        )
        assert len(sessions) == 1

    def test_activities_scoped_to_org(self, iso_db, seed):
        """Activity query for Org B employee returns empty when called with Org A org_id."""
        now       = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)

        activities = _activities(
            iso_db,
            user_id=seed["employee_b"].id,
            org_id=seed["org_a_id"],
            day_start=day_start,
            day_end=day_end,
        )
        assert activities == [], (
            "SECURITY: Org A received Org B activity records"
        )

    def test_activities_own_org_returns_data(self, iso_db, seed):
        """Sanity: Org A admin can see Org A employee activities."""
        now       = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)

        activities = _activities(
            iso_db,
            user_id=seed["employee_a"].id,
            org_id=seed["org_a_id"],
            day_start=day_start,
            day_end=day_end,
        )
        assert len(activities) == 1


# ─── Cross-tenant isolation: Intelligence routes ──────────────────────────────

class TestIntelligenceRoutesIsolation:

    def test_get_user_in_org_blocks_cross_tenant(self, iso_db, seed):
        """Intelligence router helper returns None for cross-org user_id."""
        result = _user_in_org(
            iso_db,
            user_id=seed["employee_b"].id,
            org_id=seed["org_a_id"],
        )
        assert result is None, (
            "SECURITY: _get_user_in_org allows cross-org user lookup"
        )

    def test_get_user_in_org_own_org_works(self, iso_db, seed):
        """Intelligence router helper succeeds for same-org user_id."""
        result = _user_in_org(
            iso_db,
            user_id=seed["employee_a"].id,
            org_id=seed["org_a_id"],
        )
        assert result is not None
        assert result.organization_id == seed["org_a_id"]

    def test_team_pulse_scoped_to_org(self, iso_db, seed):
        """get_team_pulse query must only return users from caller's org."""
        users = _team_users(iso_db, org_id=seed["org_a_id"])
        ids   = {u.id for u in users}

        assert seed["employee_a"].id in ids, "Org A employee must appear in Org A team pulse"
        assert seed["employee_b"].id not in ids, (
            "SECURITY: Org B employee appeared in Org A team pulse"
        )

    def test_team_pulse_org_b_separate(self, iso_db, seed):
        """Org B team pulse must not include Org A users."""
        users = _team_users(iso_db, org_id=seed["org_b_id"])
        ids   = {u.id for u in users}

        assert seed["employee_b"].id in ids
        assert seed["employee_a"].id not in ids, (
            "SECURITY: Org A employee appeared in Org B team pulse"
        )


# ─── Cross-tenant isolation: Leave routes ────────────────────────────────────

class TestLeaveRoutesIsolation:

    def test_leave_requests_scoped_to_org(self, iso_db, seed):
        """Admin GET /leave/requests must only return requests from own org."""
        # Org A admin
        reqs = _leave_requests_for_org(iso_db, admin_org_id=seed["org_a_id"])
        request_user_ids = {r.user_id for r in reqs}

        assert seed["employee_a"].id in request_user_ids
        assert seed["employee_b"].id not in request_user_ids, (
            "SECURITY: Org A received Org B leave requests"
        )

    def test_leave_requests_org_b_separate(self, iso_db, seed):
        """Org B admin only sees Org B leave requests."""
        reqs = _leave_requests_for_org(iso_db, admin_org_id=seed["org_b_id"])
        request_user_ids = {r.user_id for r in reqs}

        assert seed["employee_b"].id in request_user_ids
        assert seed["employee_a"].id not in request_user_ids, (
            "SECURITY: Org B received Org A leave requests"
        )

    def test_team_coverage_scoped_to_org(self, iso_db, seed):
        """_assess_team_coverage must not pull teammates from other orgs."""
        # employee_a has department=None so result is [], but the query must
        # not cross org boundaries even if departments match across orgs.
        employee_a = seed["employee_a"]
        teammates  = _teammates_in_coverage(iso_db, employee_a)
        teammate_ids = {t.id for t in teammates}

        assert seed["employee_b"].id not in teammate_ids, (
            "SECURITY: team coverage included employee from different org"
        )

    def test_eligibility_user_lookup_blocked_across_orgs(self, iso_db, seed):
        """GET /leave/eligibility/{user_id} must not resolve cross-org users."""
        result = _user_in_org(
            iso_db,
            user_id=seed["employee_b"].id,
            org_id=seed["org_a_id"],
        )
        assert result is None, (
            "SECURITY: eligibility endpoint can resolve cross-org user"
        )


# ─── Employee self-isolation ──────────────────────────────────────────────────

class TestEmployeeSelfIsolation:
    """Employees must only access their own data, never another org."""

    def test_employee_cannot_resolve_cross_org_user(self, iso_db, seed):
        """An employee-role caller with org_a_id cannot resolve org_b user."""
        result = _user_in_org(
            iso_db,
            user_id=seed["employee_b"].id,
            org_id=seed["org_a_id"],
        )
        assert result is None

    def test_employee_can_resolve_own_user(self, iso_db, seed):
        """An employee can resolve their own record via org-scoped lookup."""
        result = _user_in_org(
            iso_db,
            user_id=seed["employee_a"].id,
            org_id=seed["org_a_id"],
        )
        assert result is not None
        assert result.id == seed["employee_a"].id


# ─── Super Admin access ───────────────────────────────────────────────────────

class TestSuperAdminAccess:
    """Super admin queries must be able to span all orgs."""

    def test_super_admin_can_query_org_a_users(self, iso_db, seed):
        """Super admin can explicitly query Org A users."""
        users = db_query_org_users(iso_db, org_id=seed["org_a_id"])
        ids   = {u.id for u in users}
        assert seed["admin_a"].id    in ids
        assert seed["employee_a"].id in ids

    def test_super_admin_can_query_org_b_users(self, iso_db, seed):
        """Super admin can explicitly query Org B users."""
        users = db_query_org_users(iso_db, org_id=seed["org_b_id"])
        ids   = {u.id for u in users}
        assert seed["admin_b"].id    in ids
        assert seed["employee_b"].id in ids

    def test_super_admin_sees_all_orgs(self, iso_db, seed):
        """Platform-wide user query returns users from both orgs."""
        all_users = iso_db.query(models.User).filter(
            models.User.organization_id.isnot(None)
        ).all()
        org_ids = {u.organization_id for u in all_users}
        assert 1 in org_ids and 2 in org_ids


def db_query_org_users(db, org_id: int):
    return db.query(models.User).filter(
        models.User.organization_id == org_id
    ).all()


# ─── Team membership scoping (bug #41) ───────────────────────────────────────
# Mirrors the fixed org/router.py team-membership queries. Before the fix
# these three were NOT org-scoped, while list_teams' worker count and the
# rename re-point WERE — so a foreign-org member showed in the modal but
# counted 0 on the grid and orphaned on the next rename.

def _assignable_users(db, team_name, org_id):
    return db.query(models.User).filter(
        models.User.organization_id == org_id,
        models.User.is_active.is_(True),
        (models.User.team != team_name) | (models.User.team.is_(None)),
    ).all()


def _addable_users(db, user_ids, org_id):
    return db.query(models.User).filter(
        models.User.id.in_(user_ids),
        models.User.organization_id == org_id,
        models.User.is_active.is_(True),
    ).all()


def _member_count(db, team_name, org_id):
    return db.query(models.User).filter(
        models.User.team == team_name,
        models.User.organization_id == org_id,
        models.User.is_active.is_(True),
    ).count()


class TestTeamMembershipScoping:

    def test_assignable_excludes_other_org(self, iso_db, seed):
        candidates = _assignable_users(iso_db, "tester2", seed["org_a_id"])
        ids = {u.id for u in candidates}
        assert seed["employee_a"].id in ids
        assert seed["employee_b"].id not in ids, (
            "SECURITY: Org B user offered as assignable member for Org A team"
        )

    def test_add_and_count_consistent_within_org(self, iso_db, seed):
        # Admin A tries to add both an own-org and a foreign-org user.
        users = _addable_users(
            iso_db,
            [seed["employee_a"].id, seed["employee_b"].id],
            seed["org_a_id"],
        )
        assert {u.id for u in users} == {seed["employee_a"].id}, (
            "add must silently drop cross-org user_ids"
        )
        for u in users:
            u.team = "tester2"
        iso_db.commit()

        # The grid count (org-scoped) must reflect the added member — the
        # bug report's "persistent 0 workers" symptom.
        assert _member_count(iso_db, "tester2", seed["org_a_id"]) == 1
        # Cleanup so the module-scoped session stays clean for other tests.
        seed["employee_a"].team = None
        iso_db.commit()
