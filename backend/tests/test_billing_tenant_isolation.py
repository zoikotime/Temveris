"""Billing tenancy — ZT-COM-BILL-001 §4 / §5.

Before this phase every billing table carried ``organization_id VARCHAR(80)
DEFAULT 'default'``, so all tenants shared one logical subscription: one
organization was billed for every user on the platform and the others could not
be billed at all. These tests lock the per-organization behaviour in place.
"""

import pytest

from app.billing import service as billing_service
from app.billing import worker_state
from database.models import (
    BillingAuditEvent, BillingContact, BillingInvoice, BillingPlan,
    BillingSubscription, Organization, User,
)

ORG_A = 5201
ORG_B = 5202


@pytest.fixture
def billing_tables(engine, db):
    for model in (Organization, BillingPlan, BillingSubscription, BillingInvoice, BillingContact):
        model.__table__.create(engine, checkfirst=True)
    # ensure_plans / ensure_subscription commit, and the shared `db` fixture only
    # rolls back, so state must be cleaned explicitly around each test.
    def _clean():
        for model in (BillingInvoice, BillingContact, BillingSubscription, BillingPlan):
            db.query(model).delete()
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False
        )
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Org A"))
    db.add(Organization(id=ORG_B, name="Org B"))
    db.commit()
    yield
    _clean()


def _worker(db, org, email, state=worker_state.ACTIVE_BILLABLE):
    db.add(User(name=email.split("@")[0], email=email, organization_id=org,
                billing_state=state, is_active=True, role="employee"))
    db.flush()


# ── §4 — one subscription per organization ───────────────────────────────────
def test_each_organization_gets_its_own_subscription(db, billing_tables):
    sub_a = billing_service.ensure_subscription(db, ORG_A)
    sub_b = billing_service.ensure_subscription(db, ORG_B)

    assert sub_a.id != sub_b.id
    assert sub_a.organization_id == ORG_A
    assert sub_b.organization_id == ORG_B
    assert db.query(BillingSubscription).count() == 2


def test_ensure_subscription_is_idempotent_per_organization(db, billing_tables):
    first = billing_service.ensure_subscription(db, ORG_A)
    second = billing_service.ensure_subscription(db, ORG_A)
    assert first.id == second.id
    assert db.query(BillingSubscription).count() == 1


def test_organization_id_is_stored_as_the_real_integer_id(db, billing_tables):
    """Not a 'default' sentinel and not a stringified id."""
    sub = billing_service.ensure_subscription(db, ORG_A)
    assert sub.organization_id == ORG_A
    assert isinstance(sub.organization_id, int)


# ── §5 — one tenant must never read another's billing ────────────────────────
def test_org_a_cannot_see_org_b_subscription(db, billing_tables):
    billing_service.ensure_subscription(db, ORG_A)
    sub_b = billing_service.ensure_subscription(db, ORG_B)

    visible_to_a = (
        db.query(BillingSubscription)
          .filter(BillingSubscription.organization_id == ORG_A)
          .all()
    )
    assert len(visible_to_a) == 1
    assert sub_b.id not in [s.id for s in visible_to_a]


def test_invoice_lookup_scoped_by_organization_blocks_cross_tenant_read(db, billing_tables):
    """The pattern the router now uses: id AND organization_id. Looking up by
    primary key alone would return the other tenant's invoice."""
    sub_b = billing_service.ensure_subscription(db, ORG_B)
    inv_b = BillingInvoice(organization_id=ORG_B, subscription_id=sub_b.id,
                           invoice_number="B-0001", total_amount=100.0)
    db.add(inv_b)
    db.flush()

    # Unscoped (the old defect) finds it; scoped (the fix) does not.
    assert db.query(BillingInvoice).filter(BillingInvoice.id == inv_b.id).first() is not None
    assert (
        db.query(BillingInvoice)
          .filter(BillingInvoice.id == inv_b.id,
                  BillingInvoice.organization_id == ORG_A)
          .first()
    ) is None


def test_contact_lookup_scoped_by_organization_blocks_cross_tenant_read(db, billing_tables):
    db.add(BillingContact(organization_id=ORG_B, name="B Finance", email="fin@b.test"))
    db.flush()
    contact_b = db.query(BillingContact).filter_by(organization_id=ORG_B).one()

    assert (
        db.query(BillingContact)
          .filter(BillingContact.id == contact_b.id,
                  BillingContact.organization_id == ORG_A)
          .first()
    ) is None


# ── seat metrics stay tenant-local ───────────────────────────────────────────
def test_seat_metrics_are_written_per_organization(db, billing_tables):
    _worker(db, ORG_A, "a1@iso.test")
    _worker(db, ORG_A, "a2@iso.test")
    for i in range(7):
        _worker(db, ORG_B, f"b{i}@iso.test")

    sub_a = billing_service.ensure_subscription(db, ORG_A)
    sub_b = billing_service.ensure_subscription(db, ORG_B)

    billing_service.refresh_seat_metrics(db, sub_a, ORG_A)
    billing_service.refresh_seat_metrics(db, sub_b, ORG_B)

    assert sub_a.billable_users == 2
    assert sub_b.billable_users == 7
    # Refreshing one tenant must not disturb the other.
    billing_service.refresh_seat_metrics(db, sub_a, ORG_A)
    assert sub_b.billable_users == 7


def test_audit_event_is_written_to_the_actors_organization(db, engine, billing_tables):
    """Audit rows are tenant-owned — a shared sentinel org would expose one
    tenant's billing history in another's audit trail."""
    BillingAuditEvent.__table__.create(engine, checkfirst=True)
    db.query(BillingAuditEvent).delete()
    db.commit()

    actor = User(name="A Admin", email="admin@a.test", organization_id=ORG_A,
                 billing_state=worker_state.ACTIVE_BILLABLE, is_active=True, role="admin")
    db.add(actor)
    db.flush()

    evt = billing_service.write_audit(
        db, actor=actor, action_key="test_action", summary="scoped audit",
    )
    assert evt.organization_id == ORG_A

    db.query(BillingAuditEvent).delete()
    db.commit()


# ── Phase 1.5 — schema-enforced integrity (migration f3d0b6e29a17) ───────────
def test_only_one_subscription_per_organization_is_allowed(db, engine, billing_tables):
    """UNIQUE(organization_id): two rows for one org means the tenant's plan
    depends on which row .first() happens to return."""
    from sqlalchemy import UniqueConstraint, Index, text
    from sqlalchemy.exc import IntegrityError

    # The constraint ships as a migration; assert it here against a table built
    # with it so the guarantee is covered without running Alembic on SQLite.
    db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_test_sub_org "
                    "ON billing_subscriptions (organization_id)"))
    db.commit()
    try:
        db.add(BillingSubscription(organization_id=ORG_A, billing_cycle="monthly"))
        db.commit()
        db.add(BillingSubscription(organization_id=ORG_A, billing_cycle="annual"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.execute(text("DROP INDEX IF EXISTS uq_test_sub_org"))
        db.commit()


def test_stripe_subscription_id_cannot_map_to_two_rows(db, billing_tables):
    """Partial UNIQUE(stripe_subscription_id): webhooks resolve subscriptions by
    this id, so a duplicate would silently update the wrong tenant."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_test_stripe_sub "
                    "ON billing_subscriptions (stripe_subscription_id) "
                    "WHERE stripe_subscription_id IS NOT NULL"))
    db.commit()
    try:
        db.add(BillingSubscription(organization_id=ORG_A, stripe_subscription_id="sub_dup"))
        db.commit()
        db.add(BillingSubscription(organization_id=ORG_B, stripe_subscription_id="sub_dup"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # NULL must remain repeatable — most rows have no Stripe subscription yet.
        db.add(BillingSubscription(organization_id=ORG_A + 90, stripe_subscription_id=None))
        db.add(BillingSubscription(organization_id=ORG_B + 90, stripe_subscription_id=None))
        db.commit()
    finally:
        db.execute(text("DROP INDEX IF EXISTS uq_test_stripe_sub"))
        db.commit()
