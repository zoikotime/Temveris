"""Phase 9 — one authoritative 14-day trial.

Before this there were two trial systems that did not know about each other:

* ``app/billing`` created a 14-day trial (``trial_start_date`` / ``trial_end_date``),
  displayed it, sent nothing, and never expired it — a customer kept full access
  forever and was never asked to pay.
* ``app/commercial/trial_lifecycle`` ran a 30-day ladder hourly, sent nine emails, and
  moved a parallel state machine through trial_active → … → trial_closed.

The customer-visible countdown came from one and the email from the other, 16 days
apart. Temveris's trial is 14 days (confirmed 2026-08-18), so the billing clock is now
authoritative, ``app/billing/trial.py`` is the only sender, and the commercial scheduler
is no longer started.

No test here sends mail: ``trial_email.send`` is monkeypatched wherever dispatch matters,
and the sweep is called with ``send=False`` everywhere else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.billing import dunning as billing_dunning
from app.billing import service as billing_service
from app.billing import trial as billing_trial
from app.billing import trial_email
from app.billing import worker_state
from database.models import (
    BillingContact, BillingDunningNotice, BillingPlan, BillingSubscription,
    Organization, User,
)

ORG_A, ORG_B, ORG_INT = 9401, 9402, 9403


def _t(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingDunningNotice).delete()
        db.query(BillingContact).filter(
            BillingContact.organization_id.in_([ORG_A, ORG_B, ORG_INT])).delete(
                synchronize_session=False)
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        for o in (ORG_A, ORG_B, ORG_INT):
            db.query(User).filter(User.organization_id == o).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B, ORG_INT])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Trial A", billing_type="customer"))
    db.add(Organization(id=ORG_B, name="Trial B", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Temveris", billing_type="internal"))
    verified = BillingPlan(code="verified", name="Verified", tier_order=1,
                           monthly_price_per_user=8.0, annual_price_per_user=76.80,
                           user_limit=25)
    db.add(verified)
    db.flush()
    for org in (ORG_A, ORG_B, ORG_INT):
        db.add(BillingSubscription(organization_id=org, plan_id=verified.id,
                                   billing_cycle="monthly",
                                   subscription_status=billing_trial.STATUS_TRIAL,
                                   trial_start_date=_t(0), trial_end_date=_t(-14),
                                   active_users=2, currency="USD"))
        db.add(User(name=f"Admin {org}", email=f"admin{org}@trial.test",
                    organization_id=org, role="admin", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    yield {"verified": verified}
    _clean()


def _sub(db, org=ORG_A):
    return db.query(BillingSubscription).filter_by(organization_id=org).first()


def _start_trial(db, org=ORG_A, *, days_ago: float, length: int = 14):
    """Backdate a trial so a given day of the ladder is 'now'.

    ``created_at`` is moved with the start date, because that is what a real customer
    looks like: the row is written the moment the trial begins. Leaving created_at at
    "now" would instead describe a *grandfathered* row, which the anti-back-send guard
    deliberately treats differently — see _grandfather() and its tests.
    """
    sub = _sub(db, org)
    start = _t(days_ago)
    sub.trial_start_date = start
    sub.trial_end_date = start + timedelta(days=length)
    sub.created_at = start
    sub.subscription_status = billing_trial.STATUS_TRIAL
    db.commit()
    return sub


def _grandfather(db, org=ORG_A, *, started_days_ago: float, length: int):
    """A row created NOW for a trial that began earlier — the orgs 12-15 shape."""
    sub = _sub(db, org)
    start = _t(started_days_ago)
    sub.trial_start_date = start
    sub.trial_end_date = start + timedelta(days=length)
    sub.created_at = datetime.now(timezone.utc)          # written today
    sub.subscription_status = billing_trial.STATUS_TRIAL
    db.commit()
    return sub


def _markers(db, org=ORG_A):
    sub = _sub(db, org)
    return sorted(
        r.day_marker for r in
        db.query(BillingDunningNotice).filter_by(subscription_id=sub.id).all()
    )


@pytest.fixture
def no_mail(monkeypatch):
    """Capture dispatch instead of sending. Nothing may reach a real mailbox."""
    sent = []

    def _fake(db, sub, *, marker, recipient):
        sent.append((sub.organization_id, marker, recipient))
        return True, None

    monkeypatch.setattr(trial_email, "send", _fake)
    return sent


# ══════════════════════════════════════════════════════════════════════════════
# 1-2. The clock is 14 days, and trial_end_date is derived from it
# ══════════════════════════════════════════════════════════════════════════════
def test_trial_length_is_exactly_14_days():
    assert billing_trial.TRIAL_LENGTH_DAYS == 14


def test_ensure_subscription_sets_end_date_14_days_after_start(db, seeded):
    db.query(BillingSubscription).filter_by(organization_id=ORG_B).delete()
    db.commit()
    sub = billing_service.ensure_subscription(db, ORG_B)
    span = sub.trial_end_date - sub.trial_start_date
    assert span == timedelta(days=14)
    assert sub.subscription_status == billing_trial.STATUS_TRIAL


def test_renewal_date_matches_trial_end_not_thirty_days(db, seeded):
    """The banner falls back to renewal_date; a +30d value made a 14-day trial read 30."""
    db.query(BillingSubscription).filter_by(organization_id=ORG_B).delete()
    db.commit()
    sub = billing_service.ensure_subscription(db, ORG_B)
    assert sub.renewal_date == sub.trial_end_date


def test_days_remaining_counts_down(db, seeded):
    assert billing_trial.days_remaining(_start_trial(db, days_ago=0)) == 14
    assert billing_trial.days_remaining(_start_trial(db, days_ago=11)) == 3
    assert billing_trial.days_remaining(_start_trial(db, days_ago=14)) == 0
    assert billing_trial.days_remaining(_start_trial(db, days_ago=40)) == 0


def test_an_extended_trial_is_honoured_not_reset(db, seeded):
    """expires_at trusts a stored end date, so an operator can extend a trial."""
    sub = _start_trial(db, days_ago=10, length=45)
    assert billing_trial.days_remaining(sub) == 35
    assert not billing_trial.is_expired(sub)


# ══════════════════════════════════════════════════════════════════════════════
# Reminders are anchored to the END date, so they are true for any trial length
# ══════════════════════════════════════════════════════════════════════════════
def test_reminders_are_scheduled_by_days_remaining(db, seeded):
    """On a 14-day trial the end-anchored schedule is identical to the day numbers."""
    sub = _start_trial(db, days_ago=0)
    start = billing_trial._aware(sub.trial_start_date)
    for marker, day in (("trial_day_0", 0), ("trial_day_7", 7),
                        ("trial_day_11", 11), ("trial_day_13", 13),
                        ("trial_expired", 14)):
        assert billing_trial.scheduled_at(sub, marker) == start + timedelta(days=day), marker


def test_thirty_day_trial_does_not_claim_to_end_on_day_14(db, seeded):
    """The bug grandfathering would have caused.

    Anchored to the start, 'your trial ends tomorrow' would fire on day 13 of a 30-day
    trial — seventeen days early, and false. Anchored to the end it fires the day before
    the trial actually ends.
    """
    sub = _start_trial(db, days_ago=0, length=30)
    start = billing_trial._aware(sub.trial_start_date)
    assert billing_trial.scheduled_at(sub, "trial_day_13") == start + timedelta(days=29)
    assert billing_trial.scheduled_at(sub, "trial_day_7") == start + timedelta(days=23)
    assert billing_trial.scheduled_at(sub, "trial_expired") == start + timedelta(days=30)
    # On day 13 of a 30-day trial nothing but the welcome is due.
    assert billing_trial.due_markers(sub, now=start + timedelta(days=13)) == ["trial_day_0"]


def test_thirty_day_reminders_fire_in_the_final_week(db, seeded):
    sub = _start_trial(db, days_ago=0, length=30)
    start = billing_trial._aware(sub.trial_start_date)
    at = lambda d: billing_trial.due_markers(sub, now=start + timedelta(days=d))
    assert "trial_day_7" in at(23)
    assert "trial_day_11" in at(27)
    assert "trial_day_13" in at(29)
    assert "trial_expired" in at(30)


# ══════════════════════════════════════════════════════════════════════════════
# Grandfathered rows: no back-sending
# ══════════════════════════════════════════════════════════════════════════════
def test_grandfathered_row_does_not_back_send_the_welcome(db, seeded, no_mail):
    """Orgs 12-15: a row created today for a 30-day trial that began 6 days ago.

    The welcome was already sent by the old system. Its scheduled moment predates this
    row, so it must be closed out rather than delivered.
    """
    _grandfather(db, started_days_ago=6, length=30)
    report = billing_trial.process_subscription(db, _sub(db))
    assert no_mail == []                                  # nothing sent at all
    row = db.query(BillingDunningNotice).filter_by(day_marker="trial_day_0").one()
    assert row.status == "superseded"
    assert "not back-sent" in row.last_error
    assert report["expired"] is False


def test_grandfathered_row_still_sends_its_future_reminders(db, seeded, no_mail):
    """The remaining reminders are in the future, so they must still be delivered."""
    sub = _grandfather(db, started_days_ago=6, length=30)
    billing_trial.process_subscription(db, _sub(db))      # closes out the welcome
    no_mail.clear()

    start = billing_trial._aware(sub.trial_start_date)
    for offset, expected in ((23, "trial_day_7"), (27, "trial_day_11"),
                             (29, "trial_day_13"), (30, "trial_expired")):
        billing_trial.process_subscription(db, _sub(db), now=start + timedelta(days=offset))
    assert [m for _, m, _ in no_mail] == ["trial_day_7", "trial_day_11",
                                          "trial_day_13", "trial_expired"]


def test_grandfathered_thirty_day_trial_is_not_expired_early(db, seeded, no_mail):
    """A 30-day trial must survive day 14. Shortening it would break a written promise."""
    _grandfather(db, started_days_ago=14, length=30)
    report = billing_trial.process_subscription(db, _sub(db))
    assert report["expired"] is False
    assert _sub(db).subscription_status == billing_trial.STATUS_TRIAL
    assert billing_trial.days_remaining(_sub(db)) == 16


# ══════════════════════════════════════════════════════════════════════════════
# 3-7. Each rung fires on its day, and day 14 expires the trial
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("age,expected", [
    (0,  ["trial_day_0"]),
    (6,  ["trial_day_0"]),
    (7,  ["trial_day_0", "trial_day_7"]),
    (11, ["trial_day_0", "trial_day_7", "trial_day_11"]),
    (13, ["trial_day_0", "trial_day_7", "trial_day_11", "trial_day_13"]),
])
def test_due_markers_by_age(db, seeded, age, expected):
    assert billing_trial.due_markers(_start_trial(db, days_ago=age)) == expected


def test_day_14_marks_the_trial_expired(db, seeded, no_mail):
    _start_trial(db, days_ago=14)
    report = billing_trial.process_subscription(db, _sub(db))
    assert report["expired"] is True
    assert _sub(db).subscription_status == billing_trial.STATUS_TRIAL_EXPIRED
    assert "trial_expired" in _markers(db)


def test_expiry_does_not_touch_stripe_or_start_dunning(db, seeded, no_mail):
    _start_trial(db, days_ago=14)
    billing_trial.process_subscription(db, _sub(db))
    sub = _sub(db)
    # No subscription created, no customer created, no dunning clock started.
    assert sub.stripe_subscription_id is None
    assert sub.stripe_customer_id is None
    assert sub.dunning_started_at is None


def test_a_backdated_trial_does_not_flood_five_emails(db, seeded, no_mail):
    """Grandfathering an old trial must send only the expiry mail, not the whole ladder."""
    _start_trial(db, days_ago=40)
    billing_trial.process_subscription(db, _sub(db))
    dispatched = [m for _, m, _ in no_mail]
    assert dispatched == ["trial_expired"]
    superseded = {r.day_marker for r in db.query(BillingDunningNotice)
                  .filter_by(status="superseded").all()}
    assert superseded == {"trial_day_0", "trial_day_7", "trial_day_11", "trial_day_13"}


# ══════════════════════════════════════════════════════════════════════════════
# 8 + 20. Idempotency, including across the two ladders
# ══════════════════════════════════════════════════════════════════════════════
def test_repeated_sweeps_send_no_duplicates(db, seeded, no_mail):
    _start_trial(db, days_ago=11)
    for _ in range(24):                      # a full day of hourly sweeps
        billing_trial.process_subscription(db, _sub(db))
    assert _markers(db) == ["trial_day_0", "trial_day_11", "trial_day_7"]
    assert len(no_mail) == 3


def test_dunning_recovery_does_not_delete_trial_notices(db, seeded, no_mail):
    """The two ladders share billing_dunning_notices. clear() must only drop its own.

    Without the marker filter in dunning.clear(), a customer who converted from trial and
    later recovered from one failed payment would have their trial history wiped and the
    trial emails sent a second time.
    """
    _start_trial(db, days_ago=13)
    billing_trial.process_subscription(db, _sub(db))
    trial_rows = _markers(db)
    assert trial_rows

    sub = _sub(db)
    sub.subscription_status = "past_due"
    sub.dunning_started_at = _t(3)
    db.commit()
    billing_dunning.process_subscription(db, _sub(db), send=False)
    assert "day_1" in _markers(db)

    billing_dunning.clear(db, _sub(db), commit=True)
    after = _markers(db)
    assert "day_1" not in after                      # dunning rows gone
    assert set(trial_rows).issubset(set(after))      # trial rows survived


def test_the_two_ladders_use_disjoint_markers():
    assert not billing_trial.TRIAL_MARKERS & set(billing_dunning.DUNNING_LADDER)


# ══════════════════════════════════════════════════════════════════════════════
# 9-11. Converted, cancelled, and card-added customers
# ══════════════════════════════════════════════════════════════════════════════
def test_paid_customer_receives_no_trial_email(db, seeded, no_mail):
    _start_trial(db, days_ago=13)
    sub = _sub(db)
    sub.subscription_status = "active"
    sub.stripe_subscription_id = "sub_live_1"
    db.commit()
    report = billing_trial.process_subscription(db, _sub(db))
    assert report["status"] == "converted"
    assert no_mail == []


def test_cancelled_customer_receives_no_trial_email(db, seeded, no_mail):
    _start_trial(db, days_ago=13)
    sub = _sub(db)
    sub.subscription_status = "cancel_pending"
    db.commit()
    assert billing_trial.process_subscription(db, _sub(db))["status"] == "not_on_trial"
    assert no_mail == []


def test_adding_a_card_alone_does_not_stop_the_ladder(db, seeded, no_mail):
    """A saved card is not a subscription. Reminders continue until they actually convert.

    Stopping on a card alone would leave a customer who saved a card but never chose a
    plan with no further contact, and their trial would still lapse.
    """
    _start_trial(db, days_ago=11)
    sub = _sub(db)
    sub.stripe_customer_id = "cus_card_only"        # card captured, no subscription
    db.commit()
    report = billing_trial.process_subscription(db, _sub(db))
    assert report["status"] == "ok"
    assert "trial_day_11" in report["sent"]


def test_expired_trial_is_not_swept_again(db, seeded, no_mail):
    _start_trial(db, days_ago=14)
    billing_trial.process_subscription(db, _sub(db))
    no_mail.clear()
    assert billing_trial.process_subscription(db, _sub(db))["status"] == "not_on_trial"
    assert no_mail == []


# ══════════════════════════════════════════════════════════════════════════════
# 12. Internal tenant
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_never_receives_a_trial_email(db, seeded, no_mail):
    _start_trial(db, org=ORG_INT, days_ago=13)
    report = billing_trial.process_subscription(db, _sub(db, ORG_INT))
    assert report["status"] == "internal_tenant"
    assert no_mail == []
    assert _markers(db, ORG_INT) == []


def test_internal_tenant_is_excluded_from_the_sweep_sql(db, seeded, no_mail):
    _start_trial(db, org=ORG_A, days_ago=7)
    _start_trial(db, org=ORG_INT, days_ago=7)
    billing_trial.run_sweep(db, send=False)
    assert _markers(db, ORG_A)
    assert _markers(db, ORG_INT) == []


def test_internal_tenant_trial_is_never_expired(db, seeded, no_mail):
    _start_trial(db, org=ORG_INT, days_ago=30)
    billing_trial.run_sweep(db, send=False)
    assert _sub(db, ORG_INT).subscription_status == billing_trial.STATUS_TRIAL


# ══════════════════════════════════════════════════════════════════════════════
# 13-14. Recipient resolution and opt-out
# ══════════════════════════════════════════════════════════════════════════════
def test_primary_billing_contact_wins_over_admin(db, seeded):
    db.add(BillingContact(organization_id=ORG_A, name="Second", email="second@a.test",
                          is_primary=False, receives_renewal_notices=True))
    db.add(BillingContact(organization_id=ORG_A, name="Primary", email="primary@a.test",
                          is_primary=True, receives_renewal_notices=True))
    db.commit()
    assert billing_trial.trial_recipient(db, ORG_A) == "primary@a.test"


def test_falls_back_to_an_active_admin(db, seeded):
    assert billing_trial.trial_recipient(db, ORG_A) == f"admin{ORG_A}@trial.test"


def test_an_ordinary_employee_is_never_selected(db, seeded):
    db.query(User).filter(User.organization_id == ORG_B).delete(synchronize_session=False)
    db.add(User(name="Worker", email="worker@b.test", organization_id=ORG_B,
                role="employee", is_active=True,
                billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    assert billing_trial.trial_recipient(db, ORG_B) is None


def test_renewal_notice_opt_out_is_respected(db, seeded):
    db.add(BillingContact(organization_id=ORG_A, name="Opted out", email="no@a.test",
                          is_primary=True, receives_renewal_notices=False))
    db.commit()
    # Must not silently fall through to the admin — that would defeat the opt-out.
    assert billing_trial.trial_recipient(db, ORG_A) is None


def test_no_recipient_fails_safely_and_records_once(db, seeded, no_mail):
    db.query(User).filter(User.organization_id == ORG_B).delete(synchronize_session=False)
    db.commit()
    _start_trial(db, org=ORG_B, days_ago=7)
    for _ in range(5):
        billing_trial.process_subscription(db, _sub(db, ORG_B))
    rows = db.query(BillingDunningNotice).filter_by(
        subscription_id=_sub(db, ORG_B).id).all()
    assert {r.status for r in rows} == {"failed"}
    assert {r.last_error for r in rows} == {"no_recipient"}
    assert len(rows) == 2                     # day_0 and day_7, recorded once each
    assert no_mail == []


def test_email_failure_does_not_block_expiry(db, seeded, monkeypatch):
    """A dead mail transport must not leave trials un-expired forever."""
    monkeypatch.setattr(trial_email, "send", lambda *a, **kw: (False, "smtp down"))
    _start_trial(db, days_ago=14)
    report = billing_trial.process_subscription(db, _sub(db))
    assert report["expired"] is True
    assert _sub(db).subscription_status == billing_trial.STATUS_TRIAL_EXPIRED


# ══════════════════════════════════════════════════════════════════════════════
# 15. What the billing page is given
# ══════════════════════════════════════════════════════════════════════════════
def test_summary_serializer_exposes_trial_end_date_and_remaining(db, seeded):
    from app.billing import router as billing_router
    _start_trial(db, days_ago=11)
    sub = _sub(db)
    plan = db.query(BillingPlan).filter_by(id=sub.plan_id).first()
    payload = billing_router._serialize_subscription(sub, plan)
    # The page previously read a `trial_ends_at` key this API has never emitted.
    assert "trial_end_date" in payload
    assert payload["trial_days_remaining"] == 3
    assert payload["is_trial"] is True
    assert payload["trial_expired"] is False


def test_summary_marks_an_expired_trial(db, seeded, no_mail):
    from app.billing import router as billing_router
    _start_trial(db, days_ago=14)
    billing_trial.process_subscription(db, _sub(db))
    sub = _sub(db)
    payload = billing_router._serialize_subscription(
        sub, db.query(BillingPlan).filter_by(id=sub.plan_id).first())
    assert payload["is_trial"] is False
    assert payload["trial_expired"] is True
    assert payload["trial_days_remaining"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Templates
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("marker", sorted(billing_trial.TRIAL_MARKERS))
def test_every_marker_has_a_template(marker):
    subject, html = trial_email.build(
        marker, company="Acme", ends="26 August 2026",
        plan_line="Verified at $8.00 per employee / month",
        billing_url="https://example.test/billing", remaining=3,
    )
    assert subject and html
    # Internal vocabulary must never reach a customer.
    for leaked in ("dunning", "state machine", "commercial", "internal tenant",
                   "billing_dunning_notices", "trial_day_"):
        assert leaked not in html.lower()


def test_expiry_email_states_no_charge_and_no_deletion():
    _, html = trial_email.build(
        "trial_expired", company="Acme", ends="26 August 2026",
        plan_line="Verified at $8.00 per employee / month",
        billing_url="https://example.test/billing",
    )
    assert "not been charged" in html
    assert "deleted" in html


def test_unknown_marker_is_rejected():
    with pytest.raises(ValueError):
        trial_email.build("trial_day_99", company="A", ends="x", plan_line="y",
                          billing_url="z")
