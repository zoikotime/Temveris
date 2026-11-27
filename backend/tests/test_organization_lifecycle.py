"""Organization lifecycle: ACTIVE → SUSPENDED → DELETED.

Two things were broken and are pinned here:

  1. Suspension wrote ``commercial_state = 'suspended'`` and nothing read it.
     Every authentication surface admitted a suspended tenant's users exactly
     as before. These tests assert the gate now bites at login, on an
     already-issued token, and on refresh — and that it does NOT bite super
     admins, impersonation sessions, or any OTHER tenant.

  2. There was no deletion of any kind. These tests assert the purge removes
     the tenant's rows across every scope (operational / commercial /
     offboarding / user-scoped), leaves other tenants and global tables
     untouched, refuses an active organization, refuses a mistyped name, and
     rolls the whole thing back on failure.

Style follows tests/test_super_admin_provisioning_characterization.py: a
module-scoped SQLite engine pointed at by `database.database`, handlers called
directly with an explicit session and a stub Request, so the assertions are
about behaviour and rows rather than FastAPI wiring. The `require_super_admin`
gate is asserted on the dependency itself, the same way that module does it.
"""

from __future__ import annotations

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db          # noqa: E402
from database import models                   # noqa: E402
from app.commercial import models as cm       # noqa: E402
from app.org import deletion_service          # noqa: E402
from app.org import lifecycle                 # noqa: E402


# ─── Stubs ───────────────────────────────────────────────────────────────────

class _StubClient:
    host = "203.0.113.10"


class _StubRequest:
    def __init__(self, forwarded_for: str = ""):
        self.headers = {"x-forwarded-for": forwarded_for}
        self.client = _StubClient()


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lc_engine():
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
        models.Organization, models.User, models.OrgSettings, models.AppSettings,
        models.LeavePolicy, models.Team, models.AuditLog, models.ClockSession,
        models.Activity, models.LeaveRequest, models.UserSession,
        models.RefreshToken, models.Holiday,
        # The three tables whose ORM models declare an organization_id that the
        # real database does not have. They are purged through the tenant's
        # users; these tests prove the rows actually go.
        models.PayrollRun, models.PayrollItem, models.IntegrityScore,
        models.DailyIntelligenceSummary,
        cm.CommercialOrganization, cm.CommercialSubscription, cm.CommercialEvent,
        cm.EntitlementsCache,
    ):
        tbl.__table__.create(eng, checkfirst=True)

    try:
        yield eng
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def db(lc_engine):
    s = _db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _clear_caches():
    """The lifecycle answer is cached per organization; tests mutate state
    directly, so every test starts from a cold cache."""
    lifecycle.invalidate()
    from app.auth import service as auth_service
    auth_service._USER_CACHE.clear()
    auth_service._ORG_CACHE.clear()
    yield
    lifecycle.invalidate()


@pytest.fixture(autouse=True)
def _no_storage_calls(monkeypatch):
    """Storage cleanup runs post-commit against whatever STORAGE_BACKEND the
    developer's .env names — which may be a real bucket. Tests assert on the
    database, never on object storage, so the call is stubbed out entirely."""
    calls: list = []
    monkeypatch.setattr(
        "app.org.deletion_service.cleanup_storage",
        lambda org_id, paths: (calls.append((org_id, list(paths))) or
                               {"screenshot_dirs": 0, "objects": 0, "failed": 0}),
    )
    return calls


@pytest.fixture
def super_admin(db):
    existing = db.query(models.User).filter_by(email="platform@temveris.example").first()
    if existing:
        return existing
    u = models.User(
        name="Platform Owner", email="platform@temveris.example",
        role="super_admin", organization_id=None,
        is_active=True, is_password_set=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


_SEQ = {"n": 0}


def make_tenant(db, name: str, *, state: str = "active", users: int = 2):
    """Build a minimal but realistic tenant: org, commercial org, users, and a
    row in each of the scopes the purge has to reach."""
    _SEQ["n"] += 1
    n = _SEQ["n"]

    org = models.Organization(name=name, is_active=True)
    db.add(org)
    db.flush()

    db.add(models.OrgSettings(organization_id=org.id, company_name=name))
    db.add(models.AppSettings(organization_id=org.id,
                              key=f"shared_pool_enabled:{org.id}", value="true"))
    db.add(models.Team(organization_id=org.id, name=f"{name} Team"))
    db.add(models.LeavePolicy(organization_id=org.id, leave_type="annual",
                              annual_limit=12, is_active=True))

    created = []
    for i in range(users):
        u = models.User(
            name=f"{name} User {i}", email=f"u{i}.{n}@{name.lower().replace(' ', '')}.test",
            role="admin" if i == 0 else "employee",
            organization_id=org.id, is_active=True, is_password_set=True,
            password="x",
        )
        db.add(u)
        created.append(u)
    db.flush()

    # One row in each remaining scope so the purge has something to prove.
    db.add(models.ClockSession(user_id=created[0].id, organization_id=org.id,
                               clock_in_time=datetime.now(timezone.utc), is_active=False))
    db.add(models.Activity(user_id=created[0].id, organization_id=org.id,
                           timestamp=datetime.now(timezone.utc), app_name="Editor"))
    db.add(models.AuditLog(actor_id=created[0].id, organization_id=org.id,
                           entity="User", action="UserLogin", payload={}))

    # ── The three user-scoped tables the P0 was about ────────────────────────
    # Reached through the org's users, not through organization_id, because the
    # real schema has no such column on them.
    run = models.PayrollRun(organization_id=org.id,
                            period_start=datetime.now(timezone.utc),
                            period_end=datetime.now(timezone.utc), status="draft")
    db.add(run)
    db.flush()
    db.add(models.PayrollItem(payroll_run_id=run.id, user_id=created[0].id,
                              worked_hours=8.0, net_pay=100.0))
    db.add(models.IntegrityScore(user_id=created[0].id,
                                 date=datetime.now(timezone.utc), score=90.0))
    db.add(models.DailyIntelligenceSummary(user_id=created[0].id,
                                           summary_date=datetime.now(timezone.utc),
                                           narrative="test narrative", headline="ok"))

    comm = cm.CommercialOrganization(
        id=f"org_test_{n}", name=name, commercial_state=state,
        operational_org_id=org.id, owner_user_id=created[0].id,
    )
    db.add(comm)
    db.add(cm.CommercialEvent(
        event_id=f"evt_{n}", event_type="tenant.provisioned",
        occurred_at=datetime.now(timezone.utc), source_service="test",
        organization_id=comm.id, correlation_id=f"cor_{n}", payload={},
    ))
    db.commit()
    return org, comm, created


# ═════════════════════════════════════════════════════════════════════════════
# SUSPENSION — enforcement
# ═════════════════════════════════════════════════════════════════════════════

class TestSuspensionEnforcement:

    def test_active_tenant_is_not_blocked(self, db):
        org, comm, users = make_tenant(db, "Acme Active")
        assert lifecycle.is_suspended(db, org.id) is False
        lifecycle.assert_active(db, users[1])  # does not raise

    def test_suspended_commercial_state_blocks(self, db):
        org, comm, users = make_tenant(db, "Acme Susp", state="suspended")
        assert lifecycle.is_suspended(db, org.id) is True
        with pytest.raises(HTTPException) as exc:
            lifecycle.assert_active(db, users[1])
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "organization_suspended"

    def test_operational_is_active_false_also_blocks(self, db):
        """The two fields are independent gates — either one denies. This is
        what keeps a tenant with no commercial row coverable."""
        org, comm, users = make_tenant(db, "Acme Flag")
        org.is_active = False
        db.commit()
        lifecycle.invalidate(org.id)
        assert lifecycle.is_suspended(db, org.id) is True

    def test_deleted_organization_blocks(self, db):
        """A token minted before a purge must not outlive the tenant."""
        org, comm, users = make_tenant(db, "Acme Gone")
        missing_id = org.id + 9999
        assert lifecycle.is_suspended(db, missing_id) is True

    def test_super_admin_is_exempt(self, db, super_admin):
        make_tenant(db, "Acme Exempt", state="suspended")
        lifecycle.assert_active(db, super_admin)  # does not raise
        assert lifecycle.is_exempt(super_admin) is True

    def test_impersonation_is_exempt(self, db):
        """Support has to be able to look inside a suspended tenant."""
        org, comm, users = make_tenant(db, "Acme Imp", state="suspended")
        lifecycle.assert_active(db, users[0], impersonated=True)  # does not raise

    def test_suspending_one_tenant_does_not_affect_another(self, db):
        org_a, comm_a, users_a = make_tenant(db, "Tenant A", state="suspended")
        org_b, comm_b, users_b = make_tenant(db, "Tenant B", state="active")
        assert lifecycle.is_suspended(db, org_a.id) is True
        assert lifecycle.is_suspended(db, org_b.id) is False
        lifecycle.assert_active(db, users_b[0])  # does not raise

    def test_billing_states_are_not_access_states(self, db):
        """past_due / grace_period / trial_expired are the subscription state
        machine's business. Gating login on them would lock out paying
        customers mid-dunning."""
        for state in ("past_due", "grace_period", "trial_expired", "cancelled"):
            org, comm, users = make_tenant(db, f"Billing {state}", state=state)
            assert lifecycle.is_suspended(db, org.id) is False, state

    def test_state_read_failure_fails_open(self, db, monkeypatch):
        """A transient database error must not lock every tenant out."""
        org, comm, users = make_tenant(db, "Acme Flaky")
        monkeypatch.setattr(
            lifecycle, "_read_suspended",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection reset")),
        )
        lifecycle.invalidate(org.id)
        assert lifecycle.is_suspended(db, org.id) is False

    def test_cache_is_invalidated_on_demand(self, db):
        org, comm, users = make_tenant(db, "Acme Cache")
        assert lifecycle.is_suspended(db, org.id) is False
        comm.commercial_state = "suspended"
        db.commit()
        # Still cached as allowed…
        assert lifecycle.is_suspended(db, org.id) is False
        # …until the write path invalidates, which is what the endpoint does.
        lifecycle.invalidate(org.id)
        assert lifecycle.is_suspended(db, org.id) is True


# ═════════════════════════════════════════════════════════════════════════════
# SUSPENSION — the endpoint
# ═════════════════════════════════════════════════════════════════════════════

class TestSuspendEndpoint:

    def test_suspend_writes_both_fields_and_revokes_sessions(self, db, super_admin):
        from app.super_admin.router import suspend_organization, OrgStateChangeRequest

        org, comm, users = make_tenant(db, "Acme Suspendable")
        db.add(models.UserSession(user_id=users[0].id, token_hash="h1"))
        db.commit()

        out = suspend_organization(
            comm.id, OrgStateChangeRequest(reason="testing"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["commercial_state"] == "suspended"

        db.expire_all()
        assert db.get(cm.CommercialOrganization, comm.id).commercial_state == "suspended"
        assert db.get(models.Organization, org.id).is_active is False

        sess = db.query(models.UserSession).filter_by(user_id=users[0].id).one()
        assert sess.is_revoked is True

        row = (db.query(models.AuditLog)
                 .filter_by(action="OrgSuspended", organization_id=org.id).one())
        assert row.payload["organization_id"] == comm.id
        assert row.actor_id == super_admin.id

    def test_suspend_is_idempotent(self, db, super_admin):
        from app.super_admin.router import suspend_organization, OrgStateChangeRequest

        org, comm, users = make_tenant(db, "Acme Twice", state="suspended")
        out = suspend_organization(
            comm.id, OrgStateChangeRequest(), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["already"] is True
        # Re-asserts the side effects, which is how an operator repairs a tenant
        # suspended before enforcement existed.
        db.expire_all()
        assert db.get(models.Organization, org.id).is_active is False

    def test_restore_reverses_both_fields(self, db, super_admin):
        from app.super_admin.router import restore_organization, OrgStateChangeRequest

        org, comm, users = make_tenant(db, "Acme Restorable", state="suspended")
        org.is_active = False
        db.commit()

        out = restore_organization(
            comm.id, OrgStateChangeRequest(reason="resolved"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["commercial_state"] == "active"
        db.expire_all()
        assert db.get(models.Organization, org.id).is_active is True
        assert lifecycle.is_suspended(db, org.id) is False

    def test_restore_returns_trial_when_trial_still_running(self, db, super_admin):
        from app.super_admin.router import restore_organization, OrgStateChangeRequest

        org, comm, users = make_tenant(db, "Acme Trial", state="suspended")
        db.add(cm.CommercialSubscription(
            id=f"sub_{comm.id}", organization_id=comm.id, plan_code="verified",
            state="trial_active",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
        ))
        db.commit()
        out = restore_organization(
            comm.id, OrgStateChangeRequest(), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["commercial_state"] == "trial_active"

    def test_only_super_admin_may_suspend(self):
        """Asserted on the dependency, which every /api/super-admin route uses."""
        from app.auth.service import require_super_admin

        for role in ("employee", "admin", "hr", "owner", "billing_admin"):
            user = models.User(name="x", email=f"{role}@t.test", role=role)
            with pytest.raises(HTTPException) as exc:
                require_super_admin(current_user=user)
            assert exc.value.status_code == 403

    def test_suspend_route_is_gated(self):
        import app.super_admin.router as sar
        from app.auth.service import require_super_admin

        for fn in (sar.suspend_organization, sar.restore_organization,
                   sar.delete_organization):
            deps = [
                d.dependency
                for d in fn.__defaults__ or ()
                if hasattr(d, "dependency")
            ]
            assert require_super_admin in deps, fn.__name__


# ═════════════════════════════════════════════════════════════════════════════
# DELETION
# ═════════════════════════════════════════════════════════════════════════════

class TestDeletionGates:

    def test_active_organization_cannot_be_deleted(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Live", state="active")
        with pytest.raises(HTTPException) as exc:
            delete_organization(
                comm.id, OrgDeleteRequest(confirm_name="Acme Live"), _StubRequest(),
                db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "organization_not_suspended"
        assert db.get(cm.CommercialOrganization, comm.id) is not None

    def test_mistyped_name_is_refused(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Typo", state="suspended")
        with pytest.raises(HTTPException) as exc:
            delete_organization(
                comm.id, OrgDeleteRequest(confirm_name="acme typo"), _StubRequest(),
                db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "confirmation_mismatch"
        assert db.get(cm.CommercialOrganization, comm.id) is not None

    def test_unknown_organization_is_404(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        with pytest.raises(HTTPException) as exc:
            delete_organization(
                "org_does_not_exist", OrgDeleteRequest(confirm_name="x"),
                _StubRequest(), db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 404

    def test_repeated_delete_is_safe(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Repeat", state="suspended")
        comm_id = comm.id  # the instance is detached once its row is gone
        delete_organization(
            comm_id, OrgDeleteRequest(confirm_name="Acme Repeat"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        # Second call finds nothing — a clear 404, not a partial re-run.
        with pytest.raises(HTTPException) as exc:
            delete_organization(
                comm_id, OrgDeleteRequest(confirm_name="Acme Repeat"), _StubRequest(),
                db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 404

    def test_live_billing_subscription_blocks_deletion(self, db, super_admin):
        """Deleting would CASCADE away provider_subscription_id — the only
        handle to a subscription that keeps charging the customer."""
        org, comm, users = make_tenant(db, "Acme Paid", state="suspended")
        db.add(cm.CommercialSubscription(
            id=f"sub_{comm.id}", organization_id=comm.id, plan_code="governed",
            state="active", provider="stripe", provider_subscription_id="sub_live_123",
        ))
        db.commit()

        with pytest.raises(deletion_service.OrganizationDeletionError) as exc:
            deletion_service.assert_billing_safe(db, comm.id)
        assert exc.value.code == "billing_subscription_active"

    def test_stub_provider_does_not_block(self, db):
        org, comm, users = make_tenant(db, "Acme Stub", state="suspended")
        db.add(cm.CommercialSubscription(
            id=f"sub_{comm.id}", organization_id=comm.id, plan_code="verified",
            state="trial_active", provider="stub", provider_subscription_id=None,
        ))
        db.commit()
        summary = deletion_service.assert_billing_safe(db, comm.id)
        assert summary["provider"] == "stub"

    def test_cancelled_stripe_subscription_does_not_block(self, db):
        org, comm, users = make_tenant(db, "Acme Cancelled", state="suspended")
        db.add(cm.CommercialSubscription(
            id=f"sub_{comm.id}", organization_id=comm.id, plan_code="governed",
            state="cancelled", provider="stripe", provider_subscription_id="sub_old",
        ))
        db.commit()
        summary = deletion_service.assert_billing_safe(db, comm.id)
        assert summary["subscription_state"] == "cancelled"

    def test_missing_operational_link_blocks_deletion(self, db, super_admin):
        """A commercial org with no operational_org_id cannot be reached: its
        users and operational rows would survive while the endpoint reported
        success. Refusing beats half-deleting."""
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        comm = cm.CommercialOrganization(
            id="org_unlinked", name="Unlinked Co", commercial_state="suspended",
            operational_org_id=None,
        )
        db.add(comm)
        db.commit()

        with pytest.raises(HTTPException) as exc:
            delete_organization(
                "org_unlinked", OrgDeleteRequest(confirm_name="Unlinked Co"),
                _StubRequest(), db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "organization_operational_link_missing"
        # Nothing was removed.
        assert db.get(cm.CommercialOrganization, "org_unlinked") is not None

    def test_operational_link_guard_runs_before_anything_destructive(self, db):
        """Asserted on the guard directly so the ordering cannot regress."""
        class _NoLink:
            operational_org_id = None
        with pytest.raises(deletion_service.OrganizationDeletionError) as exc:
            deletion_service.assert_operational_link(_NoLink())
        assert exc.value.code == "organization_operational_link_missing"

        class _Linked:
            operational_org_id = 42
        assert deletion_service.assert_operational_link(_Linked()) == 42


class TestDeletionBehaviour:

    def test_deletes_every_scope_of_tenant_data(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Doomed", state="suspended")
        op_id, comm_id = org.id, comm.id

        out = delete_organization(
            comm_id, OrgDeleteRequest(confirm_name="Acme Doomed", reason="test cleanup"),
            _StubRequest(), db=db, super_admin=super_admin,
        )
        assert out["success"] is True
        assert out["rows_deleted"] > 0

        assert db.get(cm.CommercialOrganization, comm_id) is None
        assert db.get(models.Organization, op_id) is None
        for table, col, val in (
            ("users", "organization_id", op_id),
            ("teams", "organization_id", op_id),
            ("clock_sessions", "organization_id", op_id),
            ("activities", "organization_id", op_id),
            ("audit_logs", "organization_id", op_id),
            ("app_settings", "organization_id", op_id),
            ("org_settings", "organization_id", op_id),
            ("leave_policies", "organization_id", op_id),
        ):
            n = db.execute(text(f"SELECT count(*) FROM {table} WHERE {col} = :v"),
                           {"v": val}).scalar()
            assert n == 0, f"{table} still has {n} rows for the deleted tenant"

        n = db.execute(text("SELECT count(*) FROM commercial_events "
                            "WHERE organization_id = :v"), {"v": comm_id}).scalar()
        assert n == 0

    def test_user_scoped_payroll_items_are_removed(self, db, super_admin):
        """P0 regression. `payroll_items` has no organization_id in the real
        schema, so it must be reached through the tenant's users."""
        self._assert_user_scoped_table_purged(db, super_admin, "payroll_items")

    def test_user_scoped_integrity_scores_are_removed(self, db, super_admin):
        """P0 regression — see test_user_scoped_payroll_items_are_removed."""
        self._assert_user_scoped_table_purged(db, super_admin, "integrity_scores")

    def test_user_scoped_intelligence_summaries_are_removed(self, db, super_admin):
        """P0 regression — see test_user_scoped_payroll_items_are_removed."""
        self._assert_user_scoped_table_purged(
            db, super_admin, "daily_intelligence_summaries")

    @staticmethod
    def _assert_user_scoped_table_purged(db, super_admin, table):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        name = f"Acme {table}"
        org, comm, users = make_tenant(db, name, state="suspended")
        comm_id = comm.id
        uids = [u.id for u in users]

        before = db.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id IN ({','.join(map(str, uids))})")
        ).scalar()
        assert before > 0, f"fixture wrote no {table} rows — the test proves nothing"

        out = delete_organization(
            comm_id, OrgDeleteRequest(confirm_name=name), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["success"] is True
        # Nothing may be skipped: a skip here means the manifest is wrong again.
        assert out["skipped_tables"] == []

        after = db.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id IN ({','.join(map(str, uids))})")
        ).scalar()
        assert after == 0, f"{table} still has {after} rows for the deleted tenant"
        assert table in out["deleted_rows"], (
            f"{table} was not reported as deleted — it is probably not in "
            "USER_SCOPED_TABLES"
        )

    def test_other_tenants_are_untouched(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org_a, comm_a, users_a = make_tenant(db, "Doomed Co", state="suspended")
        org_b, comm_b, users_b = make_tenant(db, "Survivor Co", state="active")

        before = {
            t: db.execute(text(f"SELECT count(*) FROM {t} WHERE organization_id = :v"),
                          {"v": org_b.id}).scalar()
            for t in ("users", "teams", "clock_sessions", "activities",
                      "audit_logs", "app_settings", "org_settings", "leave_policies")
        }
        assert all(v > 0 for v in before.values())

        delete_organization(
            comm_a.id, OrgDeleteRequest(confirm_name="Doomed Co"), _StubRequest(),
            db=db, super_admin=super_admin,
        )

        after = {
            t: db.execute(text(f"SELECT count(*) FROM {t} WHERE organization_id = :v"),
                          {"v": org_b.id}).scalar()
            for t in before
        }
        assert after == before
        assert db.get(models.Organization, org_b.id) is not None
        assert db.get(cm.CommercialOrganization, comm_b.id) is not None

    def test_super_admin_account_survives(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Purge SA", state="suspended")
        delete_organization(
            comm.id, OrgDeleteRequest(confirm_name="Acme Purge SA"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        # organization_id IS NULL, so the org-scoped delete can never match it.
        assert db.get(models.User, super_admin.id) is not None

    def test_platform_audit_row_survives_the_purge(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Audited", state="suspended")
        comm_id = comm.id
        delete_organization(
            comm_id, OrgDeleteRequest(confirm_name="Acme Audited", reason="why"),
            _StubRequest(), db=db, super_admin=super_admin,
        )
        row = (db.query(models.AuditLog)
                 .filter(models.AuditLog.action == "OrgDeleted")
                 .order_by(models.AuditLog.id.desc()).first())
        assert row is not None
        # NULL org id is what makes it survive — a tenant-scoped row would have
        # deleted itself along with the rest of that tenant's audit trail.
        assert row.organization_id is None
        assert row.payload["organization_id"] == comm_id
        assert row.payload["name"] == "Acme Audited"
        assert row.payload["reason"] == "why"

    def test_empty_tenant_can_be_deleted(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org = models.Organization(name="Bare Co", is_active=True)
        db.add(org)
        db.flush()
        comm = cm.CommercialOrganization(
            id="org_bare", name="Bare Co", commercial_state="suspended",
            operational_org_id=org.id,
        )
        db.add(comm)
        db.commit()
        op_id = org.id

        out = delete_organization(
            "org_bare", OrgDeleteRequest(confirm_name="Bare Co"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["success"] is True
        db.expunge_all()
        assert db.get(models.Organization, op_id) is None

    def test_failure_rolls_everything_back(self, db, super_admin, monkeypatch):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Rollback", state="suspended")
        op_id, comm_id = org.id, comm.id
        users_before = db.execute(
            text("SELECT count(*) FROM users WHERE organization_id = :v"), {"v": op_id}
        ).scalar()

        real_purge = deletion_service.purge_organization

        def _explode(session, organization):
            # Delete a good chunk, THEN fail — proving the rollback undoes work
            # already done rather than merely skipping it.
            result = real_purge(session, organization)
            raise RuntimeError("simulated failure after partial delete")

        monkeypatch.setattr(
            "app.super_admin.router.deletion_service.purge_organization", _explode
        )

        with pytest.raises(HTTPException) as exc:
            delete_organization(
                comm_id, OrgDeleteRequest(confirm_name="Acme Rollback"),
                _StubRequest(), db=db, super_admin=super_admin,
            )
        assert exc.value.status_code == 500
        assert exc.value.detail["code"] == "deletion_failed"

        assert db.get(cm.CommercialOrganization, comm_id) is not None
        assert db.get(models.Organization, op_id) is not None
        assert db.execute(
            text("SELECT count(*) FROM users WHERE organization_id = :v"), {"v": op_id}
        ).scalar() == users_before

    def test_deleted_tenant_users_cannot_authenticate(self, db, super_admin):
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        org, comm, users = make_tenant(db, "Acme Ghost", state="suspended")
        op_id = org.id
        ghost = models.User(name="Ghost", email="g@ghost.test", role="employee",
                            organization_id=op_id, is_active=True)

        delete_organization(
            comm.id, OrgDeleteRequest(confirm_name="Acme Ghost"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        lifecycle.invalidate(op_id)
        # A token minted before the purge resolves to an organization that no
        # longer exists — which the gate treats as denied, not as absent.
        with pytest.raises(HTTPException) as exc:
            lifecycle.assert_active(db, ghost)
        assert exc.value.status_code == 403


class TestManifestCoverage:

    def test_every_tenant_keyed_table_is_accounted_for(self, db):
        """The manifest must not silently miss a table added by a migration.

        This is the guard that makes a hand-written delete list safe: a new
        tenant-keyed table fails here rather than quietly orphaning rows.
        """
        report = deletion_service.coverage_report(db)
        assert report["unhandled_organization_id"] == [], (
            "tables with organization_id that the purge neither deletes nor "
            "explicitly keeps: " + ", ".join(report["unhandled_organization_id"])
        )
        assert report["unhandled_tenant_id"] == [], (
            "tables with tenant_id that the purge neither deletes nor "
            "explicitly keeps: " + ", ".join(report["unhandled_tenant_id"])
        )

    def test_no_table_is_left_unclassified(self, db):
        """Closes the blind spot that hid six legacy tables.

        The original report only inspected tables that already carried a tenant
        key, so an untenanted table could never be flagged. Every table must now
        be named somewhere — even if the answer is "leave it alone".
        """
        report = deletion_service.coverage_report(db)
        assert report["unclassified"] == [], (
            "tables with no classification at all: "
            + ", ".join(report["unclassified"])
        )

    def test_legacy_untenanted_tables_are_explicitly_classified(self):
        """The six tables the audit found invisible must stay named."""
        for table in ("chats", "conversations", "new_prompts",
                      "email_rate_limits", "activity", "departments"):
            assert table in deletion_service.LEGACY_UNTENANTED_TABLES, (
                f"{table} must be explicitly classified, not silently ignored"
            )
            reason = deletion_service.LEGACY_UNTENANTED_TABLES[table]
            assert reason and len(reason) > 15, f"{table} has no real reason"

    def test_legacy_untenanted_tables_are_never_deleted(self):
        """There is no safe tenant key for them, so they must not be swept."""
        deleted = (set(deletion_service.OPERATIONAL_TABLES)
                   | set(deletion_service.COMMERCIAL_TABLES)
                   | set(deletion_service.OFFBOARDING_TABLES)
                   | set(deletion_service.USER_SCOPED_TABLES)
                   | set(deletion_service.DELETED_BY_NAME))
        overlap = deleted & set(deletion_service.LEGACY_UNTENANTED_TABLES)
        assert not overlap, f"legacy untenanted tables marked for deletion: {overlap}"

    def test_kept_tables_all_state_a_reason(self):
        for table, reason in {**deletion_service.KEEP_TABLES,
                              **deletion_service.GLOBAL_TABLES,
                              **deletion_service.LEGACY_UNTENANTED_TABLES,
                              **deletion_service.DELETED_BY_NAME}.items():
            assert reason and len(reason) > 15, f"{table} has no real reason"

    def test_global_tables_are_never_in_a_delete_list(self):
        deleted = (set(deletion_service.OPERATIONAL_TABLES)
                   | set(deletion_service.COMMERCIAL_TABLES)
                   | set(deletion_service.OFFBOARDING_TABLES)
                   | set(deletion_service.USER_SCOPED_TABLES))
        overlap = deleted & set(deletion_service.GLOBAL_TABLES)
        assert not overlap, f"global tables marked for deletion: {overlap}"

    def test_no_manifest_table_is_missing_its_scope_column(self, db):
        """The P0 in one assertion.

        `payroll_items`, `integrity_scores` and `daily_intelligence_summaries`
        were listed as organization_id-scoped because their ORM models declare
        that column, but no migration ever added it — so every delete raised
        UndefinedColumn and rolled back. This asserts no manifest entry is
        filtered on a column the connected schema does not have.
        """
        report = deletion_service.coverage_report(db)
        assert report["scope_column_missing"] == [], (
            "manifest tables filtered on a column this database does not have: "
            + ", ".join(report["scope_column_missing"])
        )

    def test_the_three_p0_tables_are_user_scoped_not_org_scoped(self):
        """Dialect-independent statement of the fix.

        The SQLite test schema is built from the ORM models, which DO declare
        `organization_id`, so a column-existence check cannot catch a
        re-introduction here. This asserts the classification itself.
        """
        for table in ("payroll_items", "integrity_scores",
                      "daily_intelligence_summaries"):
            assert table in deletion_service.USER_SCOPED_TABLES, (
                f"{table} must be purged through the tenant's users — the real "
                "schema has no organization_id on it"
            )
            assert table not in deletion_service.OPERATIONAL_TABLES, (
                f"{table} would raise UndefinedColumn against the real database"
            )

    def test_scope_manifest_covers_every_swept_table(self):
        swept = (set(deletion_service.OPERATIONAL_TABLES)
                 | set(deletion_service.COMMERCIAL_TABLES)
                 | set(deletion_service.OFFBOARDING_TABLES)
                 | set(deletion_service.USER_SCOPED_TABLES))
        assert {t for t, _c in deletion_service.scope_manifest()} == swept

    def test_a_table_with_a_missing_scope_column_is_skipped_not_fatal(
        self, db, super_admin, monkeypatch
    ):
        """The hardening backstop.

        Simulates the production condition (a manifest table whose scope column
        is absent) and asserts the purge completes, reports the skip, and does
        NOT raise — instead of rolling the whole transaction back.
        """
        from app.super_admin.router import delete_organization, OrgDeleteRequest

        real = deletion_service._missing_scope_columns
        monkeypatch.setattr(
            deletion_service, "_missing_scope_columns",
            lambda session, present: real(session, present) + [("teams", "organization_id")],
        )

        org, comm, users = make_tenant(db, "Acme Skip", state="suspended")
        op_id, comm_id = org.id, comm.id

        out = delete_organization(
            comm_id, OrgDeleteRequest(confirm_name="Acme Skip"), _StubRequest(),
            db=db, super_admin=super_admin,
        )
        assert out["success"] is True
        assert "teams" in out["skipped_tables"]
        assert "teams" not in out["deleted_rows"]
        # The skipped table's rows survive — visible, not silently lost.
        assert db.execute(
            text("SELECT count(*) FROM teams WHERE organization_id = :v"), {"v": op_id}
        ).scalar() == 1

    def test_missing_tables_are_skipped_not_fatal(self, db, super_admin):
        """The SQLite test schema has far fewer tables than production; the
        purge must skip what is absent rather than raise."""
        org, comm, users = make_tenant(db, "Acme Sparse", state="suspended")
        result = deletion_service.purge_organization(db, comm)
        db.commit()
        assert result.total_rows > 0
