"""Phase 2.2 — billing_state lifecycle + Stripe seat quantity synchronisation.

Seats are *derived* from workers (ZT-COM-BILL-001 §17), so Stripe's subscription
quantity must track the organization's billable headcount. Before this phase
``billing_state`` was written only by the Phase 1 migration backfill — every
worker created afterwards stayed ``NULL`` and was never billable, and nobody who
left ever stopped being billed.

Stripe is stubbed throughout; no network, no live resources.
"""

from __future__ import annotations

import pytest

from app.billing import seats as billing_seats
from app.billing import service as billing_service
from app.billing import worker_state as ws
from database.models import (
    BillingAuditEvent, BillingPlan, BillingSubscription, Organization, User,
)

ORG_A = 9501
ORG_B = 9502
SUB_A, CUS_A, ITEM_A = "sub_A", "cus_A", "si_A"
SUB_B, CUS_B, ITEM_B = "sub_B", "cus_B", "si_B"


class StubStripe:
    """Minimal Stripe double that tracks quantity per subscription."""

    def __init__(self, quantities=None, *, customers=None, status="active",
                 fail_on=None, items_per_sub=1):
        self.quantities = dict(quantities or {SUB_A: 1, SUB_B: 1})
        self.customers = dict(customers or {SUB_A: CUS_A, SUB_B: CUS_B})
        self.items = {SUB_A: ITEM_A, SUB_B: ITEM_B}
        self._status = status
        self._fail_on = fail_on
        self._items_per_sub = items_per_sub
        self.retrieve_calls, self.modify_calls, self.session_calls = [], [], []
        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                outer.retrieve_calls.append(sid)
                if outer._fail_on == "retrieve":
                    raise RuntimeError("stripe unreachable")
                data = [{"id": outer.items[sid], "quantity": outer.quantities[sid]}]
                if outer._items_per_sub > 1:
                    data.append({"id": "si_extra", "quantity": 1})
                return {"id": sid, "customer": outer.customers[sid],
                        "status": outer._status, "items": {"data": data}}

            @staticmethod
            def modify(sid, **kwargs):
                outer.modify_calls.append((sid, kwargs))
                if outer._fail_on == "modify":
                    raise RuntimeError("stripe rejected the change")
                for it in kwargs.get("items", []):
                    if "quantity" in it:
                        outer.quantities[sid] = it["quantity"]
                return {"id": sid, "status": "active"}

        class _Session:
            @staticmethod
            def create(**kwargs):
                outer.session_calls.append(kwargs)
                return type("S", (), {"id": "cs", "url": "https://stub"})()

        self.Subscription = _Subscription
        self.checkout = type("C", (), {"Session": _Session})


@pytest.fixture
def env(engine, db):
    for m in (Organization, BillingPlan, BillingSubscription, BillingAuditEvent):
        m.__table__.create(engine, checkfirst=True)

    def _clean():
        db.query(BillingAuditEvent).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add_all([Organization(id=ORG_A, name="A"), Organization(id=ORG_B, name="B")])
    plans = {}
    for i, (code, m_, a_, cap) in enumerate(
            [("governed", 15.0, 149.0, 100), ("sovereign", 25.0, 240.0, None)], 1):
        p = BillingPlan(code=code, name=code, tier_order=i,
                        monthly_price_per_user=m_, annual_price_per_user=a_,
                        user_limit=cap)
        db.add(p); plans[code] = p
    db.flush()
    db.add(BillingSubscription(
        organization_id=ORG_A, plan_id=plans["governed"].id, billing_cycle="monthly",
        subscription_status="active", stripe_customer_id=CUS_A,
        stripe_subscription_id=SUB_A, billable_users=1, active_users=1))
    db.add(BillingSubscription(
        organization_id=ORG_B, plan_id=plans["governed"].id, billing_cycle="monthly",
        subscription_status="active", stripe_customer_id=CUS_B,
        stripe_subscription_id=SUB_B, billable_users=1, active_users=1))
    db.commit()
    yield {"plans": plans}
    _clean()


def _worker(db, org, email, state):
    u = User(name=email.split("@")[0], email=email, organization_id=org,
             billing_state=state, is_active=True, role="employee")
    db.add(u); db.flush()
    return u


def _sub(db, org):
    return db.query(BillingSubscription).filter_by(organization_id=org).one()


# ══════════════════════════════════════════════════════════════════════════════
# 1-7, 27-29 — billing_state determines billability
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state,billable", [
    (ws.ACTIVE_BILLABLE, True),
    (ws.SUSPENDED_BILLABLE, True),      # §A4 suspension keeps the seat paid
    (ws.INVITED_PENDING, False),        # reserves capacity, never charged
    (ws.DEACTIVATED, False),
    (ws.TERMINATED, False),
    (ws.ARCHIVED, False),
    (ws.MIGRATION_REVIEW, False),
    (None, False),                      # not yet backfilled → never billable
])
def test_billable_states_determine_quantity(db, env, state, billable):
    _worker(db, ORG_A, f"{state}@s.test", state)
    # baseline is MIN_QUANTITY(1); a billable worker makes it 1 as well, so
    # assert against the raw count rather than the clamped quantity.
    count = billing_service.count_billable_seats(db, ORG_A)
    assert (count == 1) is billable


def test_invited_users_never_increase_quantity(db, env):
    for i in range(4):
        _worker(db, ORG_A, f"inv{i}@s.test", ws.INVITED_PENDING)
    assert billing_service.count_billable_seats(db, ORG_A) == 0
    assert billing_seats.desired_seat_quantity(db, ORG_A) == 1   # clamped floor


def test_deactivated_users_never_increase_quantity(db, env):
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    for i in range(3):
        _worker(db, ORG_A, f"gone{i}@s.test", ws.DEACTIVATED)
    assert billing_seats.desired_seat_quantity(db, ORG_A) == 1


def test_suspended_billable_does_increase_quantity(db, env):
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    _worker(db, ORG_A, "susp@s.test", ws.SUSPENDED_BILLABLE)
    assert billing_seats.desired_seat_quantity(db, ORG_A) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle transitions via set_worker_billing_state
# ══════════════════════════════════════════════════════════════════════════════
def test_invite_then_acceptance_transitions_to_billable(db, env):
    stub = StubStripe()
    u = _worker(db, ORG_A, "new@s.test", ws.INVITED_PENDING)
    assert billing_seats.desired_seat_quantity(db, ORG_A) == 1   # invite not billed

    billing_seats.set_worker_billing_state(db, u, ws.ACTIVE_BILLABLE, sync=False)
    assert u.billing_state == ws.ACTIVE_BILLABLE
    assert billing_service.count_billable_seats(db, ORG_A) == 1


def test_deactivation_then_reactivation(db, env):
    u = _worker(db, ORG_A, "cycle@s.test", ws.ACTIVE_BILLABLE)
    billing_seats.set_worker_billing_state(db, u, ws.DEACTIVATED, sync=False)
    assert billing_service.count_billable_seats(db, ORG_A) == 0
    billing_seats.set_worker_billing_state(db, u, ws.ACTIVE_BILLABLE, sync=False)
    assert billing_service.count_billable_seats(db, ORG_A) == 1


def test_unknown_state_is_rejected(db, env):
    u = _worker(db, ORG_A, "bad@s.test", ws.ACTIVE_BILLABLE)
    with pytest.raises(ValueError):
        billing_seats.set_worker_billing_state(db, u, "NOT_A_STATE", sync=False)


# ══════════════════════════════════════════════════════════════════════════════
# 9-11 — quantity synchronisation
# ══════════════════════════════════════════════════════════════════════════════
def test_seat_increase_updates_stripe_quantity(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 1})
    for i in range(5):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "updated"
    assert res["quantity"] == 5
    assert stub.quantities[SUB_A] == 5
    assert _sub(db, ORG_A).billable_users == 5


def test_seat_decrease_updates_stripe_quantity(db, env):
    stub = StubStripe({SUB_A: 5, SUB_B: 1})
    workers = [_worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE) for i in range(5)]
    db.commit()
    billing_seats.set_worker_billing_state(db, workers[0], ws.DEACTIVATED, sync=False)
    db.commit()

    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "updated"
    assert stub.quantities[SUB_A] == 4


def test_unchanged_quantity_makes_no_modify_call(db, env):
    stub = StubStripe({SUB_A: 3, SUB_B: 1})
    for i in range(3):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "in_sync"
    assert stub.modify_calls == [], "no Stripe write when already correct"


# ══════════════════════════════════════════════════════════════════════════════
# 21-24 — proration behaviour, monthly and annual, both directions
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cycle", ["monthly", "annual"])
@pytest.mark.parametrize("direction", ["increase", "decrease"])
def test_seat_changes_use_create_prorations(db, env, cycle, direction):
    sub = _sub(db, ORG_A)
    sub.billing_cycle = cycle
    db.commit()

    start = 1 if direction == "increase" else 5
    stub = StubStripe({SUB_A: start, SUB_B: 1})
    target = 4 if direction == "increase" else 2
    for i in range(target):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    _, kwargs = stub.modify_calls[0]
    assert kwargs["proration_behavior"] == "create_prorations"
    assert "always_invoice" not in str(kwargs), "Phase 2.3 owns invoice-now"


def test_seat_only_sync_never_sends_a_price(db, env):
    """A seat change must not be able to move the customer's plan."""
    stub = StubStripe({SUB_A: 1, SUB_B: 1})
    for i in range(3):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    _, kwargs = stub.modify_calls[0]
    assert "price" not in kwargs["items"][0]


# ══════════════════════════════════════════════════════════════════════════════
# 12, 16-19 — no subscription created, ids preserved, no duplicates
# ══════════════════════════════════════════════════════════════════════════════
def test_no_stripe_subscription_means_no_subscription_is_created(db, env):
    sub = _sub(db, ORG_A)
    sub.stripe_subscription_id = None
    sub.stripe_customer_id = None
    db.commit()

    stub = StubStripe()
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "no_stripe_subscription"
    assert stub.session_calls == [], "an employee change is not a purchase"
    assert stub.modify_calls == []


def test_ids_preserved_and_no_duplicates(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 1})
    for i in range(3):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)

    row = _sub(db, ORG_A)
    assert row.stripe_subscription_id == SUB_A
    assert row.stripe_customer_id == CUS_A
    assert stub.session_calls == []                       # no second subscription
    assert len(stub.modify_calls) == 1
    assert len(stub.modify_calls[0][1]["items"]) == 1      # no second item
    assert db.query(BillingSubscription).filter_by(organization_id=ORG_A).count() == 1


def test_multi_item_subscription_is_refused(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 1}, items_per_sub=2)
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"
    assert stub.modify_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 8, 25 — tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_org_seat_counts_are_isolated(db, env):
    for i in range(5):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    for i in range(3):
        _worker(db, ORG_B, f"b{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    assert billing_seats.desired_seat_quantity(db, ORG_A) == 5
    assert billing_seats.desired_seat_quantity(db, ORG_B) == 3


def test_syncing_org_a_never_touches_org_b(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 3})
    for i in range(5):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    for i in range(3):
        _worker(db, ORG_B, f"b{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert stub.retrieve_calls == [SUB_A]
    assert [c[0] for c in stub.modify_calls] == [SUB_A]
    assert stub.quantities[SUB_B] == 3, "org B quantity untouched"
    assert _sub(db, ORG_B).billable_users == 1


def test_subscription_belonging_to_another_customer_is_refused(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 1}, customers={SUB_A: "cus_someone_else",
                                                       SUB_B: CUS_B})
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"
    assert res["error"] == "customer_mismatch"
    assert stub.modify_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 26 — missing organization fails closed
# ══════════════════════════════════════════════════════════════════════════════
def test_missing_organization_fails_closed(db, env):
    stub = StubStripe()
    res = billing_seats.sync_org_seats(db, None, stripe_client=stub)
    assert res["status"] == "no_organization"
    assert stub.retrieve_calls == [] and stub.modify_calls == []


def test_worker_without_org_cannot_trigger_a_sync(db, env):
    stub = StubStripe()
    u = User(name="orphan", email="orphan@s.test", organization_id=None,
             billing_state=ws.INVITED_PENDING, is_active=True, role="employee")
    db.add(u); db.flush()
    billing_seats.set_worker_billing_state(db, u, ws.ACTIVE_BILLABLE, sync=True)
    assert u.billing_state == ws.ACTIVE_BILLABLE
    assert stub.modify_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 13, 14 — failure safety and retryability
# ══════════════════════════════════════════════════════════════════════════════
def test_stripe_failure_leaves_lifecycle_intact_and_is_recorded(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 1}, fail_on="modify")
    for i in range(4):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"

    # The workers keep their states — a Stripe outage must not undo the
    # lifecycle operation.
    assert billing_service.count_billable_seats(db, ORG_A) == 4
    # And the failure is recorded so reconciliation can retry.
    ev = db.query(BillingAuditEvent).filter_by(
        organization_id=ORG_A, action_key=billing_seats.SEAT_SYNC_FAILED).all()
    assert len(ev) == 1
    assert ev[0].details_json["desired_quantity"] == 4


def test_stripe_unreachable_does_not_attempt_a_modify(db, env):
    stub = StubStripe(fail_on="retrieve")
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"
    assert stub.modify_calls == []


def test_failed_sync_is_retryable(db, env):
    """A failure is transient: retrying with a healthy Stripe converges."""
    for i in range(4):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    failing = StubStripe({SUB_A: 1, SUB_B: 1}, fail_on="modify")
    assert billing_seats.sync_org_seats(db, ORG_A, stripe_client=failing)["status"] == "failed"
    assert failing.quantities[SUB_A] == 1

    healthy = StubStripe({SUB_A: 1, SUB_B: 1})
    res = billing_seats.reconcile_org_seat_quantity(db, ORG_A, stripe_client=healthy)
    assert res["status"] == "updated"
    assert healthy.quantities[SUB_A] == 4


@pytest.mark.parametrize("status", ["canceled", "incomplete_expired", "unpaid"])
def test_non_modifiable_subscription_is_skipped(db, env, status):
    stub = StubStripe({SUB_A: 1, SUB_B: 1}, status=status)
    _worker(db, ORG_A, "a@s.test", ws.ACTIVE_BILLABLE)
    db.commit()
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"
    assert stub.modify_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 15 — reconciliation corrects drift
# ══════════════════════════════════════════════════════════════════════════════
def test_reconciliation_corrects_quantity_drift(db, env):
    """Stripe says 9, the org actually has 2 billable workers."""
    stub = StubStripe({SUB_A: 9, SUB_B: 1})
    for i in range(2):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    res = billing_seats.reconcile_org_seat_quantity(db, ORG_A, stripe_client=stub)
    assert res["status"] == "updated"
    assert stub.quantities[SUB_A] == 2


def test_reconcile_all_orgs_sweeps_every_tenant(db, env):
    stub = StubStripe({SUB_A: 1, SUB_B: 7})
    for i in range(3):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    for i in range(2):
        _worker(db, ORG_B, f"b{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    tally = billing_seats.reconcile_all_orgs(db, stripe_client=stub)
    assert tally["checked"] == 2
    assert tally["updated"] == 2
    assert stub.quantities[SUB_A] == 3
    assert stub.quantities[SUB_B] == 2


def test_reconcile_is_a_no_op_when_everything_agrees(db, env):
    stub = StubStripe({SUB_A: 2, SUB_B: 1})
    for i in range(2):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    _worker(db, ORG_B, "b@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    tally = billing_seats.reconcile_all_orgs(db, stripe_client=stub)
    assert tally["in_sync"] == 2
    assert stub.modify_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 20 — plan + seat change is ONE Stripe modify
# ══════════════════════════════════════════════════════════════════════════════
def test_plan_and_seat_change_is_a_single_modify(db, env):
    """Governed 5 seats → Sovereign 7 seats must be one call carrying both,
    otherwise Stripe generates two separate proration events."""
    stub = StubStripe({SUB_A: 5, SUB_B: 1})
    for i in range(7):
        _worker(db, ORG_A, f"a{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    billing_service.change_stripe_plan(
        db, _sub(db, ORG_A),
        price_id="price_sovereign_monthly",
        seats=billing_seats.desired_seat_quantity(db, ORG_A),
        target_plan=env["plans"]["sovereign"],
        billing_cycle="monthly",
        stripe_client=stub,
    )

    assert len(stub.modify_calls) == 1, "one modify, not two"
    _, kwargs = stub.modify_calls[0]
    item = kwargs["items"][0]
    assert item["price"] == "price_sovereign_monthly"
    assert item["quantity"] == 7
    assert kwargs["proration_behavior"] == "create_prorations"

    row = _sub(db, ORG_A)
    assert row.stripe_subscription_id == SUB_A
    assert row.stripe_customer_id == CUS_A
    assert row.plan_id == env["plans"]["sovereign"].id


# ══════════════════════════════════════════════════════════════════════════════
# Concurrency convergence
# ══════════════════════════════════════════════════════════════════════════════
def test_concurrent_activations_converge_to_the_true_total(db, env):
    """Two activations landing together must not leave Stripe at 6 when the true
    count is 7 — sync sends the absolute recomputed count, never a delta."""
    stub = StubStripe({SUB_A: 5, SUB_B: 1})
    for i in range(5):
        _worker(db, ORG_A, f"base{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    a = _worker(db, ORG_A, "newA@s.test", ws.INVITED_PENDING)
    b = _worker(db, ORG_A, "newB@s.test", ws.INVITED_PENDING)
    db.commit()

    # Both become billable, then both syncs run (as would happen concurrently).
    a.billing_state = ws.ACTIVE_BILLABLE
    b.billing_state = ws.ACTIVE_BILLABLE
    db.commit()

    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)

    assert stub.quantities[SUB_A] == 7, "converged on the true total"
    assert len(stub.modify_calls) == 1, "second sync saw it was already correct"


def test_sync_failure_does_not_discard_uncommitted_lifecycle_work(db, env):
    """Regression: recording a sync failure must not touch the caller's session.

    An earlier implementation called ``db.rollback()`` before writing the audit
    row, which discarded the activation/deactivation the caller had flushed but
    not yet committed — a Stripe outage silently reverted the lifecycle change.
    """
    for i in range(3):
        _worker(db, ORG_A, f"base{i}@s.test", ws.ACTIVE_BILLABLE)
    db.commit()

    # Flush (not commit) a new activation, exactly as a lifecycle hook does.
    u = _worker(db, ORG_A, "pending@s.test", ws.INVITED_PENDING)
    u.billing_state = ws.ACTIVE_BILLABLE
    db.flush()

    stub = StubStripe({SUB_A: 3, SUB_B: 1}, fail_on="retrieve")
    res = billing_seats.sync_org_seats(db, ORG_A, stripe_client=stub)
    assert res["status"] == "failed"

    # The uncommitted lifecycle write must still be there.
    assert u.billing_state == ws.ACTIVE_BILLABLE
    db.commit()
    db.refresh(u)
    assert u.billing_state == ws.ACTIVE_BILLABLE, "lifecycle change was discarded"
    assert billing_service.count_billable_seats(db, ORG_A) == 4
