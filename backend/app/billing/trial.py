"""The 14-day free trial: one clock, one email ladder, one expiry.

Before this module there were **two** trial systems that did not know about each other:

* ``app/billing`` created a **14-day** trial (``billing_subscriptions.trial_start_date``
  / ``trial_end_date``), showed it on the billing page, sent nothing, and never expired
  it. A customer kept full access forever.
* ``app/commercial/trial_lifecycle`` ran a **30-day** ladder hourly, sent nine emails,
  and moved a parallel state machine through ``trial_active → trial_ending →
  trial_expired → trial_closed``.

The customer-visible countdown came from one system and the email came from the other,
16 days apart. This module makes the billing clock authoritative and is the only place
that sends trial mail. ``app/commercial`` is left untouched; its scheduler is simply no
longer started (see main.py).

Design, deliberately mirroring ``app/billing/dunning.py`` so there is one shape to
learn:

* **The dates are the state.** Which emails are due, and whether the trial is over, are
  both derived from ``trial_start_date`` / ``trial_end_date``. No separate "trial
  stage" column exists to drift out of agreement.
* **Duplicate suppression is a database constraint**, not a Python check. Trial notices
  live in ``billing_dunning_notices`` under ``UNIQUE (subscription_id, day_marker)``
  with a ``trial_`` marker prefix. The sweep runs hourly; without that, day 0 would
  send 24 emails. Reusing that table is why this phase needs no migration.
* **Expiry never touches Stripe.** No subscription is created, no card is charged, no
  Stripe object is read or written anywhere in this module. Expiry is a single local
  status change.
* **Internal tenants are excluded twice** — in the sweep's SQL and again per
  subscription, so the platform owner can never be sent a trial email.
* **A backdated trial does not flood.** If a trial is already past its end date when
  first swept (the grandfathering case), the intermediate reminders are recorded as
  ``superseded`` without being sent and only the expiry email goes out. Otherwise
  creating a row for an old trial would fire five emails at once.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.trial")

# ── The single source of truth for trial length ──────────────────────────────
# ``service.ensure_subscription`` imports this, so the stored trial_end_date and
# everything derived here cannot disagree. Temveris's trial is 14 days
# (confirmed 2026-08-18); app/commercial's TRIAL_LENGTH_DAYS = 30 is no longer
# authoritative for customers.
TRIAL_LENGTH_DAYS = 14

# Marker → day offset from trial_start_date. Kept because the marker names are the
# stored vocabulary, and because a 14-day trial's schedule is identical either way.
TRIAL_LADDER: Dict[str, int] = {
    "trial_day_0":   0,    # welcome
    "trial_day_7":   7,    # ~7 days remaining
    "trial_day_11":  11,   # ~3 days remaining
    "trial_day_13":  13,   # ends tomorrow
    "trial_expired": TRIAL_LENGTH_DAYS,
}

# Reminders are scheduled by **days remaining**, not days elapsed.
#
# On a 14-day trial the two are the same thing (14-7=7, 14-3=11, 14-1=13), which is why
# the marker names still read as day numbers. They diverge the moment a trial is not 14
# days long — a grandfathered 30-day trial, or one an operator extended. Anchored to the
# start, ``trial_day_13`` would fire on day 13 of a 30-day trial and tell the customer
# "your trial ends tomorrow" seventeen days early. Anchored to the end, every reminder
# says something true for any trial length.
REMINDERS_BEFORE_END: Dict[str, int] = {
    "trial_day_7":  7,
    "trial_day_11": 3,
    "trial_day_13": 1,
}

# The welcome email is the one rung genuinely tied to the start.
START_MARKER = "trial_day_0"

# Every marker this module owns. ``dunning.clear()`` must not delete these, and this
# module must not delete dunning's — the two ladders share one table.
TRIAL_MARKERS = frozenset(TRIAL_LADDER)

# The marker sent when the trial is over. Reminders older than this are pointless once
# it has passed.
EXPIRY_MARKER = "trial_expired"

# Local status meaning "was on trial, trial ran out, never paid". Stored in
# ``subscription_status`` (VARCHAR(30)) alongside the existing vocabulary, so no schema
# change is needed. A later Stripe checkout overwrites it with "active" through the
# normal webhook path — nothing here has to undo it.
STATUS_TRIAL = "trial"
STATUS_TRIAL_EXPIRED = "trial_expired"

# Statuses that mean the customer already converted. A paying customer must never
# receive trial mail.
PAID_STATUSES = frozenset({"active", "past_due", "unpaid", "incomplete",
                           "enterprise_contract"})

# Statuses where the trial is deliberately over and no further mail is wanted.
STOPPED_STATUSES = frozenset({"cancel_pending", "canceled", "cancelled",
                              "incomplete_expired", STATUS_TRIAL_EXPIRED})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns tz-aware, SQLite naive. Compare in UTC either way."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# The clock
# ══════════════════════════════════════════════════════════════════════════════
def expires_at(sub: models.BillingSubscription) -> Optional[datetime]:
    """When this trial ends.

    ``trial_end_date`` is authoritative because it is what the customer was shown and
    what the billing page displays. It is only recomputed when absent, so an
    intentionally extended trial (an operator moving the date out) is honoured rather
    than silently reset to start + 14.
    """
    end = _aware(sub.trial_end_date)
    if end is not None:
        return end
    start = _aware(sub.trial_start_date)
    return None if start is None else start + timedelta(days=TRIAL_LENGTH_DAYS)


def age_days(sub: models.BillingSubscription, *,
             now: Optional[datetime] = None) -> Optional[int]:
    """Whole days since the trial began, or None when there is no start date."""
    start = _aware(sub.trial_start_date)
    if start is None:
        return None
    return max(0, int(((now or _now()) - start).total_seconds() // 86400))


def days_remaining(sub: models.BillingSubscription, *,
                   now: Optional[datetime] = None) -> Optional[int]:
    """Days left, as a customer would count them. Never negative.

    Rounds **up**, deliberately. A trial with 2 days and 23 hours left has "3 days"
    remaining in the only sense a customer means it — flooring would call that 2, and
    would report "0 days remaining" for the whole of the final day while the trial was
    still live. Rounding up also matches how the billing page counted before this, so the
    number does not change under the customer as the server takes over the calculation.
    """
    end = expires_at(sub)
    if end is None:
        return None
    delta = end - (now or _now())
    if delta.total_seconds() <= 0:
        return 0
    return max(0, math.ceil(delta.total_seconds() / 86400))


def is_expired(sub: models.BillingSubscription, *,
               now: Optional[datetime] = None) -> bool:
    end = expires_at(sub)
    return end is not None and (now or _now()) >= end


def is_on_trial(sub: models.BillingSubscription, *,
                now: Optional[datetime] = None) -> bool:
    """On an active, unexpired trial. Excludes converted and expired customers."""
    if (sub.subscription_status or "").strip().lower() != STATUS_TRIAL:
        return False
    if sub.trial_start_date is None:
        return False
    return not is_expired(sub, now=now)


def has_converted(sub: models.BillingSubscription) -> bool:
    """True once there is a real Stripe subscription behind this row.

    Both conditions are required: a ``stripe_subscription_id`` alone can be left over
    from an earlier experiment, and a paid-looking status alone can be a stale local
    value. Together they mean the customer is genuinely on a paid plan.
    """
    status = (sub.subscription_status or "").strip().lower()
    return bool(sub.stripe_subscription_id) and status in PAID_STATUSES


def scheduled_at(sub: models.BillingSubscription,
                 marker: str) -> Optional[datetime]:
    """When this marker is due for this subscription, or None if undeterminable.

    The welcome is at the start; every reminder and the expiry are measured back from the
    real end date, so the ladder is correct for a 14-day trial, a grandfathered 30-day
    one, and anything an operator extends.
    """
    if marker == START_MARKER:
        return _aware(sub.trial_start_date)
    end = expires_at(sub)
    if end is None:
        return None
    if marker == EXPIRY_MARKER:
        return end
    before = REMINDERS_BEFORE_END.get(marker)
    return None if before is None else end - timedelta(days=before)


def due_markers(sub: models.BillingSubscription, *,
                now: Optional[datetime] = None) -> List[str]:
    """Markers whose scheduled moment has arrived, earliest first.

    Independent of what was already sent — the caller filters against the notice table.
    """
    at = now or _now()
    due = []
    for marker in TRIAL_LADDER:
        when = scheduled_at(sub, marker)
        if when is not None and at >= when:
            due.append((when, marker))
    return [m for _, m in sorted(due)]


# ══════════════════════════════════════════════════════════════════════════════
# Recipient
# ══════════════════════════════════════════════════════════════════════════════
def trial_recipient(db: Session, org_id: int) -> Optional[str]:
    """Who receives trial mail for this organization, or None.

    Order: the primary billing contact, then any other billing contact, then the
    lowest-id active owner / billing_admin / admin.

    ``receives_renewal_notices`` is honoured for contacts — a trial reminder is a
    renewal notice, and that flag exists precisely so a contact can opt out of this
    class of mail. The admin fallback has no such flag; it is the account's responsible
    party and is only used when no contact is configured at all.

    Role is filtered, so an ordinary employee can never be selected.
    """
    contacts = (
        db.query(models.BillingContact)
          .filter(models.BillingContact.organization_id == org_id)
          .order_by(models.BillingContact.is_primary.desc(),
                    models.BillingContact.id)
          .all()
    )
    for contact in contacts:
        if contact.email and contact.receives_renewal_notices is not False:
            return contact.email
    # Contacts exist but every one has opted out — respect that and do not fall
    # through to an admin, which would defeat the opt-out.
    if contacts:
        _log.info("trial: all billing contacts for org=%s opted out of renewal notices",
                  org_id)
        return None

    admin = (
        db.query(models.User)
          .filter(models.User.organization_id == org_id,
                  models.User.role.in_(["owner", "billing_admin", "admin"]),
                  models.User.is_active == True)                  # noqa: E712
          .order_by(models.User.id)
          .first()
    )
    return getattr(admin, "email", None) if admin else None


# ══════════════════════════════════════════════════════════════════════════════
# Notices — shared table, trial-prefixed markers
# ══════════════════════════════════════════════════════════════════════════════
def sent_markers(db: Session, sub: models.BillingSubscription) -> set:
    """Trial markers already recorded for this subscription.

    Filtered to ``TRIAL_MARKERS`` so a dunning row can never be mistaken for a trial
    notice, even though both live in ``billing_dunning_notices``.
    """
    return {
        r.day_marker for r in
        db.query(models.BillingDunningNotice)
          .filter(models.BillingDunningNotice.subscription_id == sub.id,
                  models.BillingDunningNotice.day_marker.in_(sorted(TRIAL_MARKERS)))
          .all()
    }


def record_notice(db: Session, sub: models.BillingSubscription, marker: str, *,
                  recipient: Optional[str], status: str = "sent",
                  error: Optional[str] = None) -> bool:
    """Record that a trial notice was handled. False when already recorded.

    Delegates to the dunning recorder so there is one INSERT path, one SAVEPOINT, and
    one place where the UNIQUE constraint is relied upon as the real guard against a
    concurrent sweep.
    """
    from app.billing import dunning as billing_dunning        # noqa: PLC0415
    return billing_dunning.record_notice(
        db, sub, marker, recipient=recipient, status=status, error=error,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Expiry
# ══════════════════════════════════════════════════════════════════════════════
def expire(db: Session, sub: models.BillingSubscription, *,
           commit: bool = False) -> bool:
    """Mark an ended trial as expired. Local status change only.

    Explicitly does NOT: create a Stripe subscription, charge anyone, attach or detach a
    payment method, cancel a Stripe object, or start dunning. Dunning is for a *failed
    payment*; a trial that ran out never had one.

    Access is not revoked here either. Whether an expired trial should lose write
    access is a product decision, and the read-only machinery already exists
    (``app/billing/suspension.py``) if that is later wanted — but wiring it in would
    silently change what customers can do, so it is left to an explicit decision.
    """
    if (sub.subscription_status or "").strip().lower() != STATUS_TRIAL:
        return False
    if not is_expired(sub):
        return False
    sub.subscription_status = STATUS_TRIAL_EXPIRED
    db.flush()
    if commit:
        db.commit()
    _log.info("trial EXPIRED org=%s ended=%s", sub.organization_id, expires_at(sub))
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Sweep
# ══════════════════════════════════════════════════════════════════════════════
def process_subscription(db: Session, sub: models.BillingSubscription, *,
                         now: Optional[datetime] = None,
                         send: bool = True) -> Dict[str, Any]:
    """Send whatever trial mail is due for one subscription, then expire it if over.

    ``send=False`` records notices without dispatching mail — used by tests and by a
    dry run so nothing can reach a real customer inadvertently.
    """
    from app.billing import trial_email                        # noqa: PLC0415

    result: Dict[str, Any] = {"organization_id": sub.organization_id, "sent": [],
                              "skipped": [], "expired": False, "status": "ok"}

    # Guard 1 of 2 for internal tenants (the sweep's SQL is the other). Kept here so a
    # direct call from a script or a test cannot bypass it.
    if billing_service.is_internal_tenant(db, sub.organization_id):
        result["status"] = "internal_tenant"
        return result
    if has_converted(sub):
        result["status"] = "converted"
        return result
    status = (sub.subscription_status or "").strip().lower()
    if status in STOPPED_STATUSES:
        result["status"] = "not_on_trial"
        return result
    if status != STATUS_TRIAL or sub.trial_start_date is None:
        result["status"] = "not_on_trial"
        return result

    already = sent_markers(db, sub)
    recipient = trial_recipient(db, sub.organization_id)
    due = [m for m in due_markers(sub, now=now) if m not in already]
    expired_now = is_expired(sub, now=now)

    created = _aware(sub.created_at)

    for marker in due:
        # ── Never back-send ───────────────────────────────────────────────────
        # A marker whose scheduled moment predates the subscription row itself could
        # never legitimately have fired: the row did not exist yet. This is what stops a
        # grandfathered customer — a row created today for a trial that began weeks ago —
        # being welcomed to a trial they are already halfway through, and what stops the
        # whole ladder arriving at once. Recorded, not skipped, so the marker is closed
        # out permanently and the hourly sweep does not reconsider it.
        #
        # A normally created row has created_at == trial_start_date, so the welcome is
        # scheduled exactly at creation and still sends.
        when = scheduled_at(sub, marker)
        if (created is not None and when is not None and when < created
                and marker != EXPIRY_MARKER):
            record_notice(db, sub, marker, recipient=recipient, status="superseded",
                          error="scheduled before this record existed — not back-sent")
            result["skipped"].append(marker)
            continue
        # Once the trial is over the reminders are false ("3 days remaining" when it
        # ended last week), so only the expiry email goes out.
        if expired_now and marker != EXPIRY_MARKER:
            record_notice(db, sub, marker, recipient=recipient, status="superseded",
                          error="trial already ended when first swept")
            result["skipped"].append(marker)
            continue
        if not recipient:
            # Nobody to write to. Recorded rather than skipped silently: the trial ends
            # either way, and "we could not reach anyone" is what an operator needs to
            # see. Recording also stops the hourly sweep re-logging this forever.
            _log.error("trial: no recipient for org=%s — cannot send %s",
                       sub.organization_id, marker)
            record_notice(db, sub, marker, recipient=None,
                          status="failed", error="no_recipient")
            result["skipped"].append(marker)
            continue

        ok, error = (True, None)
        if send:
            ok, error = trial_email.send(db, sub, marker=marker, recipient=recipient)
        if record_notice(db, sub, marker, recipient=recipient,
                         status="sent" if ok else "failed", error=error):
            result["sent"].append(marker)
        else:
            result["skipped"].append(marker)

    # Expire last, so the expiry email is sent while the row still reads "trial".
    if expired_now:
        result["expired"] = expire(db, sub)

    result["age_days"] = age_days(sub, now=now)
    result["days_remaining"] = days_remaining(sub, now=now)
    return result


def run_sweep(db: Session, *, now: Optional[datetime] = None,
              send: bool = True) -> Dict[str, Any]:
    """Advance every organization currently on trial.

    Internal tenants are excluded in the query itself, so the sweep can never email or
    expire the platform owner's workspace even if its status were somehow set to trial.
    """
    tally = {"checked": 0, "sent": 0, "expired": 0, "reports": []}
    subs = (
        db.query(models.BillingSubscription)
          .join(models.Organization,
                models.Organization.id == models.BillingSubscription.organization_id)
          .filter(models.BillingSubscription.subscription_status == STATUS_TRIAL,
                  models.BillingSubscription.trial_start_date.isnot(None),
                  models.BillingSubscription.organization_id.isnot(None),
                  models.Organization.billing_type != billing_service.BILLING_TYPE_INTERNAL)
          .all()
    )
    for sub in subs:
        tally["checked"] += 1
        report = process_subscription(db, sub, now=now, send=send)
        tally["sent"] += len(report.get("sent", []))
        if report.get("expired"):
            tally["expired"] += 1
        if report.get("sent") or report.get("status") != "ok":
            tally["reports"].append(report)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        _log.error("trial sweep commit failed: %s", exc)
        tally["error"] = str(exc)
    _log.info("trial sweep: %s", {k: v for k, v in tally.items() if k != "reports"})
    return tally


def run_once_sync() -> Dict[str, Any]:                 # pragma: no cover - runtime path
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return run_sweep(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = 3600,
                      initial_delay_seconds: int = 0):    # pragma: no cover - runtime path
    # Environment gate. The task is still created so the shutdown handler stays
    # valid, but it exits immediately outside production: these sweeps send real
    # mail and can modify Stripe, and "Trial ladder" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Trial ladder not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except Exception as exc:
            _log.error("trial sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)
