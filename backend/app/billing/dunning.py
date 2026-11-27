"""Failed-payment handling: warn, grant a grace period, then restrict.

Before this, a failed payment produced exactly one thing: a red banner on the billing
page. No email, no grace period, no consequence. A customer whose card failed kept
full service indefinitely, unpaid, and was never told.

The ladder (approved 2026-08-13):

    day 1   warning email
    day 3   warning email
    day 7   warning email
    day 14  final email + organization becomes billing-suspended (read-only)

Design notes:

* **The anchor is the state.** ``BillingSubscription.dunning_started_at`` records when
  the current failure run began; which warnings are due and whether the grace period
  has expired are both derived from it. There is no separate "suspended" flag that
  could disagree, and clearing the anchor restores the tenant atomically.
* **Suspension also requires Stripe to still be unhappy.** A stale anchor alone never
  suspends anyone — ``derive`` checks the live subscription status too.
* **Duplicate suppression is a database constraint**, not a Python check:
  ``UNIQUE (subscription_id, day_marker)`` on ``billing_dunning_notices``. The sweep
  runs hourly; without that, day 1 would send 24 emails.
* **The Stripe subscription is never cancelled here** (approved decision 5). It stays
  active until payment is recovered or someone acts manually.
* **``SUSPENDED_BILLABLE`` is deliberately not used** (decision 8). That is a
  *per-worker* state meaning "access blocked, still billed"; payment failure is an
  *organization-level* condition. Conflating them would corrupt seat counting.
* **Internal tenants never enter dunning** — excluded in the sweep's SQL, and
  ``begin`` refuses them outright.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.dunning")

# Days after the first failure at which each warning is due.
DUNNING_LADDER: Dict[str, int] = {
    "day_1": 1,
    "day_3": 3,
    "day_7": 7,
    "day_14": 14,
}

# Grace period. On this day the final warning goes out and access becomes read-only.
GRACE_PERIOD_DAYS = 14

# Stripe-derived local statuses that mean "we are not being paid".
UNPAID_STATUSES = frozenset({"past_due", "unpaid", "incomplete"})

# Terminal states — a cancelled subscription is not in dunning, it is over.
TERMINAL_STATUSES = frozenset({"canceled", "incomplete_expired"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns tz-aware, SQLite naive. Compare in UTC either way."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_unpaid(sub: models.BillingSubscription) -> bool:
    return (sub.subscription_status or "").strip().lower() in UNPAID_STATUSES


def age_days(sub: models.BillingSubscription, *, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days since the failure run began, or None when not in dunning."""
    anchor = _aware(sub.dunning_started_at)
    if anchor is None:
        return None
    return max(0, int(((now or _now()) - anchor).total_seconds() // 86400))


def due_markers(sub: models.BillingSubscription, *,
                now: Optional[datetime] = None) -> List[str]:
    """Markers whose day has arrived, oldest first. Independent of what was sent."""
    age = age_days(sub, now=now)
    if age is None:
        return []
    return [m for m, d in sorted(DUNNING_LADDER.items(), key=lambda kv: kv[1]) if age >= d]


def grace_expires_at(sub: models.BillingSubscription) -> Optional[datetime]:
    anchor = _aware(sub.dunning_started_at)
    return None if anchor is None else anchor + timedelta(days=GRACE_PERIOD_DAYS)


def is_suspended(db: Session, sub: models.BillingSubscription, *,
                 now: Optional[datetime] = None) -> bool:
    """True when this organization is billing-suspended (read-only).

    Requires *both* an expired grace period and Stripe still reporting non-payment, so
    a stale anchor can never suspend a tenant who has actually paid. Internal tenants
    are never suspended for billing.
    """
    if sub.dunning_started_at is None:
        return False
    if not is_unpaid(sub):
        return False
    if billing_service.is_internal_tenant(db, sub.organization_id):
        return False
    expires = grace_expires_at(sub)
    return expires is not None and (now or _now()) >= expires


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle: begin / clear
# ══════════════════════════════════════════════════════════════════════════════
def begin(db: Session, sub: models.BillingSubscription, *,
          now: Optional[datetime] = None, commit: bool = False) -> bool:
    """Start the dunning clock if it is not already running.

    Idempotent: a second failure event while already in dunning must not reset the
    clock, or a customer could be kept in a permanent grace period by repeated failed
    attempts and never actually be restricted.
    """
    if sub.dunning_started_at is not None:
        return False
    if billing_service.is_internal_tenant(db, sub.organization_id):
        _log.info("dunning: refusing to start for internal tenant org=%s",
                  sub.organization_id)
        return False
    if (sub.subscription_status or "").strip().lower() in TERMINAL_STATUSES:
        return False

    sub.dunning_started_at = now or _now()
    db.flush()
    if commit:
        db.commit()
    _log.warning("dunning STARTED org=%s subscription=%s status=%s",
                 sub.organization_id, sub.stripe_subscription_id, sub.subscription_status)
    return True


def clear(db: Session, sub: models.BillingSubscription, *, reason: str = "payment_recovered",
          commit: bool = False) -> bool:
    """Stop dunning and restore the organization.

    Clears the anchor (which un-suspends, since suspension is derived) and deletes the
    sent-notice rows so a genuinely new failure cycle can warn again from day 1.
    Deliberately does not touch ``subscription_status`` — that mirrors Stripe and is
    owned by the webhook layer.

    Only *dunning* markers are deleted. ``billing_dunning_notices`` is shared with the
    trial ladder (``app/billing/trial.py``, ``trial_*`` markers), and this used to
    delete every row for the subscription. A customer who converted from trial and later
    recovered from one failed payment would have had their trial history wiped, and the
    trial emails could then be sent a second time. Filtering to this ladder's own
    markers keeps the two independent.
    """
    if sub.dunning_started_at is None:
        return False
    (db.query(models.BillingDunningNotice)
       .filter(models.BillingDunningNotice.subscription_id == sub.id,
               models.BillingDunningNotice.day_marker.in_(sorted(DUNNING_LADDER)))
       .delete(synchronize_session=False))
    sub.dunning_started_at = None
    db.flush()
    if commit:
        db.commit()
    _log.info("dunning CLEARED org=%s subscription=%s reason=%s",
              sub.organization_id, sub.stripe_subscription_id, reason)
    return True


def sync_from_status(db: Session, sub: models.BillingSubscription, *,
                     now: Optional[datetime] = None, commit: bool = False) -> str:
    """Align the dunning clock with the subscription's current Stripe-derived status.

    Called from the webhook layer after ``apply_subscription_state``, so recovery and
    onset both follow Stripe rather than the mere existence of an event.
    Returns: started | cleared | unchanged.
    """
    status = (sub.subscription_status or "").strip().lower()
    if status in UNPAID_STATUSES:
        return "started" if begin(db, sub, now=now, commit=commit) else "unchanged"
    # Anything else — including terminal states — ends the dunning run. A cancelled
    # subscription is not "in grace"; there is nothing left to recover.
    return "cleared" if clear(db, sub, reason=f"status={status}", commit=commit) else "unchanged"


# ══════════════════════════════════════════════════════════════════════════════
# Notices
# ══════════════════════════════════════════════════════════════════════════════
def sent_markers(db: Session, sub: models.BillingSubscription) -> set:
    """Dunning markers already recorded for this subscription.

    Scoped to this ladder's markers because the table is shared with the trial ladder.
    The two vocabularies do not currently overlap, so this changes no behaviour — it
    states the invariant so a future marker name cannot quietly break it.
    """
    return {
        r.day_marker for r in
        db.query(models.BillingDunningNotice)
          .filter(models.BillingDunningNotice.subscription_id == sub.id,
                  models.BillingDunningNotice.day_marker.in_(sorted(DUNNING_LADDER)))
          .all()
    }


def _billing_recipient(db: Session, org_id: int) -> Optional[str]:
    """Who receives dunning mail: the billing contact, else an org admin/owner."""
    contact = (
        db.query(models.BillingContact)
          .filter(models.BillingContact.organization_id == org_id)
          .order_by(models.BillingContact.id)
          .first()
    )
    email = getattr(contact, "email", None) if contact else None
    if email:
        return email
    admin = (
        db.query(models.User)
          .filter(models.User.organization_id == org_id,
                  models.User.role.in_(["owner", "billing_admin", "admin"]),
                  models.User.is_active == True)                  # noqa: E712
          .order_by(models.User.id)
          .first()
    )
    return getattr(admin, "email", None) if admin else None


def record_notice(db: Session, sub: models.BillingSubscription, marker: str, *,
                  recipient: Optional[str], status: str = "sent",
                  error: Optional[str] = None) -> bool:
    """Record that a notice was sent. False when it was already recorded.

    The UNIQUE constraint is the real guard: two concurrent sweeps both pass the
    membership check, then exactly one INSERT survives. Wrapped in a SAVEPOINT so the
    loser does not poison the surrounding transaction.
    """
    row = models.BillingDunningNotice(
        subscription_id=sub.id,
        organization_id=sub.organization_id,
        day_marker=marker,
        status=status,
        recipient_email=recipient,
        last_error=error,
        dunning_started_at=sub.dunning_started_at,
        sent_at=_now(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return True
    except IntegrityError:
        _log.info("dunning notice %s already recorded for subscription %s",
                  marker, sub.id)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Sweep
# ══════════════════════════════════════════════════════════════════════════════
def process_subscription(db: Session, sub: models.BillingSubscription, *,
                         now: Optional[datetime] = None,
                         send: bool = True) -> Dict[str, Any]:
    """Send whatever warnings are now due for one subscription.

    ``send=False`` records notices without dispatching mail — used by tests and by a
    dry run, so nothing reaches a real customer inadvertently.
    """
    from app.billing import dunning_email        # noqa: PLC0415 - avoid import cycle

    result: Dict[str, Any] = {"organization_id": sub.organization_id, "sent": [],
                              "skipped": [], "suspended": False, "status": "ok"}

    if billing_service.is_internal_tenant(db, sub.organization_id):
        result["status"] = "internal_tenant"
        return result
    if sub.dunning_started_at is None:
        result["status"] = "not_in_dunning"
        return result
    if not is_unpaid(sub):
        # Stripe is happy again but the anchor survived (a missed webhook). Recover.
        clear(db, sub, reason="status_recovered")
        result["status"] = "recovered"
        return result

    already = sent_markers(db, sub)
    recipient = _billing_recipient(db, sub.organization_id)

    for marker in due_markers(sub, now=now):
        if marker in already:
            result["skipped"].append(marker)
            continue
        if not recipient:
            # No billing contact and no admin to warn. Record the attempt as failed
            # rather than skipping silently: the ladder must still be auditable (the
            # tenant will be suspended on day 14 either way, and "we could not reach
            # anyone" is exactly what an operator needs to see), and recording stops
            # the hourly sweep re-logging the same error forever.
            _log.error("dunning: no recipient for org=%s — cannot send %s",
                       sub.organization_id, marker)
            record_notice(db, sub, marker, recipient=None,
                          status="failed", error="no_recipient")
            result["skipped"].append(marker)
            continue

        ok, error = (True, None)
        if send:
            ok, error = dunning_email.send(db, sub, marker=marker, recipient=recipient)
        if record_notice(db, sub, marker, recipient=recipient,
                         status="sent" if ok else "failed", error=error):
            result["sent"].append(marker)
        else:
            result["skipped"].append(marker)

    result["suspended"] = is_suspended(db, sub, now=now)
    result["age_days"] = age_days(sub, now=now)
    return result


def run_sweep(db: Session, *, now: Optional[datetime] = None,
              send: bool = True) -> Dict[str, Any]:
    """Advance every organization currently in dunning.

    Internal tenants are excluded in the query itself, so a sweep can never email or
    suspend the platform owner even if an anchor were somehow set.
    """
    tally = {"checked": 0, "sent": 0, "recovered": 0, "suspended": 0, "reports": []}
    subs = (
        db.query(models.BillingSubscription)
          .join(models.Organization,
                models.Organization.id == models.BillingSubscription.organization_id)
          .filter(models.BillingSubscription.dunning_started_at.isnot(None),
                  models.BillingSubscription.organization_id.isnot(None),
                  models.Organization.billing_type != billing_service.BILLING_TYPE_INTERNAL)
          .all()
    )
    for sub in subs:
        tally["checked"] += 1
        report = process_subscription(db, sub, now=now, send=send)
        tally["sent"] += len(report.get("sent", []))
        if report.get("status") == "recovered":
            tally["recovered"] += 1
        if report.get("suspended"):
            tally["suspended"] += 1
        if report.get("sent") or report.get("status") != "ok":
            tally["reports"].append(report)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        _log.error("dunning sweep commit failed: %s", exc)
        tally["error"] = str(exc)
    _log.info("dunning sweep: %s", {k: v for k, v in tally.items() if k != "reports"})
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
    # mail and can modify Stripe, and "Dunning ladder" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Dunning ladder not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except Exception as exc:
            _log.error("dunning sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)
