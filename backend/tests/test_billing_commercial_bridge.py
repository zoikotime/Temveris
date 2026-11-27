"""Phase 1 - billing to commercial subscription bridge.

Before this bridge, Stripe wrote to ``billing_subscriptions`` and the entitlement
resolver read ``commercial_organizations`` / ``commercial_subscriptions``, with
nothing joining them. A customer could pay for Governed and keep Verified
entitlements indefinitely. These tests pin the projection and, most importantly,
that entitlements now follow the payment.
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.billing import webhooks as billing_webhooks
from app.commercial import constants, entitlements, ids, manifests
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EntitlementsCache,
)
from app.commercial.subscription_ops import (
    BILLING_TO_COMMERCIAL_STATE, _UNMAPPED_STATUSES, sync_from_billing,
)
from database import models


@pytest.fixture(scope="module", autouse=True)
def _billing_tables(engine):
    """conftest creates only the commercial subset of the schema. This suite
    drives the real billing webhook path, so it needs the operational billing
    tables too. Created here rather than in conftest to keep the shared fixture
    untouched for every other suite."""
    models.BillingPlan.__table__.create(engine, checkfirst=True)
    models.BillingSubscription.__table__.create(engine, checkfirst=True)
    yield


def _tenant(db, *, plan="verified", state="trial_active", linked=True):
    """An operational org plus the commercial rows the resolver reads."""
    op = models.Organization(name=f"Acme {ids.org_id()[:8]}")
    db.add(op)
    db.flush()

    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state=state,
        operational_org_id=op.id if linked else None,
    ))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id,
        plan_code=plan, billing_cycle="monthly", seat_count=10,
        state=state, provider="stub",
        trial_started_at=datetime.now(timezone.utc),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
    ))
    db.flush()
    return op.id, org_id


def _org(db, org_id):
    return db.query(CommercialOrganization).filter_by(id=org_id).one()


def _sub(db, org_id):
    return db.query(CommercialSubscription).filter_by(organization_id=org_id).one()


# -- the mapping itself ------------------------------------------------------
def test_every_mapping_target_is_a_real_commercial_state():
    """A typo here would write a state no state machine recognises."""
    for billing_status, commercial in BILLING_TO_COMMERCIAL_STATE.items():
        assert commercial in constants.COMMERCIAL_STATES, (
            f"{billing_status} -> {commercial} is not a COMMERCIAL_STATE")


def test_every_status_stripe_can_produce_is_accounted_for():
    """Either mapped or *deliberately* unmapped - never silently dropped.

    If a future Stripe status is added to STRIPE_STATUS_MAP without a decision
    about its commercial meaning, this fails rather than letting the projection
    quietly skip it.
    """
    producible = set(billing_webhooks.STRIPE_STATUS_MAP.values()) | {"cancel_pending"}
    unaccounted = producible - set(BILLING_TO_COMMERCIAL_STATE) - _UNMAPPED_STATUSES
    assert not unaccounted, f"no commercial decision recorded for: {sorted(unaccounted)}"


def test_terminal_billing_states_map_to_terminal_commercial_states():
    for status in billing_webhooks.TERMINAL_STATES:
        target = BILLING_TO_COMMERCIAL_STATE.get(status)
        if target is not None:
            assert target in {"cancelled", "closed"}


# -- projection behaviour ----------------------------------------------------
def test_projects_plan_and_state(db):
    op_id, org_id = _tenant(db, plan="verified", state="trial_active")

    changed = sync_from_billing(db, operational_org_id=op_id,
                                billing_status="active", plan_code="governed")

    assert changed["plan_code"] == ("verified", "governed")
    assert changed["commercial_state"] == ("trial_active", "active")
    assert _sub(db, org_id).plan_code == "governed"
    assert _org(db, org_id).commercial_state == "active"


def test_writes_both_org_state_and_subscription_state(db):
    """resolve() reads ``org.commercial_state or sub.state`` - if only the
    primary were written the fallback would silently contradict it."""
    op_id, org_id = _tenant(db, state="trial_active")

    sync_from_billing(db, operational_org_id=op_id, billing_status="past_due")

    assert _org(db, org_id).commercial_state == "past_due"
    assert _sub(db, org_id).state == "past_due"


def test_is_idempotent(db):
    """Webhook redelivery and the confirm-endpoint race both replay this."""
    op_id, _ = _tenant(db, plan="verified", state="trial_active")

    first = sync_from_billing(db, operational_org_id=op_id,
                              billing_status="active", plan_code="governed")
    second = sync_from_billing(db, operational_org_id=op_id,
                               billing_status="active", plan_code="governed")

    assert first, "first call should report changes"
    assert second == {}, "replay must be a no-op, not a second write"


def test_refuses_to_resurrect_a_cancelled_tenant(db):
    """A late or replayed event must not restore access after cancellation."""
    op_id, org_id = _tenant(db, plan="governed", state="cancelled")

    changed = sync_from_billing(db, operational_org_id=op_id, billing_status="active")

    assert changed == {}
    assert _org(db, org_id).commercial_state == "cancelled"


def test_unmapped_status_leaves_the_commercial_row_untouched(db):
    """``incomplete`` means the first payment never finalized. Projecting it
    either way is a business decision, so the row is preserved verbatim."""
    op_id, org_id = _tenant(db, plan="verified", state="trial_active")

    for status in sorted(_UNMAPPED_STATUSES):
        changed = sync_from_billing(db, operational_org_id=op_id, billing_status=status)
        assert changed == {}
        assert _org(db, org_id).commercial_state == "trial_active"
        assert _sub(db, org_id).plan_code == "verified"


def test_plan_still_projects_when_state_is_unmapped(db):
    """The two halves are independent: an ambiguous state must not block a
    known plan change."""
    op_id, org_id = _tenant(db, plan="verified", state="trial_active")

    changed = sync_from_billing(db, operational_org_id=op_id,
                                billing_status="incomplete", plan_code="governed")

    assert changed["plan_code"] == ("verified", "governed")
    assert _org(db, org_id).commercial_state == "trial_active"


def test_tenant_without_commercial_rows_is_not_an_error(db):
    """Internal/super-admin orgs predate the commercial layer - nothing to
    sync, and a webhook must not fail because of them."""
    op = models.Organization(name="Internal")
    db.add(op)
    db.flush()

    assert sync_from_billing(db, operational_org_id=op.id, billing_status="active") is None
    assert sync_from_billing(db, operational_org_id=None, billing_status="active") is None


def test_unlinked_commercial_org_is_skipped(db):
    op_id, org_id = _tenant(db, state="trial_active", linked=False)

    assert sync_from_billing(db, operational_org_id=op_id, billing_status="active") is None
    assert _org(db, org_id).commercial_state == "trial_active"


def test_invalidates_the_entitlements_cache(db):
    """A stale cache entry would keep serving the old plan for the whole TTL -
    the payment would land and entitlements still would not move."""
    op_id, org_id = _tenant(db, plan="verified", state="trial_active")
    entitlements.get(db, org_id)
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none()

    sync_from_billing(db, operational_org_id=op_id,
                      billing_status="active", plan_code="governed")

    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none() is None


def test_no_cache_churn_on_a_no_op_replay(db):
    op_id, org_id = _tenant(db, plan="governed", state="active")
    sync_from_billing(db, operational_org_id=op_id,
                      billing_status="active", plan_code="governed")
    entitlements.get(db, org_id)

    sync_from_billing(db, operational_org_id=op_id,
                      billing_status="active", plan_code="governed")

    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none(), \
        "an unchanged replay must not drop a warm cache entry"


# -- the bug this phase exists to close --------------------------------------
@pytest.mark.parametrize("paid_plan", ["governed", "sovereign"])
def test_entitlements_follow_the_payment(db, paid_plan):
    """End to end: a Verified trial pays for a higher plan, and the resolver
    reports the plan they actually bought."""
    op_id, org_id = _tenant(db, plan="verified", state="trial_active")

    before = entitlements.get(db, org_id)
    assert before["plan"] == "verified"

    sync_from_billing(db, operational_org_id=op_id,
                      billing_status="active", plan_code=paid_plan)

    after = entitlements.get(db, org_id)
    assert after["plan"] == paid_plan
    assert after["trial_overlay"] is False


def test_downgrade_governed_to_verified_locks_the_paid_capability(db):
    """The mirror of the upgrade path. A Governed tenant drops to Verified and a
    Governed-only capability (payroll.calculator) must flip ALLOW -> DENY. The
    stale cache is dropped so the lock takes effect immediately rather than
    lingering for the whole TTL — the downgrade equivalent of the bug this
    bridge closes.
    """
    op_id, org_id = _tenant(db, plan="governed", state="active")

    before = entitlements.get(db, org_id)
    assert before["plan"] == "governed"
    assert before["capabilities"]["payroll.calculator"] == manifests.ALLOW
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none(), \
        "cache should be warm after the first resolve"

    changed = sync_from_billing(db, operational_org_id=op_id,
                                billing_status="active", plan_code="verified")

    assert changed["plan_code"] == ("governed", "verified")
    assert _sub(db, org_id).plan_code == "verified"
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none() is None, \
        "the stale Governed cache entry must be dropped, not kept for the TTL"

    after = entitlements.get(db, org_id)
    assert after["plan"] == "verified"
    assert after["capabilities"]["payroll.calculator"] == manifests.DENY, \
        "a downgraded tenant must lose the Governed-only payroll calculator"


# -- wiring: the projection is useless if the observer never registers --------
def test_bootstrap_registers_the_observer():
    """Dependency inversion means billing calls only what registered itself.
    If this wiring breaks the bridge fails silently, so pin it explicitly.
    """
    import app.commercial.bootstrap  # noqa: F401 - import is the thing under test
    from app.commercial import billing_bridge

    assert billing_bridge.on_billing_state_applied in billing_webhooks._STATE_OBSERVERS


def test_registration_is_idempotent():
    from app.commercial import billing_bridge

    before = list(billing_webhooks._STATE_OBSERVERS)
    billing_bridge.register()
    billing_bridge.register()
    assert billing_webhooks._STATE_OBSERVERS == before


def test_billing_still_works_with_no_observers_registered(db):
    """The invariant the architecture guard protects: strip the commercial layer
    and live billing behaves exactly as it did before this hook existed."""
    saved = list(billing_webhooks._STATE_OBSERVERS)
    billing_webhooks._STATE_OBSERVERS.clear()
    try:
        op_id, org_id = _tenant(db, plan="verified", state="trial_active")
        sub = _billing_sub(db, op_id, status="trial")

        changed = billing_webhooks.apply_subscription_state(
            db, sub, _remote(status="active"), event_type="test")

        assert changed["subscription_status"] == ("trial", "active")
        assert _org(db, org_id).commercial_state == "trial_active"
    finally:
        billing_webhooks._STATE_OBSERVERS[:] = saved


def _billing_sub(db, op_id, *, status="trial", customer="cus_bridge"):
    sub = models.BillingSubscription(
        organization_id=op_id, stripe_customer_id=customer,
        stripe_subscription_id=f"sub_{op_id}", subscription_status=status,
    )
    db.add(sub)
    db.flush()
    return sub


def _remote(*, status="active", customer="cus_bridge", cancel_at_period_end=False):
    """The subset of a Stripe subscription that apply_subscription_state reads."""
    return {
        "id": "sub_remote",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_end": int(
            (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
        "items": {"data": [{"price": {"id": "price_bridge_test"}}]},
    }


def test_webhook_state_change_reaches_the_commercial_row(db):
    """The end-to-end path this phase exists for: Stripe activates the
    subscription and the entitlement tables move with it, in one transaction."""
    import app.commercial.bootstrap  # noqa: F401 - ensures the observer is registered

    op_id, org_id = _tenant(db, plan="verified", state="trial_active")
    sub = _billing_sub(db, op_id, status="trial")

    billing_webhooks.apply_subscription_state(
        db, sub, _remote(status="active"), event_type="customer.subscription.updated")

    assert sub.subscription_status == "active"
    assert _org(db, org_id).commercial_state == "active", \
        "billing activated but entitlements stayed on the trial state"
    assert _sub(db, org_id).state == "active"


def test_webhook_past_due_reaches_the_commercial_row(db):
    import app.commercial.bootstrap  # noqa: F401

    op_id, org_id = _tenant(db, plan="governed", state="active")
    sub = _billing_sub(db, op_id, status="active")

    billing_webhooks.apply_subscription_state(
        db, sub, _remote(status="past_due"), event_type="invoice.payment_failed")

    assert _org(db, org_id).commercial_state == "past_due"


def test_subscription_deleted_cancels_the_commercial_tenant(db):
    """handle_subscription_deleted writes the status directly instead of going
    through apply_subscription_state, so it needs its own notification."""
    import app.commercial.bootstrap  # noqa: F401

    op_id, org_id = _tenant(db, plan="governed", state="active")
    _billing_sub(db, op_id, status="active")

    class _Client:
        class Subscription:
            @staticmethod
            def retrieve(_):
                raise RuntimeError("gone")  # exercises the trusted-deletion path

    billing_webhooks.handle_subscription_deleted(
        db, {"id": f"sub_{op_id}"}, _Client)

    assert _org(db, org_id).commercial_state == "cancelled"


def test_observer_failure_propagates_so_the_transaction_rolls_back(db):
    """If the projection cannot be applied, the billing write must not commit
    alone - that would recreate the divergence this bridge removes."""
    op_id, _ = _tenant(db, state="trial_active")
    sub = _billing_sub(db, op_id, status="trial")

    def _boom(*_a, **_k):
        raise RuntimeError("projection failed")

    saved = list(billing_webhooks._STATE_OBSERVERS)
    billing_webhooks._STATE_OBSERVERS[:] = [_boom]
    try:
        with pytest.raises(RuntimeError, match="projection failed"):
            billing_webhooks.apply_subscription_state(
                db, sub, _remote(status="active"), event_type="test")
    finally:
        billing_webhooks._STATE_OBSERVERS[:] = saved
