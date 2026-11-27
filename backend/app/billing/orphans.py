"""Detect Stripe subscriptions that Temveris has no record of.

WHY THIS EXISTS
---------------
``reconcile`` already reports duplicate subscriptions, but only ones it can reach: it
starts from ``BillingSubscription.stripe_customer_id`` and lists that customer's
subscriptions. A subscription whose *customer* is also unknown locally is invisible to
it — and that is exactly the shape the real incidents took. Four active subscriptions
sat billing in the sandbox for 11–16 days, each under its own Stripe customer that
appeared in no local row:

    sub_1U2t2Y…  $100.00  cus_V2z0AT…      sub_1U1JRx…  $615.00  cus_V1MAeI…
    sub_1U2rl8…  $120.00  cus_V2xgqv…      sub_1U1580…  $615.00  cus_V17MZT…

They arise because a first purchase omits ``customer=`` from the Checkout Session, so
Stripe mints a new Customer per session. Two paid sessions therefore produce two
customers and two subscriptions; the webhook records the first and refuses the second
("fill in, never replace"), which protects local state but leaves the second billing
the card. The customer cannot cancel it — the hosted portal is opened for the *known*
customer id, so the orphan is unreachable from the product.

This module is the missing instrument: it enumerates Stripe and asks which
subscriptions we cannot account for.

DELIBERATELY READ-ONLY
----------------------
Nothing here creates, modifies, cancels or deletes any Stripe object, writes to the
database, or sends mail. The only Stripe calls are ``Subscription.list`` and
``Price.retrieve`` — both GETs.

Cancelling automatically would be the wrong behaviour even though it is tempting: an
unmatched subscription is not proof of a mistake. It could be a migrated customer, a
subscription created directly in the Dashboard, or a local row that was deleted. Acting
on that guess would cut off a paying customer. Every finding is a human decision.

FAILURE STANCE
--------------
A Stripe outage must never read as "no orphans". Any listing error returns
``status="unavailable"`` with the message and ``orphans=None`` — deliberately not an
empty list, so a caller cannot mistake absence of data for absence of orphans. A single
malformed subscription is recorded and skipped rather than aborting the scan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.billing import service as billing_service
from app.billing import webhooks as billing_webhooks
from database import models

_log = logging.getLogger("zoiko.billing.orphans")

_g = billing_webhooks._g

# Stripe's maximum page size. Pagination is by ``starting_after``, not by page number.
PAGE_SIZE = 100

# A hard stop so a runaway cursor cannot loop forever against a paginated API.
# 200 pages x 100 = 20,000 subscriptions, far beyond any plausible tenant count.
MAX_PAGES = 200

# What "in scope" means. Only these Stripe statuses can still take money from a card,
# which is what makes an unmatched one dangerous. A canceled subscription bills nobody,
# so reporting it would be noise that trains an operator to ignore the report.
BILLABLE_STATUSES = ("active", "past_due", "unpaid", "trialing", "incomplete")

# The single status this scan requests from Stripe by default. Kept narrow on purpose:
# an *active* unmatched subscription is the one charging a card right now.
DEFAULT_STATUS = "active"

# Classification of each inspected subscription.
MATCHED = "matched"            # a local row owns this subscription
INTERNAL = "internal"          # belongs to an internal tenant — never a customer orphan
ORPHAN = "orphan"             # billing, and nothing local accounts for it
UNPARSED = "unparsed"          # Stripe returned something we could not read


def _amount_of(sub: Any) -> Dict[str, Any]:
    """Price, quantity and money for a subscription, read from the embedded items.

    Uses the item data Stripe already returns with the subscription rather than a
    second ``Price.retrieve`` per row — one call per subscription would turn a scan of
    a few hundred into a rate-limit problem for no extra information.
    """
    items = _g(_g(sub, "items"), "data") or []
    if not items:
        return {"price_id": None, "quantity": None, "unit_amount": None,
                "currency": None, "interval": None, "amount": None}
    first = items[0]
    price = _g(first, "price") or {}
    recurring = _g(price, "recurring") or {}
    unit = _g(price, "unit_amount")
    qty = _g(first, "quantity")
    amount = None
    if unit is not None:
        amount = round((unit / 100.0) * (qty or 1), 2)
    return {
        "price_id": _g(price, "id"),
        "quantity": qty,
        "unit_amount": None if unit is None else round(unit / 100.0, 2),
        "currency": (_g(price, "currency") or "").upper() or None,
        "interval": _g(recurring, "interval"),
        "amount": amount,
    }


def _local_index(db: Session) -> Dict[str, Dict[str, Any]]:
    """Map every locally-known Stripe subscription id to its organization.

    ``stripe_subscription_id IS NOT NULL`` is filtered in SQL rather than in Python.
    Without it a row with a NULL id would key the index under ``None`` and then match
    any subscription whose id we failed to read — a false "matched" that would hide a
    real orphan.
    """
    rows = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.stripe_subscription_id.isnot(None))
          .all()
    )
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        index[row.stripe_subscription_id] = {
            "organization_id": row.organization_id,
            "local_subscription_id": row.id,
            "local_status": row.subscription_status,
        }
    return index


def _internal_customer_ids(db: Session) -> Set[str]:
    """Stripe customer ids belonging to internal tenants.

    An orphan has no local subscription row, so its organization cannot be resolved
    from the subscription id. Its *customer* id can still be recognised, which is how
    an internal tenant's leftover subscription is kept out of the customer-orphan list.

    ``is_internal_tenant`` is the existing definition (app/billing/service.py) and is
    reused deliberately — a second copy of "which tenants are internal" is precisely
    the kind of duplicate source of truth that causes drift.
    """
    ids: Set[str] = set()
    rows = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.stripe_customer_id.isnot(None))
          .all()
    )
    for row in rows:
        if billing_service.is_internal_tenant(db, row.organization_id):
            ids.add(row.stripe_customer_id)
    return ids


def _iter_stripe_subscriptions(stripe_client, *, status: str,
                               page_size: int = PAGE_SIZE):
    """Yield every subscription Stripe reports for ``status``, across all pages.

    Stripe paginates with ``starting_after`` on the last id of the previous page —
    there is no page number. Taking only the first page would have found 2 of the 4
    known orphans and reported the rest as clean.

    Raises on a listing failure so the caller can report "unavailable"; it must never
    look like an empty result.
    """
    starting_after = None
    pages = 0
    while True:
        kwargs: Dict[str, Any] = {"status": status, "limit": page_size,
                                  "expand": ["data.items.data.price"]}
        if starting_after:
            kwargs["starting_after"] = starting_after
        page = stripe_client.Subscription.list(**kwargs)
        data = _g(page, "data") or []
        for item in data:
            yield item
        pages += 1
        if not _g(page, "has_more") or not data:
            return
        if pages >= MAX_PAGES:
            _log.error("orphan scan stopped at the %s-page ceiling — results are "
                       "incomplete", MAX_PAGES)
            return
        last_id = _g(data[-1], "id")
        if not last_id:
            # Without an id there is no cursor to continue from. Stopping is the only
            # safe option; silently restarting from the top would loop forever.
            _log.error("orphan scan cannot paginate: last subscription has no id")
            return
        starting_after = last_id


def scan(db: Session, *, stripe_client=None, status: str = DEFAULT_STATUS,
         page_size: int = PAGE_SIZE) -> Dict[str, Any]:
    """Inspect every Stripe subscription of ``status`` and classify it. Read-only.

    Returns a report dict. Never raises: a Stripe failure comes back as
    ``status="unavailable"`` with ``orphans=None``.
    """
    if stripe_client is None:                        # pragma: no cover - runtime path
        import stripe as stripe_client               # noqa: PLC0415

    local = _local_index(db)
    internal_customers = _internal_customer_ids(db)

    inspected = 0
    rows: List[Dict[str, Any]] = []
    counts = {MATCHED: 0, INTERNAL: 0, ORPHAN: 0, UNPARSED: 0}

    try:
        for remote in _iter_stripe_subscriptions(stripe_client, status=status,
                                                 page_size=page_size):
            inspected += 1
            # One unreadable subscription must not abort the scan — the point of the
            # report is the other 99.
            try:
                sub_id = _g(remote, "id")
                if not sub_id:
                    raise ValueError("subscription has no id")
                customer_id = _g(remote, "customer")
                # customer may be expanded into an object rather than an id string.
                if customer_id is not None and not isinstance(customer_id, str):
                    customer_id = _g(customer_id, "id")
                created = _g(remote, "created")
                money = _amount_of(remote)
                match = local.get(sub_id)

                if match is not None:
                    kind = (INTERNAL
                            if billing_service.is_internal_tenant(
                                db, match["organization_id"])
                            else MATCHED)
                elif customer_id and customer_id in internal_customers:
                    kind = INTERNAL
                else:
                    kind = ORPHAN

                rows.append({
                    "stripe_subscription_id": sub_id,
                    "stripe_customer_id": customer_id,
                    "stripe_status": _g(remote, "status"),
                    "created": (datetime.fromtimestamp(created, timezone.utc).isoformat()
                                if isinstance(created, (int, float)) else None),
                    "organization_id": (match or {}).get("organization_id"),
                    "local_subscription_id": (match or {}).get("local_subscription_id"),
                    "local_status": (match or {}).get("local_status"),
                    "classification": kind,
                    "orphan": kind == ORPHAN,
                    **money,
                })
                counts[kind] += 1
            except Exception as exc:                  # noqa: BLE001
                _log.error("orphan scan could not read a subscription: %s", exc)
                rows.append({"stripe_subscription_id": _g(remote, "id"),
                             "classification": UNPARSED, "orphan": False,
                             "error": str(exc)[:200]})
                counts[UNPARSED] += 1
    except Exception as exc:                          # noqa: BLE001
        # A listing failure is NOT zero orphans. orphans=None so no caller can read
        # this as a clean result.
        _log.error("orphan scan unavailable: %s", exc)
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "requested_status": status,
            "inspected": inspected,
            "orphans": None,
            "orphan_count": None,
            "counts": counts,
            "subscriptions": rows,
        }

    orphans = [r for r in rows if r.get("orphan")]
    report = {
        "status": "ok",
        "requested_status": status,
        "inspected": inspected,
        "matched": counts[MATCHED],
        "internal": counts[INTERNAL],
        "unparsed": counts[UNPARSED],
        "orphan_count": len(orphans),
        "orphans": orphans,
        "counts": counts,
        "subscriptions": rows,
    }
    if orphans:
        _log.error(
            "ORPHAN STRIPE SUBSCRIPTIONS: %s billing with no local record — %s",
            len(orphans), [o["stripe_subscription_id"] for o in orphans],
        )
    else:
        _log.info("orphan scan clean: %s inspected, 0 unmatched", inspected)
    return report


def scan_billable(db: Session, *, stripe_client=None,
                  page_size: int = PAGE_SIZE) -> Dict[str, Any]:
    """Scan every status that can still charge a card, not just ``active``.

    ``past_due`` and ``unpaid`` matter as much as ``active``: an unmatched subscription
    in those states is still attached to a card and will retry. Results are merged and
    de-duplicated on subscription id, because Stripe can report the same subscription
    under more than one requested status across calls.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    inspected = 0
    unavailable: List[str] = []
    for status in BILLABLE_STATUSES:
        part = scan(db, stripe_client=stripe_client, status=status, page_size=page_size)
        if part["status"] == "unavailable":
            unavailable.append(f"{status}: {part['error']}")
            continue
        inspected += part["inspected"]
        for row in part["subscriptions"]:
            sid = row.get("stripe_subscription_id")
            if sid:
                merged[sid] = row

    if unavailable and not merged:
        return {"status": "unavailable", "error": "; ".join(unavailable)[:300],
                "inspected": inspected, "orphans": None, "orphan_count": None,
                "subscriptions": []}

    rows = list(merged.values())
    orphans = [r for r in rows if r.get("orphan")]
    return {
        "status": "partial" if unavailable else "ok",
        "errors": unavailable or None,
        "inspected": inspected,
        "unique_subscriptions": len(rows),
        "matched": sum(1 for r in rows if r.get("classification") == MATCHED),
        "internal": sum(1 for r in rows if r.get("classification") == INTERNAL),
        "unparsed": sum(1 for r in rows if r.get("classification") == UNPARSED),
        "orphan_count": len(orphans),
        "orphans": orphans,
        "subscriptions": rows,
    }


def run_once_sync() -> Dict[str, Any]:               # pragma: no cover - runtime path
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return scan(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = 86400,
                      initial_delay_seconds: int = 0):  # pragma: no cover - runtime path
    """Daily read-only scan. NOT registered in main.py by this change.

    Provided so the scan can be scheduled deliberately rather than by editing this
    module later. It only reads and logs; it cannot cancel anything.
    """
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except Exception as exc:                      # noqa: BLE001
            _log.error("orphan scan failed: %s", exc)
        await asyncio.sleep(interval_seconds)
