"""Setup-wizard state semantics.

The load-bearing decision under test: **absence of state means "not in setup"**.

Every organization that existed before this feature — every super-admin
provisioned tenant, and every self-serve tenant created before the wizard — has
no state row. If a missing row implied "start at step 1", first-login routing
would drop working admins into a setup flow for an organization they have been
running for months. Only an explicit ``begin()`` opens the wizard.
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

from database import database as _db     # noqa: E402
from database import models              # noqa: E402
from app.org import setup_state          # noqa: E402


@pytest.fixture
def db():
    """Fresh engine per test — state is per-organization and order-sensitive."""
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = e
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=e)
    for t in (models.Organization, models.User, models.AppSettings, models.Team,
              models.OrgSettings, models.AuditLog, models.LeavePolicy):
        t.__table__.create(e, checkfirst=True)
    s = _db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def org(db):
    o = models.Organization(name="Wizard Co")
    db.add(o)
    db.commit()
    return o


# ═════════════════════════════════════════════════════════════════════════════
# Backward compatibility — the critical property
# ═════════════════════════════════════════════════════════════════════════════

class TestExistingTenantsAreNeverSentToTheWizard:

    def test_org_with_no_state_row_does_not_require_setup(self, db, org):
        state = setup_state.get_state(db, org.id)
        assert state["setup_required"] is False
        assert setup_state.setup_required(db, org.id) is False

    def test_super_admin_provisioned_tenant_is_unaffected(self, db):
        """Super-admin provisioning never calls begin(), so no wizard."""
        from app.org.provisioning_service import provision_tenant, CREATED_VIA_SUPER_ADMIN

        actor = models.User(name="SA", email="sa@ex.com", role="super_admin",
                            organization_id=None, is_active=True)
        db.add(actor)
        db.flush()

        result = provision_tenant(
            db, org_name="Contract Co", admin_email="admin@contract.com",
            created_via=CREATED_VIA_SUPER_ADMIN, actor_user_id=actor.id,
            seed_leave_policies=False, create_commercial_org=False,
        )
        db.commit()
        assert setup_state.setup_required(db, result.organization.id) is False

    def test_org_less_caller_does_not_require_setup(self, db):
        assert setup_state.setup_required(db, None) is False


# ═════════════════════════════════════════════════════════════════════════════
# Wizard lifecycle
# ═════════════════════════════════════════════════════════════════════════════

class TestWizardLifecycle:

    def test_begin_opens_the_wizard_at_step_one(self, db, org):
        state = setup_state.begin(db, org.id)
        db.commit()
        assert state["setup_required"] is True
        assert state["current_step"] == setup_state.STEP_ORGANIZATION
        assert state["completed"] is False

    def test_begin_is_idempotent(self, db, org):
        setup_state.begin(db, org.id)
        setup_state.set_step(db, org.id, setup_state.STEP_TEAMS)
        db.commit()
        # A second begin() must not rewind an in-progress wizard.
        state = setup_state.begin(db, org.id)
        assert state["current_step"] == setup_state.STEP_TEAMS

    def test_steps_advance_in_order(self, db, org):
        setup_state.begin(db, org.id)
        assert setup_state.set_step(db, org.id, setup_state.STEP_TEAMS)["current_step"] \
            == setup_state.STEP_TEAMS
        db.commit()

    def test_progress_never_moves_backwards(self, db, org):
        """A stale client, a refresh or a back-navigation must not rewind."""
        setup_state.begin(db, org.id)
        setup_state.set_step(db, org.id, setup_state.STEP_TEAMS)
        db.commit()
        state = setup_state.set_step(db, org.id, setup_state.STEP_ORGANIZATION)
        assert state["current_step"] == setup_state.STEP_TEAMS

    def test_duplicate_step_submission_is_a_noop(self, db, org):
        setup_state.begin(db, org.id)
        setup_state.set_step(db, org.id, setup_state.STEP_TEAMS)
        first = setup_state.set_step(db, org.id, setup_state.STEP_TEAMS)
        db.commit()
        assert first["current_step"] == setup_state.STEP_TEAMS
        assert first["completed"] is False

    def test_completion_is_terminal_and_idempotent(self, db, org):
        setup_state.begin(db, org.id)
        done = setup_state.mark_complete(db, org.id)
        db.commit()
        assert done["completed"] is True
        assert done["setup_required"] is False
        stamp = done["completed_at"]

        again = setup_state.mark_complete(db, org.id)
        assert again["completed_at"] == stamp, "completion timestamp was overwritten"

    def test_a_completed_wizard_cannot_be_reopened(self, db, org):
        setup_state.begin(db, org.id)
        setup_state.mark_complete(db, org.id)
        db.commit()
        state = setup_state.set_step(db, org.id, setup_state.STEP_ORGANIZATION)
        assert state["completed"] is True
        assert state["setup_required"] is False
        assert setup_state.begin(db, org.id)["completed"] is True

    def test_setting_complete_via_set_step_marks_it_complete(self, db, org):
        setup_state.begin(db, org.id)
        state = setup_state.set_step(db, org.id, setup_state.STEP_COMPLETE)
        db.commit()
        assert state["completed"] is True
        assert state["completed_at"] is not None

    def test_unknown_step_is_rejected(self, db, org):
        setup_state.begin(db, org.id)
        with pytest.raises(ValueError):
            setup_state.set_step(db, org.id, "teleport")

    def test_corrupt_stored_step_falls_back_to_the_start(self, db, org):
        setup_state.begin(db, org.id)
        db.commit()
        row = (
            db.query(models.AppSettings)
              .filter(models.AppSettings.organization_id == org.id,
                      models.AppSettings.key == setup_state.KEY_CURRENT_STEP)
              .one()
        )
        row.value = "garbage-from-somewhere"
        db.commit()
        state = setup_state.get_state(db, org.id)
        assert state["current_step"] == setup_state.STEP_ORGANIZATION
        assert state["setup_required"] is True


# ═════════════════════════════════════════════════════════════════════════════
# Tenant scoping
# ═════════════════════════════════════════════════════════════════════════════

class TestWizardStateIsTenantScoped:

    def test_one_tenants_progress_does_not_affect_another(self, db):
        a = models.Organization(name="A Co")
        b = models.Organization(name="B Co")
        db.add_all([a, b])
        db.commit()

        setup_state.begin(db, a.id)
        setup_state.set_step(db, a.id, setup_state.STEP_TEAMS)
        db.commit()

        assert setup_state.get_state(db, a.id)["current_step"] == setup_state.STEP_TEAMS
        assert setup_state.setup_required(db, b.id) is False

    def test_completing_one_tenant_does_not_complete_another(self, db):
        a = models.Organization(name="A2 Co")
        b = models.Organization(name="B2 Co")
        db.add_all([a, b])
        db.commit()
        setup_state.begin(db, a.id)
        setup_state.begin(db, b.id)
        setup_state.mark_complete(db, a.id)
        db.commit()

        assert setup_state.get_state(db, a.id)["completed"] is True
        assert setup_state.get_state(db, b.id)["completed"] is False
        assert setup_state.get_state(db, b.id)["setup_required"] is True


# ═════════════════════════════════════════════════════════════════════════════
# Storage choice
# ═════════════════════════════════════════════════════════════════════════════

class TestStorageDoesNotLeakIntoUserFacingSettings:

    def test_state_is_stored_at_org_scope(self, db, org):
        setup_state.begin(db, org.id)
        db.commit()
        row = (
            db.query(models.AppSettings)
              .filter(models.AppSettings.organization_id == org.id,
                      models.AppSettings.key == setup_state.KEY_CURRENT_STEP)
              .one()
        )
        # Org level == team_key IS NULL AND user_id IS NULL, per the cascade.
        assert row.team_key is None
        assert row.user_id is None
        assert row.organization_id == org.id

    def test_keys_are_absent_from_the_settings_allow_list(self):
        """Wizard state must not surface as a toggle in Organization Settings."""
        from app.settings.scope import KEY_META
        assert setup_state.KEY_CURRENT_STEP not in KEY_META
        assert setup_state.KEY_COMPLETED_AT not in KEY_META

    def test_the_activation_checklist_is_not_overloaded(self):
        """The commercial checklist keeps its own, separate meaning."""
        from app.commercial.models import OnboardingChecklist
        cols = set(OnboardingChecklist.__table__.columns.keys())
        assert "setup_current_step" not in cols
        assert "setup_completed_at" not in cols
