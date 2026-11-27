"""Subscription lifecycle mutations (Enterprise Billing §1).

Guards the previously-unreachable `cancelled` state, idempotency, illegal
transitions, and the authorized/idempotent HTTP path.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import subscription_ops, ids
from app.commercial.models import (
    CommercialSubscription, CommercialOrganization, CommercialEvent,
)
from app.commercial.routers.subscription import router as sub_router


def _seed(db, *, state="active", owner_user_id=None, seat_count=1, operational_org_id=None):
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="Acme", commercial_state=state,
                                  owner_user_id=owner_user_id,
                                  operational_org_id=operational_org_id))
    db.add(CommercialSubscription(id=ids.subscription_id(), organization_id=org_id,
                                  plan_code="verified", state=state, seat_count=seat_count))
    db.commit()
    return org_id


_uid = [500000]


def _add_active_users(db, operational_org_id, n):
    from database.models import User
    for _ in range(n):
        _uid[0] += 1
        db.add(User(id=_uid[0], name="U", email=f"u{_uid[0]}@acme.com",
                    organization_id=operational_org_id, is_active=True))
    db.commit()


# ── service ──────────────────────────────────────────────────────────────────
def test_cancel_at_renewal_schedules(db):
    org = _seed(db, state="active")
    r = subscription_ops.cancel(db, organization_id=org, mode="at_renewal")
    assert r["state"] == "cancelled_pending_end"
    assert r["cancel_requested_at"] is not None
    # org mirror updated + event emitted
    assert db.query(CommercialOrganization).filter_by(id=org).one().commercial_state == "cancelled_pending_end"
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.cancellation_scheduled").count() == 1


def test_cancel_immediate_reaches_cancelled(db):
    org = _seed(db, state="active")
    r = subscription_ops.cancel(db, organization_id=org, mode="immediate")
    assert r["state"] == "cancelled"          # the formerly-unreachable state
    assert r["cancelled_at"] is not None
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.cancelled").count() == 1


def test_cancel_is_idempotent(db):
    org = _seed(db, state="active")
    subscription_ops.cancel(db, organization_id=org, mode="at_renewal")
    subscription_ops.cancel(db, organization_id=org, mode="at_renewal")  # repeat
    # still exactly one scheduled event, still pending
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.cancellation_scheduled").count() == 1
    assert subscription_ops._load(db, org).state == "cancelled_pending_end"


def test_undo_cancel_restores_active(db):
    org = _seed(db, state="active")
    subscription_ops.cancel(db, organization_id=org, mode="at_renewal")
    r = subscription_ops.undo_cancel(db, organization_id=org)
    assert r["state"] == "active"
    assert r["cancel_requested_at"] is None
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.reactivated").count() == 1


def test_cannot_cancel_trial(db):
    org = _seed(db, state="trial_active")
    with pytest.raises(subscription_ops.SubscriptionOpError) as e:
        subscription_ops.cancel(db, organization_id=org, mode="at_renewal")
    assert e.value.code == "SUBSCRIPTION_STATE_INVALID"


def test_undo_when_active_is_noop(db):
    org = _seed(db, state="active")
    r = subscription_ops.undo_cancel(db, organization_id=org)
    assert r["state"] == "active"


# ── seat management ──────────────────────────────────────────────────────────
def test_increase_seats_emits_event_and_computes_available(db):
    op_id = 700001
    org = _seed(db, state="active", seat_count=1, operational_org_id=op_id)
    _add_active_users(db, op_id, 2)          # 2 seats in use
    r = subscription_ops.change_seats(db, organization_id=org, seat_count=5)
    assert r["seat_count"] == 5
    assert r["used_seats"] == 2
    assert r["available_seats"] == 3
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.seats_changed").count() == 1


def test_decrease_below_used_is_blocked(db):
    op_id = 700002
    org = _seed(db, state="active", seat_count=5, operational_org_id=op_id)
    _add_active_users(db, op_id, 3)          # 3 in use
    with pytest.raises(subscription_ops.SubscriptionOpError) as e:
        subscription_ops.change_seats(db, organization_id=org, seat_count=2)
    assert e.value.code == "SEAT_OVER_ALLOCATION"
    # seat_count unchanged
    assert subscription_ops._load(db, org).seat_count == 5


def test_same_seat_count_is_noop(db):
    org = _seed(db, state="active", seat_count=3)
    r = subscription_ops.change_seats(db, organization_id=org, seat_count=3)
    assert r["seat_count"] == 3
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.seats_changed").count() == 0


def test_seats_endpoint_idempotent(client, db):
    tc, owner = client
    org = _seed(db, state="active", seat_count=1, owner_user_id=owner.id)
    headers = {"Idempotency-Key": "seats-key-1"}
    body = {"organization_id": org, "seat_count": 10}
    r1 = tc.post("/api/v1/subscription/seats", json=body, headers=headers)
    r2 = tc.post("/api/v1/subscription/seats", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["seat_count"] == 10
    assert r2.json() == r1.json()
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.seats_changed").count() == 1


def test_seats_endpoint_rejects_zero(client, db):
    tc, owner = client
    org = _seed(db, state="active", owner_user_id=owner.id)
    r = tc.post("/api/v1/subscription/seats", json={"organization_id": org, "seat_count": 0})
    assert r.status_code == 422  # pydantic ge=1


# ── HTTP: auth + idempotency ─────────────────────────────────────────────────
@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(sub_router)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    owner = User(id=987654, email="owner@acme.com", role="admin")
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: owner
    return TestClient(app), owner


def test_cancel_endpoint_is_idempotent_and_authorized(client, db):
    tc, owner = client
    org = _seed(db, state="active", owner_user_id=owner.id)

    headers = {"Idempotency-Key": "cancel-key-1"}
    body = {"organization_id": org, "mode": "immediate"}
    r1 = tc.post("/api/v1/subscription/cancel", json=body, headers=headers)
    r2 = tc.post("/api/v1/subscription/cancel", json=body, headers=headers)

    assert r1.status_code == 200, r1.text
    assert r1.json()["state"] == "cancelled"
    assert r2.json() == r1.json()  # replayed
    # exactly one cancelled event despite two calls
    assert db.query(CommercialEvent).filter_by(
        organization_id=org, event_type="subscription.cancelled").count() == 1


def test_cancel_endpoint_forbidden_for_non_owner(client, db):
    tc, _owner = client
    org = _seed(db, state="active", owner_user_id=111111)  # different owner
    r = tc.post("/api/v1/subscription/cancel", json={"organization_id": org})
    assert r.status_code == 403
