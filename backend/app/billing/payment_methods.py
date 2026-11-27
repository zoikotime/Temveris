"""Mirror Stripe payment methods into ``billing_payment_methods``.

Temveris used to *invent* payment methods. ``POST /payment-method`` accepted a
brand picked from a dropdown, four digits typed by hand, and an expiry date — then
stored the row with ``provider="stripe"`` and ``verification_status="verified"``.
Stripe knew nothing about it, nothing was verified, and no card could be charged.

Meanwhile real cards *did* exist: every customer who completed Checkout has a card
attached to their Stripe customer and set as ``invoice_settings.default_payment_method``.
``billing_payment_methods`` held zero rows, so the UI showed "no card on file" to
customers whose card Stripe was already charging.

Payment methods are therefore mirrored, exactly like invoices (Phase 6):

* Stripe is authoritative. Brand, last four, expiry and default status all come from
  Stripe; nothing is self-declared.
* Rows are keyed by ``stripe_payment_method_id``, so a re-sync updates in place.
* A card detached at Stripe is removed from the mirror, so "remove card" cannot leave
  a stale row implying a card is on file.
* Raw card numbers and CVC never reach Temveris. Stripe's hosted Setup Session
  collects them; we only ever see the resulting ``pm_...`` reference.
* Internal tenants are skipped entirely — they have no Stripe relationship.

Rows with ``stripe_payment_method_id IS NULL`` are legacy hand-entered records. They
are never updated or deleted here: they are what a customer was shown, and removing
them is a data decision rather than a sync side effect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.payment_methods")

# Subscription states in which a customer must keep a way to pay. Deleting the last
# card while one of these is in force would silently guarantee the next renewal fails.
LIVE_SUBSCRIPTION_STATES = frozenset({
    "active", "trial", "trialing", "past_due", "cancel_pending", "incomplete",
})


def _get(obj: Any, key: str, default=None):
    """StripeObject shadows .get(), so read by key presence."""
    try:
        if key in obj:
            return obj[key]
    except Exception:
        pass
    return default


def _plain(value: Any) -> Any:
    """Convert a StripeObject into JSON-serialisable primitives.

    ``billing_details.address`` comes back as a Stripe ``Address`` object, not a dict.
    Assigning it straight to the JSON column raises
    ``TypeError: Object of type Address is not JSON serializable`` at INSERT time — a
    failure invisible to a hand-built test fixture, which is already a plain dict.

    Conversion goes through ``json.loads(str(obj))`` because on stripe-python 15.x a
    StripeObject is deliberately awkward to treat as a mapping: it is *not* a dict
    subclass, ``.keys()``/``.items()`` raise AttributeError (``__getattr__`` shadows
    them), iterating it indexes by integer and raises KeyError, and
    ``to_dict_recursive`` does not exist. Its ``__str__`` is documented JSON, so that
    is the one dependable route.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    try:
        import json                                  # noqa: PLC0415
        return json.loads(str(value))
    except Exception:
        # Never let an unexpected shape break the mirror — the address is cosmetic.
        _log.warning("payment method sync: could not serialise %s", type(value).__name__)
        return {}


def _apply(pm: Any, row: models.BillingPaymentMethod, org_id: int,
           default_pm_id: Optional[str]) -> None:
    """Copy Stripe's card facts onto a local row. Stripe owns every one of them."""
    card = _get(pm, "card") or {}
    billing = _get(pm, "billing_details") or {}

    row.organization_id          = org_id
    row.provider                 = "stripe"
    row.stripe_payment_method_id = _get(pm, "id")
    # The Stripe reference *is* the token reference; there is nothing else to store.
    row.token_reference          = _get(pm, "id")
    row.payment_type             = _get(pm, "type") or "card"
    row.brand                    = _get(card, "brand")
    row.last_four                = _get(card, "last4")
    row.expiry_month             = _get(card, "exp_month")
    row.expiry_year              = _get(card, "exp_year")
    row.billing_name             = _get(billing, "name")
    # _plain(): Stripe returns an Address object here, which the JSON column cannot
    # serialise. See the note on _plain.
    row.billing_address_json     = _plain(_get(billing, "address")) or {}
    row.is_default               = bool(default_pm_id and _get(pm, "id") == default_pm_id)
    # Stripe attached it, so it passed Stripe's checks. Previously this said "verified"
    # for a card nobody had ever seen.
    row.verification_status      = "verified"


def _default_payment_method_id(customer: Any) -> Optional[str]:
    settings = _get(customer, "invoice_settings") or {}
    value = _get(settings, "default_payment_method")
    if isinstance(value, str):
        return value
    # Expanded objects carry the id under .id
    return _get(value, "id") if value else None


def sync_org_payment_methods(db: Session, org_id: Optional[int], *, stripe_client=None,
                             commit: bool = True) -> Dict[str, Any]:
    """Mirror this organization's Stripe cards locally.

    Returns a status dict; never raises, so a webhook or background job cannot fail
    because of payment-method mirroring. Statuses:
      no_organization | no_subscription | no_stripe_customer | internal_tenant
      | stripe_unavailable | synced

    ``commit=False`` flushes instead of committing, for callers that own the
    transaction boundary (see Phase 6's rationale in invoices.py).
    """
    try:
        org_id = billing_service.require_org_id(org_id)
    except billing_service.MissingOrganization:
        return {"status": "no_organization"}

    if billing_service.is_internal_tenant(db, org_id):
        return {"status": "internal_tenant", "organization_id": org_id}

    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        return {"status": "no_subscription", "organization_id": org_id}
    if not sub.stripe_customer_id:
        return {"status": "no_stripe_customer", "organization_id": org_id}

    if stripe_client is None:                     # pragma: no cover - runtime path
        import stripe as stripe_client            # noqa: PLC0415

    try:
        listed = stripe_client.PaymentMethod.list(customer=sub.stripe_customer_id,
                                                 type="card")
        remote = list(listed["data"])
        customer = stripe_client.Customer.retrieve(sub.stripe_customer_id)
    except Exception as exc:
        _log.error("payment method sync: Stripe read failed org=%s customer=%s: %s",
                   org_id, sub.stripe_customer_id, exc)
        return {"status": "stripe_unavailable", "organization_id": org_id,
                "error": str(exc)}

    default_pm_id = _default_payment_method_id(customer)

    created = updated = skipped = removed = 0
    seen: List[str] = []
    for pm in remote:
        pm_id = _get(pm, "id")
        if not pm_id:
            skipped += 1
            continue

        # Ownership: only ever attach a card whose Stripe customer is this org's.
        if _get(pm, "customer") not in (None, sub.stripe_customer_id):
            _log.error("payment method sync: refusing %s — customer %s != org %s's %s",
                       pm_id, _get(pm, "customer"), org_id, sub.stripe_customer_id)
            skipped += 1
            continue

        row = (
            db.query(models.BillingPaymentMethod)
              .filter(models.BillingPaymentMethod.stripe_payment_method_id == pm_id)
              .first()
        )
        if row is None:
            row = models.BillingPaymentMethod()
            db.add(row)
            created += 1
        else:
            if row.organization_id is not None and row.organization_id != org_id:
                _log.error("payment method sync: %s already belongs to org %s, not %s",
                           pm_id, row.organization_id, org_id)
                skipped += 1
                continue
            updated += 1
        _apply(pm, row, org_id, default_pm_id)
        seen.append(pm_id)

    # A card detached at Stripe must disappear locally, or the UI keeps claiming a
    # card is on file. Only Stripe-backed rows are pruned; legacy hand-entered rows
    # (NULL reference) are deliberately left alone.
    stale = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.organization_id == org_id,
                  models.BillingPaymentMethod.stripe_payment_method_id.isnot(None))
          .all()
    )
    for row in stale:
        if row.stripe_payment_method_id not in seen:
            db.delete(row)
            removed += 1

    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as exc:
        if commit:
            db.rollback()
        _log.error("payment method sync: local persistence failed org=%s: %s", org_id, exc)
        return {"status": "stripe_unavailable", "organization_id": org_id,
                "error": f"local persistence failed: {exc}"}

    _log.info("payment method sync org=%s: created=%s updated=%s removed=%s skipped=%s",
              org_id, created, updated, removed, skipped)
    return {"status": "synced", "organization_id": org_id, "created": created,
            "updated": updated, "removed": removed, "skipped": skipped,
            "remote_count": len(remote), "default_payment_method": default_pm_id}


def countable_cards(db: Session, org_id: int,
                    exclude_id: Optional[int] = None) -> int:
    """How many Stripe-backed cards this org would still have.

    Legacy NULL-reference rows are not counted: they cannot be charged, so they must
    never satisfy the "keep at least one payment method" rule.
    """
    q = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.organization_id == org_id,
                  models.BillingPaymentMethod.stripe_payment_method_id.isnot(None))
    )
    if exclude_id is not None:
        q = q.filter(models.BillingPaymentMethod.id != exclude_id)
    return q.count()


def subscription_needs_a_card(db: Session, org_id: int) -> bool:
    """True when removing the last card would guarantee a failed renewal."""
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None or not sub.stripe_subscription_id:
        return False
    return (sub.subscription_status or "").strip().lower() in LIVE_SUBSCRIPTION_STATES


def sync_all_orgs(db: Session, *, stripe_client=None) -> Dict[str, int]:
    """Sweep every billable organization that has a Stripe customer.

    Internal tenants are excluded in the query, so a background job can never open a
    Stripe call for a non-billable organization.
    """
    tally = {"checked": 0, "synced": 0, "created": 0, "updated": 0,
             "removed": 0, "failed": 0}
    subs = (
        db.query(models.BillingSubscription)
          .join(models.Organization,
                models.Organization.id == models.BillingSubscription.organization_id)
          .filter(models.BillingSubscription.stripe_customer_id.isnot(None),
                  models.BillingSubscription.organization_id.isnot(None),
                  models.Organization.billing_type != billing_service.BILLING_TYPE_INTERNAL)
          .all()
    )
    for sub in subs:
        tally["checked"] += 1
        res = sync_org_payment_methods(db, sub.organization_id, stripe_client=stripe_client)
        if res.get("status") == "synced":
            tally["synced"] += 1
            for k in ("created", "updated", "removed"):
                tally[k] += res.get(k, 0)
        else:
            tally["failed"] += 1
    _log.info("payment method sync sweep: %s", tally)
    return tally


def run_once_sync() -> Dict[str, int]:                # pragma: no cover - runtime path
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return sync_all_orgs(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = 3600,
                      initial_delay_seconds: int = 0):   # pragma: no cover - runtime path
    """Safety net for cards added or removed directly in the Stripe portal.

    No new webhook event types are introduced (Phase 3 K3), so the confirmation
    endpoint covers in-app additions and this sweep covers everything else.
    """
    # Environment gate. The task is still created so the shutdown handler stays
    # valid, but it exits immediately outside production: these sweeps send real
    # mail and can modify Stripe, and "Payment method mirror" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Payment method mirror not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except Exception as exc:
            _log.error("payment method sync sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)
