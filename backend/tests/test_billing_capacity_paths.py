"""Plan-capacity enforcement reaches the right person.

Phase 8 enforced ``BillingPlan.user_limit`` inside ``set_worker_billing_state``, which
was the correct place to *count* but the wrong place to *refuse*. Three paths call it,
and none caught ``PlanCapacityExceeded`` — so hitting the ceiling produced an unhandled
500. The worst case was invite acceptance: an invited employee clicking their link and
setting a password would get a raw 500, over their employer's billing, with no way to
fix it and no explanation.

The rule these tests pin: **refuse to whoever can resolve it, never to whoever cannot.**

  invite            admin acts   -> refuse, 409, counting pending invitations (§H3)
  add-direct        admin acts   -> refuse, 409, before the row is created
  reinstate         admin acts   -> refuse, ServiceError -> 400
  set password      employee acts-> ALLOW, log the overage, never block the person
"""

from __future__ import annotations

import pytest

from app.billing import seats as billing_seats
from app.billing import service as billing_service
from app.billing import worker_state
from database.models import BillingPlan, BillingSubscription, Organization, User

ORG = 8501
LIMIT = 5          # small stand-in for 250, so fixtures stay readable


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id == ORG).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id == ORG).delete(synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG, name="Capacity Co", billing_type="customer"))
    plan = BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=144.0,
                       user_limit=LIMIT)
    db.add(plan); db.flush()
    db.add(BillingSubscription(organization_id=ORG, plan_id=plan.id,
                              billing_cycle="monthly", subscription_status="active",
                              active_users=0, currency="USD"))
    db.commit()
    yield plan
    _clean()


def _fill(db, n, state=worker_state.ACTIVE_BILLABLE, tag="f"):
    for i in range(n):
        db.add(User(name=f"{tag}{i}", email=f"{tag}{i}@cap.test", organization_id=ORG,
                    role="employee", is_active=True, billing_state=state))
    db.commit()


def _pending(db, n):
    _fill(db, n, state=worker_state.INVITED_PENDING, tag="p")


# ══════════════════════════════════════════════════════════════════════════════
# The shared pre-check
# ══════════════════════════════════════════════════════════════════════════════
def test_room_exists_below_the_ceiling(db, seeded):
    _fill(db, LIMIT - 1)
    billing_service.assert_room_for_new_worker(db, ORG)      # no raise


def test_no_room_at_the_ceiling(db, seeded):
    _fill(db, LIMIT)
    with pytest.raises(billing_service.PlanCapacityExceeded) as exc:
        billing_service.assert_room_for_new_worker(db, ORG)
    assert str(LIMIT) in str(exc.value)


def test_pending_invitations_consume_capacity(db, seeded):
    """An invitation reserves a seat even though it is never charged (§H3).

    Without this an org could queue unlimited invitations and pass the ceiling as they
    were accepted — each acceptance individually looking fine.
    """
    _fill(db, LIMIT - 2)          # 3 billable
    _pending(db, 2)               # + 2 reserved = 5, at the ceiling
    assert billing_service.count_billable_seats(db, ORG) == LIMIT - 2
    with pytest.raises(billing_service.PlanCapacityExceeded):
        billing_service.assert_room_for_new_worker(db, ORG)


def test_reservations_can_be_ignored_when_asked(db, seeded):
    _fill(db, LIMIT - 2)
    _pending(db, 2)
    billing_service.assert_room_for_new_worker(db, ORG, include_reserved=False)  # no raise


def test_unlimited_plan_always_has_room(db, seeded):
    seeded.user_limit = None
    db.commit()
    _fill(db, 50)
    billing_service.assert_room_for_new_worker(db, ORG)      # no raise


def test_org_without_a_subscription_is_not_capped(db, seeded):
    db.query(BillingSubscription).filter_by(organization_id=ORG).delete()
    db.commit()
    _fill(db, 99)
    billing_service.assert_room_for_new_worker(db, ORG)      # no raise


# ══════════════════════════════════════════════════════════════════════════════
# Acceptance must never be blocked
# ══════════════════════════════════════════════════════════════════════════════
def test_acceptance_is_allowed_over_the_ceiling(db, seeded):
    """The regression this whole change exists for.

    Before: an unhandled PlanCapacityExceeded became a 500 and the employee could not
    set a password. Now the state is recorded and the overage logged.
    """
    _fill(db, LIMIT)
    joiner = User(name="joiner", email="joiner@cap.test", organization_id=ORG,
                  role="employee", is_active=True,
                  billing_state=worker_state.INVITED_PENDING)
    db.add(joiner); db.flush()

    billing_seats.set_worker_billing_state(
        db, joiner, worker_state.ACTIVE_BILLABLE,
        sync=False, reason="password_set", enforce_capacity=False)

    assert joiner.billing_state == worker_state.ACTIVE_BILLABLE
    assert billing_service.count_billable_seats(db, ORG) == LIMIT + 1


def test_the_overage_is_logged_not_silent(db, seeded, caplog):
    _fill(db, LIMIT)
    joiner = User(name="j2", email="j2@cap.test", organization_id=ORG, role="employee",
                  is_active=True, billing_state=worker_state.INVITED_PENDING)
    db.add(joiner); db.flush()
    with caplog.at_level("WARNING"):
        billing_seats.set_worker_billing_state(
            db, joiner, worker_state.ACTIVE_BILLABLE,
            sync=False, reason="password_set", enforce_capacity=False)
    assert any("capacity EXCEEDED and allowed" in r.message for r in caplog.records)


def test_enforcement_is_still_the_default(db, seeded):
    """enforce_capacity=False must be opt-in, so a new caller fails loudly."""
    _fill(db, LIMIT)
    u = User(name="d", email="d@cap.test", organization_id=ORG, role="employee",
             is_active=True, billing_state=worker_state.INVITED_PENDING)
    db.add(u); db.flush()
    with pytest.raises(billing_service.PlanCapacityExceeded):
        billing_seats.set_worker_billing_state(
            db, u, worker_state.ACTIVE_BILLABLE, sync=False)


def test_removing_billability_is_never_capacity_checked(db, seeded):
    """Going down cannot breach a ceiling, even from an over-capacity state."""
    _fill(db, LIMIT + 3)
    victim = db.query(User).filter(User.organization_id == ORG).first()
    billing_seats.set_worker_billing_state(
        db, victim, worker_state.DEACTIVATED, sync=False)
    assert victim.billing_state == worker_state.DEACTIVATED


def test_moving_between_billable_states_is_not_checked(db, seeded):
    _fill(db, LIMIT)
    u = db.query(User).filter(User.organization_id == ORG).first()
    billing_seats.set_worker_billing_state(
        db, u, worker_state.SUSPENDED_BILLABLE, sync=False)
    assert u.billing_state == worker_state.SUSPENDED_BILLABLE


# ══════════════════════════════════════════════════════════════════════════════
# Admin paths refuse, with a message an admin can act on
# ══════════════════════════════════════════════════════════════════════════════
def test_reinstate_raises_a_handled_error_not_a_bare_exception(db, seeded):
    """Offboarding surfaces ServiceError as a 400; an uncaught PlanCapacityExceeded
    would have been a 500."""
    from app.offboarding.services import ServiceError
    import inspect
    from app.offboarding import services as off_services
    src = inspect.getsource(off_services)
    block = src[src.index("offboarding_reinstate") - 900:]
    block = block[:block.index("offboarding_reinstate") + 600]
    assert "PlanCapacityExceeded" in block, "reinstate must catch the capacity error"
    assert "ServiceError" in block
    assert ServiceError("x").http_status == 400


def test_capacity_message_names_the_plan_that_solves_it(db, seeded):
    """"Choose a larger plan" makes the admin work out which one. Name it.

    An admin who hits the ceiling while adding somebody needs one sentence containing
    the answer: which plan holds this headcount. Naming it is the difference between a
    refusal and an instruction.
    """
    db.add(BillingPlan(code="sovereign", name="Sovereign", tier_order=3,
                       monthly_price_per_user=25.0, annual_price_per_user=240.0,
                       user_limit=None, active_status=True, is_enterprise=False))
    db.commit()
    _fill(db, LIMIT)
    with pytest.raises(billing_service.PlanCapacityExceeded) as exc:
        billing_service.assert_room_for_new_worker(db, ORG)
    msg = str(exc.value)
    assert "Governed" in msg                  # the plan they are on
    assert str(LIMIT) in msg                  # the limit they hit
    assert str(LIMIT + 1) in msg              # what it would become
    assert "Sovereign" in msg                 # the plan that fixes it
    assert "unlimited users" in msg           # and how much room it gives


def test_capacity_message_points_at_sales_when_no_self_serve_plan_fits(db, seeded):
    """With nothing above the current tier, "upgrade" is not an instruction.

    Enterprise is quote-only, so offering it as a self-serve answer would be a dead
    end. The message has to send them to a human instead.
    """
    _fill(db, LIMIT)
    with pytest.raises(billing_service.PlanCapacityExceeded) as exc:
        billing_service.assert_room_for_new_worker(db, ORG)
    msg = str(exc.value)
    assert "contact sales" in msg.lower()
    assert "Enterprise" in msg


def test_enterprise_is_never_offered_as_the_upgrade(db, seeded):
    """It cannot be self-served, so naming it as the fix would strand the admin."""
    db.add(BillingPlan(code="enterprise", name="Enterprise", tier_order=4,
                       monthly_price_per_user=0.0, user_limit=None,
                       active_status=True, is_enterprise=True))
    db.commit()
    assert billing_service.smallest_plan_holding(db, LIMIT + 1, above_tier=2) is None


def test_admin_endpoints_check_before_creating_anything():
    """Both admin paths must refuse *before* the User row exists — a half-created
    employee is worse than a refusal."""
    import inspect
    from app.employees import router as emp_router
    src = inspect.getsource(emp_router)

    for marker in ("assert_room_for_new_worker",):
        assert src.count(marker) == 2, "expected the check on both invite and add-direct"

    # add-direct: the check must precede the models.User(...) construction.
    add = src[src.index("Employee created with credentials") - 3000:
              src.index("Employee created with credentials")]
    assert add.index("assert_room_for_new_worker") < add.rindex("models.User("), \
        "capacity must be checked before the row is built"


def test_resend_invite_is_exempt(db, seeded):
    """Re-sending an invitation must not be refused — its capacity was already
    reserved by the original invitation, so charging for it twice would wrongly block
    a legitimate resend at the ceiling."""
    import inspect
    from app.employees import router as emp_router
    src = inspect.getsource(emp_router)
    block = src[src.index("assert_room_for_new_worker") - 700:
                src.index("assert_room_for_new_worker") + 200]
    assert "system_user is None" in block, "the invite check must skip existing users"
