"""Mirror Stripe invoices into ``billing_invoices``.

Before this, Temveris invented its own invoices. ``seed_demo_invoices_if_empty``
fabricated three rows per subscription so the table wouldn't look empty, priced from
the local plan and stamped with a hardcoded 8% "tax". Every production invoice row
still has ``stripe_invoice_id IS NULL`` and claims $9.72, while the real Stripe
invoice for that same subscription is $615. Customers were being shown numbers that
matched nothing Stripe had charged.

Stripe is the only authority on what was invoiced, so invoices are now *mirrored*
rather than generated:

* Reads stay local. ``GET /invoices`` still queries ``billing_invoices``, so the page
  is fast and keeps working when Stripe is unreachable.
* Rows are keyed by ``stripe_invoice_id``, so a re-sync updates in place and can never
  produce a duplicate.
* Legacy rows (``stripe_invoice_id IS NULL``) are never touched, updated or deleted.
  They are historical records of what the customer was shown; correcting them is a
  finance decision, not a sync side effect.

**Tax:** none. Business decision 2026-08-12 — plan prices are final, tax-inclusive
customer prices. Stripe Tax is not enabled (``automatic_tax.enabled = false``) and no
manual rates are applied, so ``tax_amount`` is mirrored as Stripe reports it, which is
0. Nothing here computes tax.

Tenant safety: an invoice is only mirrored onto an organization whose own
``stripe_customer_id`` matches the invoice's customer. Internal tenants are skipped —
they have no Stripe relationship at all.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.invoices")

# Stripe invoice status -> the local vocabulary the UI already renders.
# Stripe: draft | open | paid | uncollectible | void
_STATUS_MAP = {
    "draft":         "draft",
    "open":          "issued",
    "paid":          "paid",
    "void":          "void",
    "uncollectible": "failed",
}


def _ts(value: Optional[int]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _money(cents: Optional[int]) -> float:
    return round((cents or 0) / 100.0, 2)


def _get(obj: Any, key: str, default=None):
    """StripeObject shadows .get(), so read defensively by key presence."""
    try:
        if key in obj:
            return obj[key]
    except Exception:
        pass
    return default


def _subscription_id(inv: Any) -> Optional[str]:
    """The subscription reference moved under ``parent`` in recent API versions.

    Checked in both places so a version change does not silently unlink invoices
    from their subscription.
    """
    top = _get(inv, "subscription")
    if isinstance(top, str) and top:
        return top
    parent = _get(inv, "parent") or {}
    details = _get(parent, "subscription_details") or {}
    sub = _get(details, "subscription")
    return sub if isinstance(sub, str) and sub else None


def _apply(inv: Any, row: models.BillingInvoice, org_id: int,
           sub: models.BillingSubscription) -> None:
    """Copy Stripe's numbers onto a local row. Stripe is authoritative for all of them."""
    transitions = _get(inv, "status_transitions") or {}
    stripe_status = _get(inv, "status") or ""

    row.organization_id      = org_id
    row.subscription_id      = sub.id
    row.stripe_invoice_id    = _get(inv, "id")
    # Stripe's own invoice number when present; the id is a stable fallback so the
    # NOT NULL column is always satisfiable.
    row.invoice_number       = _get(inv, "number") or _get(inv, "id")
    row.billing_period_start = _ts(_get(inv, "period_start"))
    row.billing_period_end   = _ts(_get(inv, "period_end"))
    row.subtotal             = _money(_get(inv, "subtotal"))
    # Mirrored, not computed. Prices are tax-inclusive by business decision, so this
    # is 0 unless Stripe itself ever reports otherwise.
    row.tax_amount           = _money(_get(inv, "tax"))
    row.discount_amount      = _money(sum(
        (_get(d, "amount") or 0) for d in (_get(inv, "total_discount_amounts") or [])
    ))
    row.total_amount         = _money(_get(inv, "total"))
    row.currency             = (_get(inv, "currency") or "usd").upper()
    row.status               = _STATUS_MAP.get(stripe_status, stripe_status or "draft")
    row.due_date             = _ts(_get(inv, "due_date"))
    row.paid_date            = _ts(_get(transitions, "paid_at"))
    # Stripe-hosted PDF — replaces the locally generated text stub.
    row.pdf_url              = _get(inv, "invoice_pdf")


def sync_org_invoices(db: Session, org_id: Optional[int], *, stripe_client=None,
                      limit: int = 100, commit: bool = True) -> Dict[str, Any]:
    """Mirror this organization's Stripe invoices locally.

    Returns a status dict; never raises, so a webhook or background job cannot fail
    because of invoice mirroring. Statuses:
      no_organization | no_subscription | no_stripe_customer | internal_tenant
      | stripe_unavailable | synced

    ``commit=False`` flushes without committing, for callers that own the transaction
    boundary. The webhook path needs this: Phase 3 processes an event as claim →
    handle → mark processed → *one* commit, and committing here would both break that
    single-commit guarantee and make the subscription-state write depend on invoice
    mirroring succeeding.
    """
    try:
        org_id = billing_service.require_org_id(org_id)
    except billing_service.MissingOrganization:
        return {"status": "no_organization"}

    # An internal tenant has no Stripe relationship; nothing to mirror.
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
        listed = stripe_client.Invoice.list(customer=sub.stripe_customer_id, limit=limit)
        remote = list(listed["data"])
    except Exception as exc:
        _log.error("invoice sync: Stripe list failed org=%s customer=%s: %s",
                   org_id, sub.stripe_customer_id, exc)
        return {"status": "stripe_unavailable", "organization_id": org_id,
                "error": str(exc)}

    created = updated = skipped = 0
    for inv in remote:
        stripe_id = _get(inv, "id")
        if not stripe_id:
            skipped += 1
            continue

        # Tenant ownership: an invoice is only ever attached to the organization whose
        # own customer id it belongs to. Stripe returned it for this customer, but the
        # check is explicit so a future caller cannot pass a mismatched pair.
        if _get(inv, "customer") != sub.stripe_customer_id:
            _log.error("invoice sync: refusing invoice %s — customer %s != org %s's %s",
                       stripe_id, _get(inv, "customer"), org_id, sub.stripe_customer_id)
            skipped += 1
            continue

        row = (
            db.query(models.BillingInvoice)
              .filter(models.BillingInvoice.stripe_invoice_id == stripe_id)
              .first()
        )
        if row is None:
            row = models.BillingInvoice()
            db.add(row)
            created += 1
        else:
            # Never let a re-sync move an invoice between tenants. Left untouched
            # rather than reassigned — a mismatch means something is wrong upstream
            # and silently moving billing records would hide it.
            if row.organization_id is not None and row.organization_id != org_id:
                _log.error("invoice sync: %s already belongs to org %s, not %s",
                           stripe_id, row.organization_id, org_id)
                skipped += 1
                continue
            updated += 1
        _apply(inv, row, org_id, sub)

    try:
        if commit:
            db.commit()
        else:
            # Caller owns the transaction (see the webhook path). Flush so the rows are
            # visible to the rest of this transaction without ending it.
            db.flush()
    except Exception as exc:
        if commit:
            db.rollback()
        _log.error("invoice sync: local persistence failed org=%s: %s", org_id, exc)
        return {"status": "stripe_unavailable", "organization_id": org_id,
                "error": f"local persistence failed: {exc}"}

    _log.info("invoice sync org=%s: created=%s updated=%s skipped=%s",
              org_id, created, updated, skipped)
    return {"status": "synced", "organization_id": org_id, "created": created,
            "updated": updated, "skipped": skipped, "remote_count": len(remote)}


def sync_all_orgs(db: Session, *, stripe_client=None) -> Dict[str, int]:
    """Sweep every billable organization that has a Stripe customer.

    Internal tenants are excluded in the query, so a background job can never open a
    Stripe call for a non-billable organization.
    """
    tally = {"checked": 0, "synced": 0, "created": 0, "updated": 0, "failed": 0}
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
        res = sync_org_invoices(db, sub.organization_id, stripe_client=stripe_client)
        if res.get("status") == "synced":
            tally["synced"] += 1
            tally["created"] += res.get("created", 0)
            tally["updated"] += res.get("updated", 0)
        else:
            tally["failed"] += 1
    _log.info("invoice sync sweep: %s", tally)
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
    """Periodic safety net for invoices that arrived while a webhook was missed."""
    # Environment gate. The task is still created so the shutdown handler stays
    # valid, but it exits immediately outside production: these sweeps send real
    # mail and can modify Stripe, and "Invoice mirror" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Invoice mirror not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except Exception as exc:
            _log.error("invoice sync sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)
