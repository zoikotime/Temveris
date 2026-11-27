"""Phase 8 — failed payments have consequences, and downgrades are actually processed.

Before this, a failed payment produced one red banner. No email, no grace period, no
restriction: a customer whose card failed kept full service indefinitely, unpaid, and
was never told. And `billing_downgrade_requests` was write-only — 21 production rows
that no code, job or screen ever read — while `/upgrade` had no tier check, so a
customer could apply a cheaper plan there instantly and bypass review entirely.

Approved ladder: warnings on day 1 / 3 / 7 / 14; on day 14 the organization becomes
read-only. The Stripe subscription is never cancelled. Paying restores everything.

`SUSPENDED_BILLABLE` is deliberately NOT used: that is a per-worker state meaning
"access blocked, still billed", and payment failure is organization-level. Conflating
them would corrupt seat counting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import downgrades as billing_downgrades
from app.billing import dunning as billing_dunning
from app.billing import dunning_email
from app.billing import router as billing_router
from app.billing import seats as billing_seats
from app.billing import service as billing_service
from app.billing import suspension as billing_suspension
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingDowngradeRequest, BillingDunningNotice, BillingPlan, BillingSubscription,
    Organization, User,
)

API = "/api/admin/billing"
ORG_A, ORG_B, ORG_INT = 8301, 8302, 8303
SUB_A, CUS_A = "sub_dunA", "cus_dunA"


def _t(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


@pytest.fixture(autouse=True)
def _clear_suspension_cache():
    billing_suspension.invalidate_cache()
    yield
    billing_suspension.invalidate_cache()


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingDunningNotice).delete()
        db.query(BillingDowngradeRequest).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        for o in (ORG_A, ORG_B, ORG_INT):
            db.query(User).filter(User.organization_id == o).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B, ORG_INT])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Dunning A", billing_type="customer"))
    db.add(Organization(id=ORG_B, name="Dunning B", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Temveris", billing_type="internal"))
    verified = BillingPlan(code="verified", name="Verified", tier_order=1,
                          monthly_price_per_user=8.0, annual_price_per_user=76.80,
                          user_limit=25)
    governed = BillingPlan(code="governed", name="Governed", tier_order=2,
                          monthly_price_per_user=15.0, annual_price_per_user=144.0,
                          user_limit=100)
    sovereign = BillingPlan(code="sovereign", name="Sovereign", tier_order=3,
                           monthly_price_per_user=25.0, annual_price_per_user=240.0,
                           user_limit=None)
    db.add_all([verified, governed, sovereign])
    db.flush()
    for org, sid, cid in ((ORG_A, SUB_A, CUS_A), (ORG_B, "sub_dunB", "cus_dunB"),
                          (ORG_INT, "sub_int", "cus_int")):
        db.add(BillingSubscription(organization_id=org, plan_id=governed.id,
                                   billing_cycle="monthly", subscription_status="active",
                                   stripe_subscription_id=sid, stripe_customer_id=cid,
                                   active_users=3, currency="USD"))
    for i in range(3):
        db.add(User(name=f"d{i}", email=f"d{i}@dun.test", organization_id=ORG_A,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    # Someone to warn. Without a billing contact or admin there is no recipient, and
    # every notice would be recorded as failed(no_recipient) instead of sent.
    db.add(User(name="Org A Admin", email="admin@dunA.test", organization_id=ORG_A,
                role="admin", is_active=True, billing_state=worker_state.ACTIVE_BILLABLE))
    db.add(User(name="Owner Admin", email="admin@owner.test", organization_id=ORG_INT,
                role="admin", is_active=True, billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    yield {"verified": verified, "governed": governed, "sovereign": sovereign}
    _clean()


def _sub(db, org=ORG_A):
    return db.query(BillingSubscription).filter_by(organization_id=org).first()


def _actor(db, org, email, role="admin"):
    u = User(name="A", email=email, organization_id=org, role=role,
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _unpaid(db, org=ORG_A, *, days_ago=0.0, status="past_due"):
    sub = _sub(db, org)
    sub.subscription_status = status
    sub.dunning_started_at = _t(days_ago)
    db.commit()
    billing_suspension.invalidate_cache(org)
    return sub


# ══════════════════════════════════════════════════════════════════════════════
# Ladder timing
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("days,expected", [
    (0.5, []),
    (1.0, ["day_1"]),
    (2.9, ["day_1"]),
    (3.0, ["day_1", "day_3"]),
    (7.0, ["day_1", "day_3", "day_7"]),
    (13.9, ["day_1", "day_3", "day_7"]),
    (14.0, ["day_1", "day_3", "day_7", "day_14"]),
    (40.0, ["day_1", "day_3", "day_7", "day_14"]),
])
def test_due_markers_follow_the_approved_ladder(db, seeded, days, expected):
    sub = _unpaid(db, days_ago=days)
    assert billing_dunning.due_markers(sub) == expected


def test_sweep_sends_only_the_markers_that_are_due(db, seeded):
    _unpaid(db, days_ago=3.2)
    report = billing_dunning.run_sweep(db, send=False)
    markers = {n.day_marker for n in db.query(BillingDunningNotice).all()}
    assert markers == {"day_1", "day_3"}
    assert report["sent"] == 2


def test_repeated_hourly_sweeps_send_no_duplicates(db, seeded):
    """The sweep runs hourly; without the UNIQUE constraint day 1 would send 24 times."""
    _unpaid(db, days_ago=1.5)
    first = billing_dunning.run_sweep(db, send=False)
    assert first["sent"] == 1
    for _ in range(24):
        again = billing_dunning.run_sweep(db, send=False)
        assert again["sent"] == 0
    assert db.query(BillingDunningNotice).count() == 1


def test_duplicate_notice_is_refused_by_the_database(db, seeded):
    sub = _unpaid(db, days_ago=1.0)
    assert billing_dunning.record_notice(db, sub, "day_1", recipient="x@y.test") is True
    assert billing_dunning.record_notice(db, sub, "day_1", recipient="x@y.test") is False
    assert db.query(BillingDunningNotice).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# Suspension
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("days,suspended", [
    (0.0, False), (7.0, False), (13.9, False), (14.0, True), (30.0, True),
])
def test_suspension_begins_after_the_14_day_grace_period(db, seeded, days, suspended):
    sub = _unpaid(db, days_ago=days)
    assert billing_dunning.is_suspended(db, sub) is suspended


def test_a_stale_anchor_alone_never_suspends(db, seeded):
    """Stripe says paid; only the anchor lingered (a missed webhook)."""
    sub = _sub(db)
    sub.subscription_status = "active"
    sub.dunning_started_at = _t(40)
    db.commit()
    assert billing_dunning.is_suspended(db, sub) is False


def test_terminal_subscription_is_not_in_dunning(db, seeded):
    sub = _sub(db)
    sub.subscription_status = "canceled"
    db.commit()
    assert billing_dunning.begin(db, sub) is False
    assert sub.dunning_started_at is None


def test_stripe_subscription_is_never_cancelled_by_dunning(db, seeded):
    """Approved decision 5 — suspension does not cancel."""
    _unpaid(db, days_ago=30)
    billing_dunning.run_sweep(db, send=False)
    db.expire_all()
    sub = _sub(db)
    assert sub.stripe_subscription_id == SUB_A
    assert sub.subscription_status == "past_due"      # not "canceled"


def test_suspended_billable_is_not_used_for_org_level_failure(db, seeded):
    """Decision 8: that per-worker state must not be repurposed."""
    _unpaid(db, days_ago=30)
    billing_dunning.run_sweep(db, send=False)
    db.expire_all()
    states = {u.billing_state for u in
              db.query(User).filter(User.organization_id == ORG_A).all()}
    assert worker_state.SUSPENDED_BILLABLE not in states


# ══════════════════════════════════════════════════════════════════════════════
# Read-only enforcement (uses the existing get_current_user choke point)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_reads_are_never_blocked(db, seeded, method):
    _unpaid(db, days_ago=30)
    actor = _actor(db, ORG_A, f"read-{method}@dun.test")
    billing_suspension.check_request(db, actor, method, "/employees/list")   # no raise


@pytest.mark.parametrize("path", [
    "/employees/clock-in", "/employees/clock-out", "/employees/start-break",
    "/employees/resume-work", "/employees/network-pause", "/tracking/heartbeat",
])
def test_clock_in_and_tracking_keep_working_while_suspended(db, seeded, path):
    """Explicitly required: a billing dispute must not become a payroll dispute."""
    _unpaid(db, days_ago=30)
    actor = _actor(db, ORG_A, f"clock-{path.replace('/', '-')}@dun.test")
    billing_suspension.check_request(db, actor, "POST", path)                # no raise


@pytest.mark.parametrize("path", ["/api/admin/billing/upgrade",
                                  "/api/admin/billing/payment-method/session",
                                  "/auth/login", "/auth/refresh"])
def test_billing_and_auth_stay_open_so_payment_can_be_recovered(db, seeded, path):
    _unpaid(db, days_ago=30)
    actor = _actor(db, ORG_A, f"pay-{path.replace('/', '-')}@dun.test")
    billing_suspension.check_request(db, actor, "POST", path)                # no raise


@pytest.mark.parametrize("path", ["/employees/invite", "/employees/add-direct",
                                  "/api/settings/organization", "/org/update"])
def test_other_writes_are_refused_with_402(db, seeded, path):
    from fastapi import HTTPException
    _unpaid(db, days_ago=30)
    actor = _actor(db, ORG_A, f"write-{path.replace('/', '-')}@dun.test")
    with pytest.raises(HTTPException) as exc:
        billing_suspension.check_request(db, actor, "POST", path)
    assert exc.value.status_code == 402
    assert "read-only" in exc.value.detail.lower()


def test_writes_are_allowed_before_the_grace_period_expires(db, seeded):
    _unpaid(db, days_ago=7)
    actor = _actor(db, ORG_A, "grace-write@dun.test")
    billing_suspension.check_request(db, actor, "POST", "/employees/invite")  # no raise


def test_suspension_is_tenant_isolated(db, seeded):
    """Org A suspended must not restrict org B."""
    from fastapi import HTTPException
    _unpaid(db, ORG_A, days_ago=30)
    a = _actor(db, ORG_A, "iso-a@dun.test")
    b = _actor(db, ORG_B, "iso-b@dun.test")
    with pytest.raises(HTTPException):
        billing_suspension.check_request(db, a, "POST", "/employees/invite")
    billing_suspension.check_request(db, b, "POST", "/employees/invite")      # no raise


# ══════════════════════════════════════════════════════════════════════════════
# Recovery
# ══════════════════════════════════════════════════════════════════════════════
def test_recovery_during_grace_clears_anchor_and_notices(db, seeded):
    sub = _unpaid(db, days_ago=7)
    billing_dunning.run_sweep(db, send=False)
    assert db.query(BillingDunningNotice).count() == 3

    sub.subscription_status = "active"
    assert billing_dunning.sync_from_status(db, sub) == "cleared"
    db.commit()
    assert sub.dunning_started_at is None
    assert db.query(BillingDunningNotice).count() == 0


def test_recovery_after_suspension_restores_write_access(db, seeded):
    from fastapi import HTTPException
    sub = _unpaid(db, days_ago=30)
    actor = _actor(db, ORG_A, "recover@dun.test")
    with pytest.raises(HTTPException):
        billing_suspension.check_request(db, actor, "POST", "/employees/invite")

    sub.subscription_status = "active"
    billing_dunning.sync_from_status(db, sub)
    db.commit()
    billing_suspension.invalidate_cache(ORG_A)
    billing_suspension.check_request(db, actor, "POST", "/employees/invite")  # no raise


def test_a_new_failure_cycle_can_warn_again_from_day_one(db, seeded):
    sub = _unpaid(db, days_ago=3)
    billing_dunning.run_sweep(db, send=False)
    sub.subscription_status = "active"
    billing_dunning.sync_from_status(db, sub)
    db.commit()

    sub.subscription_status = "past_due"
    billing_dunning.sync_from_status(db, sub)
    sub.dunning_started_at = _t(1)
    db.commit()
    report = billing_dunning.run_sweep(db, send=False)
    assert report["sent"] == 1


def test_a_second_failure_does_not_reset_the_clock(db, seeded):
    """Otherwise repeated failures would keep a tenant in permanent grace."""
    sub = _unpaid(db, days_ago=10)
    anchor = sub.dunning_started_at
    assert billing_dunning.begin(db, sub) is False
    assert sub.dunning_started_at == anchor


# ══════════════════════════════════════════════════════════════════════════════
# Internal tenant exclusion
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_never_enters_dunning(db, seeded):
    sub = _sub(db, ORG_INT)
    sub.subscription_status = "past_due"
    db.commit()
    assert billing_dunning.begin(db, sub) is False
    assert sub.dunning_started_at is None
    assert billing_dunning.sync_from_status(db, sub) == "unchanged"


def test_internal_tenant_is_never_suspended(db, seeded):
    sub = _sub(db, ORG_INT)
    sub.subscription_status = "past_due"
    sub.dunning_started_at = _t(40)      # forced, as if set by mistake
    db.commit()
    assert billing_dunning.is_suspended(db, sub) is False


def test_internal_tenant_writes_are_never_blocked(db, seeded):
    sub = _sub(db, ORG_INT)
    sub.subscription_status = "past_due"
    sub.dunning_started_at = _t(40)
    db.commit()
    billing_suspension.invalidate_cache(ORG_INT)
    actor = _actor(db, ORG_INT, "intwrite@owner.test")
    billing_suspension.check_request(db, actor, "POST", "/employees/invite")  # no raise


def test_sweep_excludes_internal_tenants_in_sql(db, seeded):
    sub = _sub(db, ORG_INT)
    sub.subscription_status = "past_due"
    sub.dunning_started_at = _t(40)
    db.commit()
    report = billing_dunning.run_sweep(db, send=False)
    assert report["checked"] == 0
    assert db.query(BillingDunningNotice).count() == 0


def test_internal_tenant_receives_no_dunning_email(db, seeded, monkeypatch):
    sent = []
    monkeypatch.setattr(dunning_email, "send",
                        lambda *a, **kw: (sent.append(kw.get("marker")), (True, None))[1])
    sub = _sub(db, ORG_INT)
    sub.subscription_status = "past_due"
    sub.dunning_started_at = _t(40)
    db.commit()
    billing_dunning.run_sweep(db, send=True)
    assert sent == []


# ══════════════════════════════════════════════════════════════════════════════
# Email templates
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("marker", ["day_1", "day_3", "day_7", "day_14"])
def test_every_ladder_step_has_a_billing_owned_template(marker):
    subject, html = dunning_email.build(
        marker, company="Acme", plan_line="Governed — 3 seats at USD 15.00/seat/month",
        deadline="27 August 2026", billing_url="https://app.test/billing")
    assert subject and len(subject) > 10
    assert "Acme" in html
    assert "https://app.test/billing" in html
    # Days 1/3/7 warn about a future cut-off; day 14 IS that date, so it states the
    # consequence rather than a deadline.
    if marker != "day_14":
        assert "27 August 2026" in html


def test_day_14_email_explains_what_still_works():
    _subject, html = dunning_email.build(
        "day_14", company="Acme", plan_line="Governed", deadline="x",
        billing_url="https://app.test/billing")
    low = html.lower()
    assert "clock in" in low
    assert "not</strong> been cancelled" in low or "not been cancelled" in low


def test_templates_do_not_come_from_app_commercial():
    import inspect
    src = inspect.getsource(dunning_email)
    assert "app.commercial" not in src
    assert "from app.commercial" not in src


def test_send_is_recorded_as_failed_without_dispatching_twice(db, seeded, monkeypatch):
    monkeypatch.setattr(dunning_email, "send", lambda *a, **kw: (False, "smtp down"))
    _unpaid(db, days_ago=1)
    billing_dunning.run_sweep(db, send=True)
    rows = db.query(BillingDunningNotice).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failed" and row.last_error == "smtp down"
    # Recorded, so the hourly sweep does not retry it forever.
    assert billing_dunning.run_sweep(db, send=True)["sent"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# user_limit enforcement
# ══════════════════════════════════════════════════════════════════════════════
def test_activating_a_worker_beyond_the_plan_limit_is_refused(db, seeded):
    sub = _sub(db)
    sub.plan_id = seeded["verified"].id            # limit 25
    db.commit()
    for i in range(21):                            # 3 + 1 admin + 21 = 25, at the limit
        db.add(User(name=f"f{i}", email=f"f{i}@dun.test", organization_id=ORG_A,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    assert billing_service.count_billable_seats(db, ORG_A) == 25

    extra = User(name="over", email="over@dun.test", organization_id=ORG_A,
                 role="employee", is_active=True,
                 billing_state=worker_state.INVITED_PENDING)
    db.add(extra); db.flush()
    with pytest.raises(billing_service.PlanCapacityExceeded):
        billing_seats.set_worker_billing_state(db, extra, worker_state.ACTIVE_BILLABLE,
                                               sync=False)


def test_removing_billability_is_never_blocked_by_capacity(db, seeded):
    """Going down can't breach a ceiling."""
    sub = _sub(db)
    sub.plan_id = seeded["verified"].id
    db.commit()
    victim = db.query(User).filter(User.organization_id == ORG_A).first()
    billing_seats.set_worker_billing_state(db, victim, worker_state.DEACTIVATED, sync=False)
    assert victim.billing_state == worker_state.DEACTIVATED


def test_unlimited_plan_has_no_capacity_ceiling(db, seeded):
    sub = _sub(db)
    sub.plan_id = seeded["sovereign"].id           # user_limit None
    db.commit()
    u = User(name="many", email="many@dun.test", organization_id=ORG_A,
             role="employee", is_active=True, billing_state=worker_state.INVITED_PENDING)
    db.add(u); db.flush()
    billing_seats.set_worker_billing_state(db, u, worker_state.ACTIVE_BILLABLE, sync=False)
    assert u.billing_state == worker_state.ACTIVE_BILLABLE
