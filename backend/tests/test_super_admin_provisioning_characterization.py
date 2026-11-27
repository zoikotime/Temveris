"""Characterization tests for Super-Admin tenant provisioning.

These pin the CURRENT, WORKING behaviour of
``app.super_admin.router.provision_organization`` *before* the shared
tenant-provisioning service is extracted out of the HTTP handler.

They are deliberately behavioural, not structural: they assert on the rows
that end up in the database and on the response contract, NOT on which
function produced them. That is what makes them useful across the
extraction — the same assertions must hold when the logic moves into
``app.org.provisioning_service``.

Scope note: the endpoint is exercised by calling the handler directly with
an explicit session and a stub Request. That keeps the test focused on
provisioning semantics rather than on FastAPI dependency wiring; the
``require_super_admin`` gate itself is covered separately below by
asserting on the dependency function.
"""

from __future__ import annotations

import os
import sys
import pathlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db          # noqa: E402
from database import models                   # noqa: E402
from app.commercial import models as cm       # noqa: E402


# ─── Stubs ───────────────────────────────────────────────────────────────────

class _StubClient:
    host = "203.0.113.10"


class _StubRequest:
    """Minimal stand-in for fastapi.Request.

    provision_organization only reads `headers.get("x-forwarded-for")` and
    `client.host`, so that is all this provides.
    """

    def __init__(self, forwarded_for: str = ""):
        self.headers = {"x-forwarded-for": forwarded_for}
        self.client = _StubClient()


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def prov_engine():
    """A focused engine for this module.

    `database.database.engine` / `SessionLocal` are module-level globals that the
    application code resolves at call time, so they have to be pointed at this
    engine — and pointed BACK afterwards. Without the restore, every later test
    module that relies on the session-scoped conftest engine would silently
    inherit this one, which only has the handful of tables created below.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = eng
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    for tbl in (
        models.Organization,
        models.User,
        models.OrgSettings,
        models.AppSettings,
        models.LeavePolicy,
        models.Team,
        models.AuditLog,
        cm.CommercialOrganization,
    ):
        tbl.__table__.create(eng, checkfirst=True)

    try:
        yield eng
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def db(prov_engine):
    s = _db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def super_admin(db):
    """A persisted super_admin actor. Platform-layer: organization_id is NULL."""
    existing = db.query(models.User).filter_by(email="platform@temveris.example").first()
    if existing:
        return existing
    u = models.User(
        name="Platform Owner",
        email="platform@temveris.example",
        role="super_admin",
        organization_id=None,
        is_active=True,
        is_password_set=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture(autouse=True)
def _no_outbound_email(monkeypatch):
    """Never touch a real transport. The handler imports send_invite_email
    lazily inside the function body, so patching the module attribute is
    sufficient and keeps the post-commit branch deterministic."""
    sent: list[dict] = []

    def _fake_send(to_email, employee_name, invite_link, inviter_name):
        sent.append({
            "to_email": to_email,
            "employee_name": employee_name,
            "invite_link": invite_link,
            "inviter_name": inviter_name,
        })
        return True

    import app.email_service as es
    monkeypatch.setattr(es, "send_invite_email", _fake_send)
    return sent


def _provision(db, super_admin, **overrides):
    """Call the handler directly with a body built from defaults + overrides."""
    from app.super_admin.router import provision_organization, CreateOrgRequest

    payload = {
        "name": "Acme Corp",
        "country": "IN",
        "timezone": "Asia/Kolkata",
        "currency": "INR",
        "admin_email": "admin@acme.com",
        "admin_name": "Acme Admin",
    }
    payload.update(overrides)
    return provision_organization(
        body=CreateOrgRequest(**payload),
        request=_StubRequest(),
        db=db,
        super_admin=super_admin,
    )


# ═════════════════════════════════════════════════════════════════════════════
# The provisioning contract
# ═════════════════════════════════════════════════════════════════════════════

class TestSuperAdminProvisioningContract:
    """Each assertion here is a property the shared service must preserve."""

    def test_operational_organization_is_created(self, db, super_admin):
        res = _provision(db, super_admin, name="Org Create", admin_email="a1@x.com")
        org = db.query(models.Organization).filter_by(id=res["organization_id"]).one()
        assert org.name == "Org Create"
        assert org.is_active is True

    def test_admin_user_is_created_with_org_and_admin_role(self, db, super_admin):
        res = _provision(db, super_admin, name="Org User", admin_email="a2@x.com")
        user = db.query(models.User).filter_by(id=res["admin_user_id"]).one()

        # The three properties that define a usable tenant admin.
        assert user.role == "admin"
        assert user.organization_id == res["organization_id"]
        assert user.organization_id is not None

        # Invite-based credential establishment: no password yet.
        assert user.password is None
        assert user.is_password_set is False
        assert user.is_invited is True
        assert user.is_active is True
        assert user.invited_by_id == super_admin.id
        assert user.invite_token is not None
        assert user.invite_token_expires_at is not None

    def test_org_settings_created_and_scoped(self, db, super_admin):
        res = _provision(
            db, super_admin, name="Org Settings", admin_email="a3@x.com",
            company_name="Settings Display Name", slug="settings-co",
        )
        oid = res["organization_id"]
        row = db.query(models.OrgSettings).filter_by(organization_id=oid).one()
        assert row.company_name == "Settings Display Name"
        assert row.slug == "settings-co"
        assert row.primary_country == "IN"
        assert row.default_timezone == "Asia/Kolkata"
        assert row.default_currency == "INR"

    def test_required_app_settings_are_seeded_and_org_stamped(self, db, super_admin):
        res = _provision(db, super_admin, name="Org AppSettings", admin_email="a4@x.com")
        oid = res["organization_id"]
        rows = {
            r.key: r
            for r in db.query(models.AppSettings).filter_by(organization_id=oid).all()
        }
        assert f"shared_pool_enabled:{oid}" in rows
        assert f"leave_waiting_period_months:{oid}" in rows

        # Both the suffixed key AND the column must be set — a row with the key
        # but a NULL organization_id lands in the shared bucket that migration
        # c4a7e2b915d3 exists to repair.
        for r in rows.values():
            assert r.organization_id == oid

        assert rows[f"shared_pool_enabled:{oid}"].value == "true"
        assert rows[f"leave_waiting_period_months:{oid}"].value == "6"

    def test_leave_policies_are_seeded_org_scoped(self, db, super_admin):
        from app.leave.router import LEAVE_POLICIES

        res = _provision(db, super_admin, name="Org Leave", admin_email="a5@x.com")
        oid = res["organization_id"]
        policies = db.query(models.LeavePolicy).filter_by(organization_id=oid).all()

        assert len(policies) == len(LEAVE_POLICIES)
        assert res["seeded"]["leave_policies"] == len(LEAVE_POLICIES)
        assert {p.leave_type for p in policies} == set(LEAVE_POLICIES.keys())
        for p in policies:
            assert p.organization_id == oid
            assert p.is_active is True

    def test_commercial_organization_links_to_operational(self, db, super_admin):
        res = _provision(db, super_admin, name="Org Commercial", admin_email="a6@x.com")
        oid = res["organization_id"]
        comm = (
            db.query(cm.CommercialOrganization)
              .filter_by(operational_org_id=oid)
              .one()
        )
        # This link is the bridge the self-service path currently fails to write.
        assert comm.operational_org_id == oid
        assert comm.owner_user_id == res["admin_user_id"]
        assert comm.name == "Org Commercial"
        assert comm.commercial_state == "active"
        assert comm.id.startswith("org_")

    def test_teams_seeded_only_when_requested(self, db, super_admin):
        res_off = _provision(db, super_admin, name="Org NoTeams", admin_email="a7@x.com")
        assert res_off["seeded"]["teams"] == 0
        assert db.query(models.Team).filter_by(
            organization_id=res_off["organization_id"]
        ).count() == 0

        res_on = _provision(
            db, super_admin, name="Org Teams", admin_email="a8@x.com",
            seed_default_teams=True, default_teams=["Engineering", "Support"],
        )
        teams = db.query(models.Team).filter_by(
            organization_id=res_on["organization_id"]
        ).all()
        assert res_on["seeded"]["teams"] == 2
        assert {t.name for t in teams} == {"Engineering", "Support"}
        for t in teams:
            assert t.organization_id == res_on["organization_id"]

    def test_audit_row_written_and_org_scoped(self, db, super_admin):
        res = _provision(db, super_admin, name="Org Audit", admin_email="a9@x.com")
        oid = res["organization_id"]
        row = (
            db.query(models.AuditLog)
              .filter_by(organization_id=oid, action="OrgProvisioned")
              .one()
        )
        assert row.entity == "Organization"
        assert row.actor_id == super_admin.id
        assert row.target_id == res["admin_user_id"]
        assert row.payload["admin_email"] == "a9@x.com"
        assert row.payload["provisioned_by"] == super_admin.email

    def test_response_contract(self, db, super_admin):
        res = _provision(db, super_admin, name="Org Response", admin_email="a10@x.com")
        for key in (
            "success", "organization_id", "org_name", "admin_email",
            "admin_user_id", "invite_link", "invite_expires_at",
            "email_sent", "seeded", "next_steps",
        ):
            assert key in res, f"missing response key: {key}"
        assert res["success"] is True
        assert res["invite_link"].endswith(
            db.query(models.User).filter_by(id=res["admin_user_id"]).one().invite_token
        )

    def test_invite_email_sent_after_commit(self, db, super_admin, _no_outbound_email):
        res = _provision(db, super_admin, name="Org Email", admin_email="a11@x.com")
        assert res["email_sent"] is True
        assert _no_outbound_email[-1]["to_email"] == "a11@x.com"
        assert _no_outbound_email[-1]["invite_link"] == res["invite_link"]


# ═════════════════════════════════════════════════════════════════════════════
# Ownership
#
# Before the extraction, OrgSettings.owner_user_id was left NULL and
# `org.router._resolve_owner` had to guess the owner as "earliest-created
# admin/hr in the org". The shared service now records it explicitly (audit
# §9, Option C). For a freshly provisioned tenant the explicit value IS that
# same user, so the /org/settings response is unchanged — the guess is simply
# no longer needed. Both properties are asserted below.
# ═════════════════════════════════════════════════════════════════════════════

class TestOwnership:
    def test_org_settings_owner_user_id_is_recorded_explicitly(self, db, super_admin):
        res = _provision(db, super_admin, name="Org Owner", admin_email="a12@x.com")
        row = db.query(models.OrgSettings).filter_by(
            organization_id=res["organization_id"]
        ).one()
        assert row.owner_user_id == res["admin_user_id"]
        assert row.owner_since is not None

    def test_resolved_owner_matches_the_pre_extraction_fallback(self, db, super_admin):
        """The explicit owner is the same user the old fallback would have
        picked, so no API response changes."""
        res = _provision(db, super_admin, name="Org Fallback", admin_email="a13@x.com")
        from app.org.router import _resolve_owner

        oid = res["organization_id"]
        row = db.query(models.OrgSettings).filter_by(organization_id=oid).one()

        # Resolution with the explicit column populated.
        owner, since = _resolve_owner(db, row, organization_id=oid)
        assert owner is not None
        assert owner.id == res["admin_user_id"]
        assert since is not None

        # …and it agrees with what the earliest-admin fallback would return.
        fallback = (
            db.query(models.User)
              .filter(models.User.role.in_(("admin", "hr")),
                      models.User.organization_id == oid)
              .order_by(models.User.created_at.asc())
              .first()
        )
        assert fallback is not None
        assert fallback.id == owner.id

    def test_commercial_owner_and_org_settings_owner_agree(self, db, super_admin):
        """Two ownership columns, one fact — they must never disagree."""
        res = _provision(db, super_admin, name="Org Owners", admin_email="a15@x.com")
        oid = res["organization_id"]
        settings = db.query(models.OrgSettings).filter_by(organization_id=oid).one()
        comm = db.query(cm.CommercialOrganization).filter_by(operational_org_id=oid).one()
        assert settings.owner_user_id == comm.owner_user_id == res["admin_user_id"]

    def test_no_owner_id_column_added_to_organizations(self):
        """Guard against a third source of truth for ownership."""
        assert not hasattr(models.Organization, "owner_id")
        assert "owner_id" not in models.Organization.__table__.columns


# ═════════════════════════════════════════════════════════════════════════════
# Validation + rollback
# ═════════════════════════════════════════════════════════════════════════════

class TestValidationAndRollback:
    def test_rejects_invalid_admin_email(self, db, super_admin):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _provision(db, super_admin, name="Bad", admin_email="not-an-email")
        assert exc.value.status_code == 400

    def test_rejects_blank_org_name(self, db, super_admin):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _provision(db, super_admin, name="   ", admin_email="ok@x.com")
        assert exc.value.status_code == 400

    def test_rejects_bad_slug(self, db, super_admin):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _provision(db, super_admin, name="Slug", admin_email="slug@x.com",
                       slug="Not A Slug")
        assert exc.value.status_code == 400

    def test_rejects_email_of_activated_account(self, db, super_admin):
        from fastapi import HTTPException
        db.add(models.User(
            name="Live", email="live@x.com", role="admin",
            password="hashed", is_password_set=True, is_active=True,
        ))
        db.commit()
        with pytest.raises(HTTPException) as exc:
            _provision(db, super_admin, name="Dup", admin_email="live@x.com")
        assert exc.value.status_code == 409

    def test_validation_failure_leaves_no_partial_tenant(self, db, super_admin):
        """A rejected request must not leave an Organization behind."""
        from fastapi import HTTPException
        before = db.query(models.Organization).count()
        with pytest.raises(HTTPException):
            _provision(db, super_admin, name="Rollback Me", admin_email="bad-email")
        db.rollback()
        assert db.query(models.Organization).count() == before
        assert db.query(models.Organization).filter_by(name="Rollback Me").count() == 0

    def test_provisioning_failure_rolls_back_everything(self, db, super_admin, monkeypatch):
        """Force a mid-transaction failure and assert nothing survives.

        The failure is injected at the CommercialOrganization step (step 7),
        i.e. AFTER the Organization, OrgSettings, AppSettings, LeavePolicy and
        User rows have all been flushed. If the transaction were not atomic,
        those rows would leak.
        """
        import app.org.provisioning_service as ps

        def _boom():
            raise RuntimeError("injected failure at commercial step")

        monkeypatch.setattr(ps, "_commercial_models", _boom)

        from fastapi import HTTPException
        orgs_before = db.query(models.Organization).count()
        users_before = db.query(models.User).count()
        settings_before = db.query(models.OrgSettings).count()
        policies_before = db.query(models.LeavePolicy).count()

        with pytest.raises(HTTPException) as exc:
            _provision(db, super_admin, name="Doomed Corp", admin_email="doomed@x.com")
        assert exc.value.status_code == 500

        assert db.query(models.Organization).count() == orgs_before
        assert db.query(models.User).count() == users_before
        assert db.query(models.OrgSettings).count() == settings_before
        assert db.query(models.LeavePolicy).count() == policies_before

        # Specifically: no orphan user, and the email stays registerable.
        assert db.query(models.User).filter_by(email="doomed@x.com").first() is None
        assert db.query(models.Organization).filter_by(name="Doomed Corp").first() is None


# ═════════════════════════════════════════════════════════════════════════════
# Authorization — the Super Admin boundary must not move
# ═════════════════════════════════════════════════════════════════════════════

class TestSuperAdminAuthorization:
    @pytest.mark.parametrize("role", ["admin", "hr", "employee", "manager", "owner", None])
    def test_require_super_admin_rejects_every_other_role(self, role):
        from fastapi import HTTPException
        from app.auth.service import require_super_admin

        user = models.User(name="X", email="x@x.com", role=role)
        with pytest.raises(HTTPException) as exc:
            require_super_admin(current_user=user)
        assert exc.value.status_code == 403

    def test_require_super_admin_allows_super_admin(self):
        from app.auth.service import require_super_admin

        user = models.User(name="S", email="s@x.com", role="super_admin")
        assert require_super_admin(current_user=user) is user

    def test_provision_endpoint_depends_on_require_super_admin(self):
        """The gate is wired as a dependency, not an inline check — assert the
        wiring so an extraction cannot silently drop it."""
        import inspect
        from app.auth.service import require_super_admin
        from app.super_admin.router import provision_organization

        sig = inspect.signature(provision_organization)
        dep = sig.parameters["super_admin"].default
        assert getattr(dep, "dependency", None) is require_super_admin

    def test_super_admin_stays_org_less_after_provisioning(self, db, super_admin):
        """Provisioning a tenant must not attach the platform actor to it."""
        _provision(db, super_admin, name="Org Platform", admin_email="a14@x.com")
        db.refresh(super_admin)
        assert super_admin.organization_id is None
        assert super_admin.role == "super_admin"
