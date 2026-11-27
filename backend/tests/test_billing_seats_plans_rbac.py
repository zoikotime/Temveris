"""Billing corrections confirmed in ZT-COM-BILL-001 (Commercial Billing Standard).

Covers three of the four confirmed corrections:

* §H3 / §A4 — billable seat definition (pending invites are not billable)
* §13 / §J1 — canonical plan price catalog
* §K7 / §15 — billing RBAC

Trial entitlements (§K3) are covered in ``test_entitlements.py`` alongside the
existing resolver tests.
"""

from types import SimpleNamespace

import pytest

from app.billing import router as billing_router
from app.billing import service as billing_service
from app.billing import worker_state
from database.models import BillingPlan, User


# ══════════════════════════════════════════════════════════════════════════════
# §H3 / §A4 / §17 — billable seats driven by users.billing_state
# ══════════════════════════════════════════════════════════════════════════════
ORG = 4101          # arbitrary org ids, isolated from any fixture data
OTHER_ORG = 4102


def _worker(db, *, org, email, state):
    """A worker in an explicit billing state.

    is_active/is_invited are set to values that would give the *wrong* answer if
    anything still consulted them — is_active=False with a billable state, and
    is_invited=True on an active one. Billability must come only from
    billing_state.
    """
    u = User(
        name=email.split("@")[0],
        email=email,
        organization_id=org,
        billing_state=state,
        is_active=(state != worker_state.DEACTIVATED),
        is_invited=True,
        role="employee",
    )
    db.add(u)
    db.flush()
    return u


def test_active_billable_counts(db):
    _worker(db, org=ORG, email="active@seats.test", state=worker_state.ACTIVE_BILLABLE)
    assert billing_service.count_billable_seats(db, ORG) == 1


def test_suspended_billable_counts(db):
    """§A4 — suspension is temporary access control; the seat stays paid."""
    _worker(db, org=ORG, email="susp@seats.test", state=worker_state.SUSPENDED_BILLABLE)
    assert billing_service.count_billable_seats(db, ORG) == 1


@pytest.mark.parametrize("state", [
    worker_state.INVITED_PENDING,
    worker_state.DEACTIVATED,
    worker_state.TERMINATED,
    worker_state.ARCHIVED,
    worker_state.MIGRATION_REVIEW,
])
def test_non_billable_states_do_not_count(db, state):
    _worker(db, org=ORG, email=f"{state.lower()}@seats.test", state=state)
    assert billing_service.count_billable_seats(db, ORG) == 0


def test_null_billing_state_is_not_billable(db):
    """A row the backfill has not reached must never be charged."""
    _worker(db, org=ORG, email="unbackfilled@seats.test", state=None)
    assert billing_service.count_billable_seats(db, ORG) == 0


def test_is_invited_flag_does_not_affect_billability(db):
    """The production defect: is_invited is not cleared on acceptance, so 21
    working employees looked 'pending'. ACTIVE_BILLABLE must win regardless."""
    u = _worker(db, org=ORG, email="stale-flag@seats.test", state=worker_state.ACTIVE_BILLABLE)
    assert u.is_invited is True
    assert billing_service.count_billable_seats(db, ORG) == 1


def test_billable_seats_mix(db):
    for i, st in enumerate([
        worker_state.ACTIVE_BILLABLE, worker_state.ACTIVE_BILLABLE,
        worker_state.SUSPENDED_BILLABLE,
        worker_state.INVITED_PENDING, worker_state.INVITED_PENDING,
        worker_state.DEACTIVATED, worker_state.TERMINATED,
        worker_state.ARCHIVED, worker_state.MIGRATION_REVIEW,
    ]):
        _worker(db, org=ORG, email=f"mix{i}@seats.test", state=st)
    assert billing_service.count_billable_seats(db, ORG) == 3     # 2 active + 1 suspended
    assert billing_service.count_reserved_seats(db, ORG) == 2     # invites reserve, not billed


def test_seat_count_never_includes_another_organization(db):
    """Regression guard for the defect that billed one tenant for every user on
    the platform."""
    _worker(db, org=ORG, email="mine@seats.test", state=worker_state.ACTIVE_BILLABLE)
    for i in range(5):
        _worker(db, org=OTHER_ORG, email=f"theirs{i}@seats.test",
                state=worker_state.ACTIVE_BILLABLE)

    assert billing_service.count_billable_seats(db, ORG) == 1
    assert billing_service.count_billable_seats(db, OTHER_ORG) == 5


def test_checkout_quantity_uses_only_this_organizations_seats(db):
    """/upgrade computes the Stripe line-item quantity from this helper, so the
    quantity can never include another tenant's workers."""
    for i in range(3):
        _worker(db, org=ORG, email=f"q{i}@seats.test", state=worker_state.ACTIVE_BILLABLE)
    for i in range(9):
        _worker(db, org=OTHER_ORG, email=f"other{i}@seats.test",
                state=worker_state.ACTIVE_BILLABLE)

    quantity = max(billing_service.count_billable_seats(db, ORG), 1)
    assert quantity == 3


# ── §4 — no shared-default organization ──────────────────────────────────────
def test_seat_count_requires_an_organization(db):
    with pytest.raises(billing_service.MissingOrganization):
        billing_service.count_billable_seats(db, None)


def test_ensure_subscription_requires_an_organization(db):
    with pytest.raises(billing_service.MissingOrganization):
        billing_service.ensure_subscription(db, None)


def test_no_default_org_sentinel_exists():
    """The 'default' sentinel must not come back — it is what made every tenant
    share one subscription row."""
    assert not hasattr(billing_service, "DEFAULT_ORG_ID")


def test_require_org_id_rejects_none_and_normalises_ints():
    assert billing_service.require_org_id(7) == 7
    assert billing_service.require_org_id("7") == 7
    with pytest.raises(billing_service.MissingOrganization):
        billing_service.require_org_id(None)


# ══════════════════════════════════════════════════════════════════════════════
# §13 / §J1 — canonical price catalog
# ══════════════════════════════════════════════════════════════════════════════
# (monthly, annual, user_limit). Two approved changes are recorded here, both of which
# these tests correctly caught when made — which is what they exist for:
#   2026-08-14  Governed ceiling 100 -> 250
#   2026-08-21  annual prices aligned to temveris.example/pricing, which advertises
#               $6.40 / $12.00 / $20.00 per user per month billed annually and a flat
#               "Save 20%" badge. The catalog held $79 / $149, so a customer was quoted
#               one price and charged a higher one. The published price wins.
CANONICAL = {
    "verified":  (8.0, 76.80, 25),
    "governed":  (15.0, 144.0, 250),
    "sovereign": (25.0, 240.0, None),
}


@pytest.fixture
def plans_table(engine, db):
    """Clean billing_plans around each test.

    ``ensure_plans`` commits, and the shared ``db`` fixture only rolls back, so
    committed rows would otherwise leak into the next test and collide on the
    unique ``code`` constraint.
    """
    BillingPlan.__table__.create(engine, checkfirst=True)
    db.query(BillingPlan).delete()
    db.commit()
    yield
    db.query(BillingPlan).delete()
    db.commit()


def test_seed_catalog_matches_confirmed_standard():
    """The in-code catalog is the canonical source (§J1) — assert it directly so
    a drifted seed value fails the build rather than reaching customers."""
    seeds = {p["code"]: p for p in billing_service.SEED_PLANS}
    for code, (monthly, annual, cap) in CANONICAL.items():
        assert seeds[code]["monthly_price_per_user"] == monthly, code
        assert seeds[code]["annual_price_per_user"] == annual, code
        assert seeds[code]["user_limit"] == cap, code


def test_ensure_plans_inserts_canonical_values(db, plans_table):
    billing_service.ensure_plans(db)
    rows = {p.code: p for p in db.query(BillingPlan).all()}
    for code, (monthly, annual, cap) in CANONICAL.items():
        assert rows[code].monthly_price_per_user == monthly, code
        assert rows[code].annual_price_per_user == annual, code
        assert rows[code].user_limit == cap, code


def test_ensure_plans_corrects_drifted_prices(db, plans_table):
    """The exact production drift: Verified $9 / $86.40 and Governed annual $144.

    Previously ensure_plans() skipped existing rows entirely, which is how those
    values survived. It must now reconcile them (§J1).
    """
    db.add(BillingPlan(code="verified", name="Verified", tier_order=1,
                       monthly_price_per_user=9.0, annual_price_per_user=86.4,
                       user_limit=25))
    db.add(BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=149.0,
                       user_limit=100))
    db.flush()

    billing_service.ensure_plans(db)

    rows = {p.code: p for p in db.query(BillingPlan).all()}
    assert rows["verified"].monthly_price_per_user == 8.0
    assert rows["verified"].annual_price_per_user == 76.80
    assert rows["governed"].annual_price_per_user == 144.0
    assert rows["governed"].user_limit == 250


def test_ensure_plans_preserves_descriptive_edits(db, plans_table):
    """Only price and cap are canonical — copy stays editable in the DB."""
    db.add(BillingPlan(code="verified", name="Verified", tier_order=1,
                       monthly_price_per_user=9.0, annual_price_per_user=86.4,
                       user_limit=25, badge="Custom Badge",
                       short_description="Locally edited copy"))
    db.flush()

    billing_service.ensure_plans(db)

    row = db.query(BillingPlan).filter_by(code="verified").one()
    assert row.monthly_price_per_user == 8.0          # corrected
    assert row.badge == "Custom Badge"                # preserved
    assert row.short_description == "Locally edited copy"


def test_ensure_plans_is_idempotent(db, plans_table):
    billing_service.ensure_plans(db)
    billing_service.ensure_plans(db)
    assert db.query(BillingPlan).filter_by(code="verified").count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# §K7 / §15 — billing RBAC
# ══════════════════════════════════════════════════════════════════════════════
def _user(role):
    return SimpleNamespace(role=role, id=1, name="t", email="t@t.test")


@pytest.mark.parametrize("role", ["owner", "billing_admin"])
def test_owner_and_billing_admin_may_view_and_manage(role):
    billing_router._require_view(_user(role))
    billing_router._require_manage(_user(role))


def test_admin_may_view_and_manage_documented_deviation():
    """DOCUMENTED DEVIATION from §15, approved 2026-08-12.

    §15 says Admin views billing but never modifies it. "admin" is nonetheless in
    the manage set because the doctrine's model is unreachable in production: there
    are 0 owner/billing_admin users, and the frontend AdminRoute admits only
    ["admin", "hr"] — so granting someone "owner" would lock them out of the admin
    area. With this deviation, mistaken spend is prevented at the *tenant* level
    (_require_billable_tenant) rather than the role level.

    If the role model later gains real owner/billing_admin users, revert this and
    restore the original assertion.
    """
    billing_router._require_view(_user("admin"))
    billing_router._require_manage(_user("admin"))      # no longer raises


@pytest.mark.parametrize("guard", ["_require_view", "_require_manage"])
def test_hr_has_no_billing_access(guard):
    """§K7 — HR must not reach billing at all."""
    with pytest.raises(Exception) as exc:
        getattr(billing_router, guard)(_user("hr"))
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.parametrize("guard", ["_require_view", "_require_manage"])
def test_employee_has_no_billing_access(guard):
    with pytest.raises(Exception) as exc:
        getattr(billing_router, guard)(_user("employee"))
    assert getattr(exc.value, "status_code", None) == 403


def test_manage_role_set_is_least_privilege():
    """Guard against a future edit quietly widening who can spend money.

    The set is pinned exactly, so adding a role is a deliberate, reviewed change
    rather than an accident. "admin" is present by the documented §15 deviation
    above — see test_admin_may_view_and_manage_documented_deviation.
    """
    assert billing_router.BILLING_MANAGE_ROLES == {"owner", "billing_admin", "admin"}
    # Non-negotiable regardless of the deviation: these roles never touch billing.
    assert "hr" not in billing_router.BILLING_VIEW_ROLES
    assert "hr" not in billing_router.BILLING_MANAGE_ROLES
    assert "employee" not in billing_router.BILLING_VIEW_ROLES
    assert "employee" not in billing_router.BILLING_MANAGE_ROLES
    assert "super_admin" not in billing_router.BILLING_MANAGE_ROLES
    # Every role that can manage must also be able to view.
    assert billing_router.BILLING_MANAGE_ROLES <= billing_router.BILLING_VIEW_ROLES
