"""Stripe webhook processing: idempotent claim → authoritative re-fetch → apply.

The previous handler read the event payload and wrote it straight to the local
row. That produced four classes of defect, all fixed here:

* **No idempotency.** ``event.id`` was never stored, so every Stripe re-delivery
  re-ran the business logic. ``checkout.session.completed`` recomputed
  ``renewal_date`` as ``now() + 30 days`` each time, so duplicates silently
  extended paid service. Now every event is claimed in ``billing_webhook_events``
  and the *database* UNIQUE constraint on ``stripe_event_id`` decides the winner —
  a Python check-then-insert cannot be safe under concurrent delivery.

* **Order dependence.** Stripe does not guarantee delivery order, and the handler
  applied whatever arrived last. An event created *earlier* could overwrite newer
  state. Fixed by treating an event as a *trigger*, never as data: we re-fetch the
  subscription from Stripe and mirror its current state (K2). A stale event
  therefore cannot move local state backwards, because the state we write never
  comes from the event.

* **No ownership verification.** IDs in the event were trusted on sight. Every
  write now verifies the Stripe customer matches the local row, the same rule
  ``seats.sync_org_seats`` already enforced.

* **Silent failure.** No attempt counter, no error record, no dead letter. Now
  tracked per event, with a hard ceiling of ``MAX_ATTEMPTS``.

The claim row and the business update share **one transaction**, so an event can
never be marked handled unless its effects also landed. That coupling is also what
makes concurrent delivery safe — the INSERT holds a lock on the UNIQUE index entry
until the first delivery finishes, so the second cannot mistake work-in-progress
for work-abandoned. A failed attempt is recorded from a separate session, since the
rollback that undid the work also discarded the claim.

Billing-owned: no dependency on ``app/commercial``. ``organizations.id`` (INTEGER)
is the only tenant identity used.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.webhooks")

# ── Ledger statuses (K5) ─────────────────────────────────────────────────────
RECEIVED = "received"
PROCESSED = "processed"
FAILED = "failed"
DEAD_LETTER = "dead_letter"

MAX_ATTEMPTS = 5

# ── Events we handle (K3). Deliberately excludes invoice.payment_succeeded and
# invoice_payment.paid: Stripe emits those alongside invoice.paid for the *same*
# payment, so handling them would process one payment up to three times. ───────
HANDLED_EVENTS = frozenset({
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
})

# Stripe subscription status -> local subscription_status.
STRIPE_STATUS_MAP = {
    "active":             "active",
    "trialing":           "trial",
    "past_due":           "past_due",
    "unpaid":             "past_due",
    "canceled":           "canceled",
    "incomplete":         "incomplete",
    "incomplete_expired": "incomplete_expired",
    "paused":             "past_due",
}

# A Stripe subscription never leaves these states — it is replaced by a new
# subscription instead. So a write moving *out* of one, for the same subscription
# id, means we resolved the wrong row or read a stale object: refuse it. This is
# what stops a late invoice.payment_failed resurrecting a cancelled tenant.
TERMINAL_STATES = frozenset({"canceled", "incomplete_expired"})

# Checkout is only proof of payment when Stripe says the money moved.
PAID_STATUSES = frozenset({"paid", "no_payment_required"})

# Fields copied into the stored payload. A whitelist, not a denylist, so no
# card/PII field can be captured by an unanticipated Stripe schema change.
_PAYLOAD_WHITELIST = (
    "id", "object", "status", "customer", "subscription", "client_reference_id",
    "payment_status", "mode", "cancel_at_period_end", "cancel_at", "canceled_at",
    "current_period_end", "currency", "billing_reason", "attempt_count",
)


class WebhookProcessingError(Exception):
    """Business processing failed; the event should be retried by Stripe."""


class WebhookRejected(Exception):
    """The event is not safe to apply (ownership/tenancy/precedence violation).

    Terminal: retrying cannot help, so it is recorded and not retried.
    """


class WebhookNotReady(Exception):
    """The event is valid but arrived before the state it depends on exists.

    RETRYABLE, and the distinction matters: Stripe does not guarantee delivery order,
    so ``invoice.paid`` regularly lands before ``checkout.session.completed`` has linked
    the subscription id to the local row. Raising ``WebhookRejected`` for that was
    wrong — the class is documented as "retrying cannot help", so the event was
    dead-lettered on attempt 1 and Stripe was told (HTTP 200) not to re-deliver it.

    The two real payments this system has processed both hit it: a 100% dead-letter
    rate on invoice.paid, silently skipping apply_subscription_state, _sync_dunning and
    _mirror_invoices. The hourly sweeps papered over the invoice mirror, but dunning
    clearing on payment recovery depends on this path — a customer paying to escape
    suspension could stay suspended for up to an hour.

    This propagates to the generic handler, which records a non-terminal failure and
    returns HTTP 500, so Stripe retries with backoff. ``MAX_ATTEMPTS`` still caps it, so
    an event whose subscription never appears dead-letters rather than retrying forever.
    """


# ══════════════════════════════════════════════════════════════════════════════
# Stripe object access helpers
# ══════════════════════════════════════════════════════════════════════════════
def _g(obj: Any, key: str, default=None):
    """Read a field from a StripeObject, a dict, or a test double.

    StripeObject supports subscription access but shadows ``.get``; plain dicts
    and namespace doubles need different handling. Try all three.
    """
    if obj is None:
        return default
    try:
        val = obj[key]
        return default if val is None else val
    except (KeyError, TypeError, IndexError):
        pass
    return getattr(obj, key, default)


def _ts(value) -> Optional[datetime]:
    """Unix seconds -> aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise to UTC-aware before comparing two stored timestamps.

    ``DateTime(timezone=True)`` keeps tzinfo on PostgreSQL but drops it on SQLite,
    so a value read back can be naive while the Stripe-derived one is aware.
    Comparing those directly reports a difference on every pass, which would make
    reconciliation rewrite ``renewal_date`` forever and never converge.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _same_instant(a: Optional[datetime], b: Optional[datetime]) -> bool:
    return _as_utc(a) == _as_utc(b)


def _redact(data: Any) -> Dict[str, Any]:
    """Whitelisted, non-sensitive projection of the event object for diagnostics."""
    out: Dict[str, Any] = {}
    for key in _PAYLOAD_WHITELIST:
        val = _g(data, key)
        if val is None:
            continue
        if isinstance(val, (str, int, float, bool)):
            out[key] = val
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Claim — the atomic, database-enforced idempotency gate
# ══════════════════════════════════════════════════════════════════════════════
def _load(db: Session, event_id: str) -> Optional[models.BillingWebhookEvent]:
    return (
        db.query(models.BillingWebhookEvent)
          .filter(models.BillingWebhookEvent.stripe_event_id == event_id)
          .first()
    )


def is_retryable(row: models.BillingWebhookEvent) -> bool:
    """Whether a previously-recorded event may be processed again.

    Only a *failed* attempt is retryable. ``processed`` is done, and
    ``dead_letter`` has exhausted its budget (or was rejected for a reason that
    re-delivery cannot fix, like an unpaid session).
    """
    if row.status in (PROCESSED, DEAD_LETTER):
        return False
    return (row.attempts or 0) < MAX_ATTEMPTS


def claim_event(db: Session, event: Any) -> Tuple[Optional[models.BillingWebhookEvent], bool]:
    """Attempt to claim ``event``. Returns ``(row, claimed)``.

    **Does not commit.** The caller commits the claim together with the business
    update, so the two can never diverge: either both land or neither does.

    That coupling is what makes concurrent duplicate delivery safe. The INSERT
    takes a lock on the UNIQUE index entry and holds it for the whole
    transaction, so a second delivery of the same event blocks here until the
    first finishes:

    * first transaction commits  → second gets IntegrityError, sees the row is
      already ``processed``, and does nothing;
    * first transaction rolls back → second's INSERT succeeds and it processes,
      which is correct because the first did no work.

    Committing the claim *before* the business update — the obvious design —
    breaks exactly this: the second delivery would see a committed ``received``
    row, be unable to tell "in flight right now" from "crashed half way", and
    process the event a second time.
    """
    event_id = _g(event, "id")
    if not event_id:
        # No id means no idempotency key. Refuse rather than process blind.
        raise WebhookRejected("event has no id")

    row = models.BillingWebhookEvent(
        stripe_event_id   = event_id,
        event_type        = _g(event, "type") or "unknown",
        api_version       = _g(event, "api_version"),
        stripe_created_at = _ts(_g(event, "created")),
        status            = RECEIVED,
        attempts          = 0,
        payload           = _redact(_g(_g(event, "data"), "object")),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        db.rollback()
        _log.info("webhook duplicate (lost claim race): event=%s", event_id)
        return _load(db, event_id), False
    return row, True


def record_failure(event: Any, error: str, *, org_id: Optional[int] = None,
                   terminal: bool = False) -> Optional[models.BillingWebhookEvent]:
    """Persist a failed attempt in its own session.

    The business transaction has been rolled back by the time this runs — which
    also discarded the uncommitted claim row — so the failure record cannot use
    that session. Opening a separate one is the same approach
    ``seats._record_failure`` takes, and for the same reason: an audit record of a
    failure must survive the rollback of the work that failed.
    """
    from database.database import SessionLocal          # noqa: PLC0415

    event_id = _g(event, "id")
    db = SessionLocal()
    try:
        row = _load(db, event_id)
        if row is None:
            row = models.BillingWebhookEvent(
                stripe_event_id   = event_id,
                event_type        = _g(event, "type") or "unknown",
                api_version       = _g(event, "api_version"),
                stripe_created_at = _ts(_g(event, "created")),
                attempts          = 0,
                payload           = _redact(_g(_g(event, "data"), "object")),
            )
            db.add(row)
        row.attempts = (row.attempts or 0) + 1
        row.last_error = (error or "")[:2000]
        row.processed_at = None
        if org_id is not None and row.organization_id is None:
            row.organization_id = org_id
        row.status = DEAD_LETTER if (terminal or row.attempts >= MAX_ATTEMPTS) else FAILED
        db.commit()
        _log.error("webhook failed: event=%s type=%s org=%s attempts=%s status=%s error=%s",
                   row.stripe_event_id, row.event_type, row.organization_id,
                   row.attempts, row.status, row.last_error)
        return row
    except Exception as exc:                            # pragma: no cover - defensive
        db.rollback()
        _log.error("could not record webhook failure for event=%s: %s", event_id, exc)
        return None
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# Authoritative Stripe state -> local mirror
# ══════════════════════════════════════════════════════════════════════════════
def current_period_end(remote: Any) -> Optional[datetime]:
    """Stripe's authoritative period end.

    Never ``now() + 30/365 days``: a computed date drifts forward on every
    delivery and silently grants free service. Recent Stripe API versions moved
    this onto the subscription item, so both locations are checked.
    """
    direct = _ts(_g(remote, "current_period_end"))
    if direct:
        return direct
    items = _g(_g(remote, "items"), "data") or []
    if items:
        return _ts(_g(items[0], "current_period_end"))
    return None


def _first_item(remote: Any) -> Optional[Any]:
    items = _g(_g(remote, "items"), "data") or []
    return items[0] if items else None


def stripe_price_id(remote: Any) -> Optional[str]:
    item = _first_item(remote)
    price = _g(item, "price") if item else None
    return _g(price, "id") if price is not None else None


def billing_cycle_from_stripe(remote: Any) -> Optional[str]:
    """Derive the cycle from the price's recurring interval — authoritative.

    Session metadata is caller-supplied and can be stale or absent; the price on
    the live subscription cannot.
    """
    item = _first_item(remote)
    price = _g(item, "price") if item else None
    interval = _g(_g(price, "recurring"), "interval") if price is not None else None
    if interval == "year":
        return "annual"
    if interval == "month":
        return "monthly"
    return None


def derive_local_status(remote: Any) -> Optional[str]:
    """Local subscription_status for an authoritative Stripe subscription.

    ``cancel_at_period_end`` is folded in here (K6): Stripe keeps such a
    subscription ``active``, but locally it must read "Cancel Pending" so the
    tenant is not told service continues indefinitely. This reuses the existing
    ``cancel_pending`` status and ``cancellation_requested_at`` column — no second
    cancellation system, and Stripe stays the source of truth.
    """
    stripe_status = _g(remote, "status")
    local = STRIPE_STATUS_MAP.get(stripe_status)
    if local is None:
        return None
    if local in ("active", "trial") and bool(_g(remote, "cancel_at_period_end")):
        return "cancel_pending"
    return local


def is_backward_transition(current: Optional[str], new: Optional[str]) -> bool:
    """True if this write would leave a terminal state — never legitimate.

    Stripe never un-cancels a subscription (a new one is created instead), so for
    a given subscription id ``canceled``/``incomplete_expired`` are absorbing.
    A write out of one means the wrong local row was resolved or the Stripe read
    was stale, and applying it would resurrect a dead tenant.
    """
    return current in TERMINAL_STATES and new not in TERMINAL_STATES


def apply_subscription_state(db: Session, sub: models.BillingSubscription,
                             remote: Any, *, event_type: str) -> Dict[str, Any]:
    """Mirror an authoritative Stripe subscription onto the local row.

    Verifies customer ownership first, then refuses backward transitions. Returns
    a dict of what changed for the caller's log.
    """
    # ── Ownership (requirement 8): same rule as seats.sync_org_seats ──────────
    remote_customer = _g(remote, "customer")
    if remote_customer and sub.stripe_customer_id and remote_customer != sub.stripe_customer_id:
        raise WebhookRejected(
            f"customer_mismatch: local={sub.stripe_customer_id} stripe={remote_customer}"
        )

    new_status = derive_local_status(remote)
    if new_status is None:
        raise WebhookRejected(f"unmapped_stripe_status:{_g(remote, 'status')}")

    if is_backward_transition(sub.subscription_status, new_status):
        raise WebhookRejected(
            f"backward_transition: {sub.subscription_status} -> {new_status}"
        )

    changed: Dict[str, Any] = {}

    if sub.subscription_status != new_status:
        changed["subscription_status"] = (sub.subscription_status, new_status)
        sub.subscription_status = new_status

    period_end = current_period_end(remote)
    if period_end and not _same_instant(sub.renewal_date, period_end):
        changed["renewal_date"] = (sub.renewal_date, period_end)
        sub.renewal_date = period_end

    cycle = billing_cycle_from_stripe(remote)
    if cycle and sub.billing_cycle != cycle:
        changed["billing_cycle"] = (sub.billing_cycle, cycle)
        sub.billing_cycle = cycle

    # cancel_at_period_end -> cancellation_requested_at (K6). Cleared when the
    # customer reverses the cancellation, so the column always reflects Stripe.
    if bool(_g(remote, "cancel_at_period_end")):
        requested = _ts(_g(remote, "canceled_at")) or datetime.now(timezone.utc)
        if sub.cancellation_requested_at is None:
            changed["cancellation_requested_at"] = (None, requested)
            sub.cancellation_requested_at = requested
    elif sub.cancellation_requested_at is not None:
        changed["cancellation_requested_at"] = (sub.cancellation_requested_at, None)
        sub.cancellation_requested_at = None

    plan_code = _plan_code_for_price(db, stripe_price_id(remote))
    if plan_code:
        plan = (db.query(models.BillingPlan)
                  .filter(models.BillingPlan.code == plan_code).first())
        if plan and sub.plan_id != plan.id:
            changed["plan_id"] = (sub.plan_id, plan.id)
            sub.plan_id = plan.id

    _log.info("webhook applying %s to org=%s subscription=%s: %s",
              event_type, sub.organization_id, sub.stripe_subscription_id,
              {k: f"{v[0]}->{v[1]}" for k, v in changed.items()} or "no change")

    # Project onto the commercial row the entitlement resolver actually reads.
    # Inside this function so every caller is covered by construction: the four
    # subscription-affecting handlers below and reconcile.py's repair sweep.
    # Shares this transaction, so it cannot be applied without the billing write
    # (or vice versa).
    notify_state_applied(db, sub, plan_code=plan_code, source=event_type)
    return changed


def _price_catalog() -> Dict[str, Tuple[str, str]]:
    """Reverse map Stripe price id -> (plan_code, billing_cycle) from env.

    Price ids live only in ``STRIPE_PRICE_{CODE}_{CYCLE}`` env vars; there is no
    local column storing them. An unmapped price is reported, never guessed.
    """
    import os
    catalog: Dict[str, Tuple[str, str]] = {}
    for key, value in os.environ.items():
        if not key.startswith("STRIPE_PRICE_") or not value:
            continue
        suffix = key[len("STRIPE_PRICE_"):]
        for cycle_key, cycle in (("_MONTHLY", "monthly"), ("_ANNUAL", "annual")):
            if suffix.endswith(cycle_key):
                catalog[value] = (suffix[: -len(cycle_key)].lower(), cycle)
                break
    return catalog


def _plan_code_for_price(db: Session, price_id: Optional[str]) -> Optional[str]:
    if not price_id:
        return None
    entry = _price_catalog().get(price_id)
    return entry[0] if entry else None


# ══════════════════════════════════════════════════════════════════════════════
# State observers
# ══════════════════════════════════════════════════════════════════════════════
# Other subsystems need to react when Stripe moves a subscription — notably the
# entitlement layer, which reads its own tables and would otherwise keep serving
# the pre-payment plan. Billing must not import those subsystems: the dependency
# rule (pinned by tests/test_billing_phase5_cleanup.py::
# test_billing_does_not_depend_on_the_commercial_layer) is that live billing
# stays independent of them and keeps working if they are changed or removed.
#
# So the dependency is inverted: consumers register a callback here, and billing
# only ever calls what happens to be registered. With no observers the behaviour
# is byte-for-byte what it was before this hook existed.
StateObserver = Callable[..., None]
_STATE_OBSERVERS: List[StateObserver] = []


def register_state_observer(fn: StateObserver) -> StateObserver:
    """Register a callback invoked after a subscription's state is applied.

    Called as ``fn(db, sub, plan_code=..., source=...)`` inside the caller's
    transaction. Registration is idempotent so repeated imports (tests, reloads)
    cannot double-fire an observer.
    """
    if fn not in _STATE_OBSERVERS:
        _STATE_OBSERVERS.append(fn)
    return fn


def notify_state_applied(db: Session, sub, *, plan_code: Optional[str] = None,
                         source: str = "webhook") -> None:
    """Fan a just-applied billing state out to registered observers.

    Deliberately does not swallow exceptions. This runs inside the webhook's
    transaction, so a raising observer rolls back the claim as well and Stripe
    redelivers the event. Converging on the retry is correct; committing a
    payment while a consumer silently stayed behind is the failure mode worth
    avoiding.
    """
    for fn in _STATE_OBSERVERS:
        fn(db, sub, plan_code=plan_code, source=source)


# ══════════════════════════════════════════════════════════════════════════════
# Event handlers — each returns the organization_id it touched
# ══════════════════════════════════════════════════════════════════════════════
def _retrieve_subscription(stripe_client, subscription_id: str) -> Any:
    try:
        return stripe_client.Subscription.retrieve(subscription_id)
    except Exception as exc:                       # network/API — retryable
        raise WebhookProcessingError(
            f"stripe_retrieve_failed:{subscription_id}:{exc}") from exc


def _sync_dunning(db: Session, sub: models.BillingSubscription, event_type: str) -> None:
    """Start or stop the dunning clock to match the subscription's Stripe-derived state.

    Runs after apply_subscription_state, so onset and recovery both follow Stripe rather
    than the mere arrival of an event — a late payment_failed cannot re-open dunning on
    a subscription Stripe now reports as paid.

    On recovery the suspension cache is invalidated so the customer regains write access
    immediately rather than after the cache TTL.
    """
    try:
        from app.billing import dunning as billing_dunning        # noqa: PLC0415
        outcome = billing_dunning.sync_from_status(db, sub)
        if outcome != "unchanged":
            from app.billing import suspension as billing_suspension   # noqa: PLC0415
            billing_suspension.invalidate_cache(sub.organization_id)
            _log.info("dunning %s after %s for org=%s", outcome, event_type,
                      sub.organization_id)
    except Exception as exc:
        _log.error("dunning sync after %s failed for org=%s: %s",
                   event_type, sub.organization_id, exc)


def _mirror_invoices(db: Session, org_id: Optional[int], stripe_client,
                     event_type: str) -> None:
    """Refresh this tenant's invoice mirror after a payment event.

    Best-effort by design: invoices are a *display* mirror, so failing to refresh
    them must never fail the webhook and cause Stripe to retry a subscription-state
    update that already succeeded. The hourly sweep is the safety net.
    """
    if org_id is None:
        return
    try:
        from app.billing import invoices as billing_invoices   # noqa: PLC0415
        # commit=False: process_event owns the transaction and commits once. Committing
        # here would end the transaction mid-handler and make the subscription-state
        # write depend on invoice mirroring succeeding.
        result = billing_invoices.sync_org_invoices(
            db, org_id, stripe_client=stripe_client, commit=False)
        _log.info("invoice mirror after %s: org=%s %s", event_type, org_id, result.get("status"))
    except Exception as exc:
        _log.error("invoice mirror after %s failed for org=%s: %s", event_type, org_id, exc)


def _invoice_subscription_id(data: Any) -> Optional[str]:
    """Resolve the subscription an invoice belongs to.

    Stripe moved this reference. On older API versions it is ``invoice.subscription``;
    from 2026-07-29.dahlia it lives at
    ``invoice.parent.subscription_details.subscription`` and the top-level key is
    absent entirely.

    Reading only the top-level key meant both invoice handlers resolved None on every
    real event from the current API version and returned early — so ``invoice.paid``
    never recovered a past_due/incomplete subscription and ``invoice.payment_failed``
    never marked one. Both locations are checked so a version change cannot silently
    disable payment-state recovery again.
    """
    top = _g(data, "subscription")
    if isinstance(top, str) and top:
        return top
    parent = _g(data, "parent")
    details = _g(parent, "subscription_details")
    nested = _g(details, "subscription")
    return nested if isinstance(nested, str) and nested else None


def _sub_by_stripe_id(db: Session, subscription_id: str) -> models.BillingSubscription:
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.stripe_subscription_id == subscription_id)
          .first()
    )
    if sub is None:
        # Not a rejection: the link is very likely moments away. See WebhookNotReady.
        raise WebhookNotReady(f"no_local_subscription_for:{subscription_id}")
    return sub


def handle_checkout_session_completed(db: Session, data: Any, stripe_client) -> Optional[int]:
    """First purchase. The only handler that may assign Stripe ids to an org."""
    payment_status = _g(data, "payment_status")
    if payment_status not in PAID_STATUSES:
        # Never grant billing from an unpaid session (requirement 6). Terminal:
        # this session will not become paid, so retrying cannot help.
        raise WebhookRejected(f"unpaid_checkout_session:payment_status={payment_status}")

    raw_org = _g(data, "client_reference_id")
    try:
        org_id = int(raw_org) if raw_org is not None else None
    except (TypeError, ValueError):
        org_id = None
    if not org_id:
        raise WebhookRejected(f"unresolvable_tenant:client_reference_id={raw_org!r}")

    # An internal tenant is never billed, so it must never be granted Stripe ids —
    # not even by a correctly-signed event. Terminal: no retry will make an internal
    # tenant billable, so this is a rejection rather than a failure.
    if billing_service.is_internal_tenant(db, org_id):
        raise WebhookRejected(f"internal_tenant_not_billable:org={org_id}")

    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        raise WebhookRejected(f"no_subscription_for_org:{org_id}")

    stripe_sub_id = _g(data, "subscription")
    stripe_customer_id = _g(data, "customer")

    # ── Cross-tenant assignment guard (requirement 12) ────────────────────────
    # An id appearing in a signed event is not proof it belongs to this tenant.
    # Without this, an org whose ids are still NULL would absorb another org's
    # subscription, and every later lookup by stripe_subscription_id would then
    # resolve to an arbitrary one of the two rows.
    for field, value in (("stripe_subscription_id", stripe_sub_id),
                         ("stripe_customer_id", stripe_customer_id)):
        if not value:
            continue
        owner = (
            db.query(models.BillingSubscription)
              .filter(getattr(models.BillingSubscription, field) == value,
                      models.BillingSubscription.organization_id != org_id)
              .first()
        )
        if owner is not None:
            raise WebhookRejected(
                f"cross_tenant_{field}: {value} already owned by org "
                f"{owner.organization_id}, event claims org {org_id}"
            )

    # Fill in, never replace (Phase 2.1 guarantee: one subscription per org).
    if stripe_sub_id and not sub.stripe_subscription_id:
        sub.stripe_subscription_id = stripe_sub_id
    elif stripe_sub_id and stripe_sub_id != sub.stripe_subscription_id:
        raise WebhookRejected(
            f"would_replace_subscription_id: stored={sub.stripe_subscription_id} "
            f"incoming={stripe_sub_id}"
        )
    if stripe_customer_id and not sub.stripe_customer_id:
        sub.stripe_customer_id = stripe_customer_id

    if _g(data, "mode") != "subscription":
        # A setup-mode session saves a payment method; it grants no plan.
        return org_id

    if not sub.stripe_subscription_id:
        raise WebhookRejected("checkout_session_without_subscription")

    remote = _retrieve_subscription(stripe_client, sub.stripe_subscription_id)
    apply_subscription_state(db, sub, remote, event_type="checkout.session.completed")
    return org_id


def handle_subscription_updated(db: Session, data: Any, stripe_client) -> Optional[int]:
    sub = _sub_by_stripe_id(db, _g(data, "id"))
    remote = _retrieve_subscription(stripe_client, sub.stripe_subscription_id)
    apply_subscription_state(db, sub, remote, event_type="customer.subscription.updated")
    return sub.organization_id


def handle_subscription_deleted(db: Session, data: Any, stripe_client) -> Optional[int]:
    """Deletion is terminal and monotonic (requirement 11).

    A re-fetch is still attempted so ``cancel_at_period_end`` and the final period
    end are recorded from Stripe. If the object can no longer be read, the
    deletion itself is trusted — it cannot move state backwards, so unlike other
    events there is no risk in applying it without a successful re-fetch.
    """
    sub = _sub_by_stripe_id(db, _g(data, "id"))
    try:
        remote = stripe_client.Subscription.retrieve(sub.stripe_subscription_id)
    except Exception as exc:
        _log.warning("subscription.deleted: re-fetch failed for %s (%s); "
                     "applying terminal cancellation from the event",
                     sub.stripe_subscription_id, exc)
        remote = None

    if remote is not None:
        remote_customer = _g(remote, "customer")
        if remote_customer and sub.stripe_customer_id and remote_customer != sub.stripe_customer_id:
            raise WebhookRejected(
                f"customer_mismatch: local={sub.stripe_customer_id} stripe={remote_customer}")
        period_end = current_period_end(remote)
        if period_end:
            sub.renewal_date = period_end
        if bool(_g(remote, "cancel_at_period_end")) and sub.cancellation_requested_at is None:
            sub.cancellation_requested_at = (
                _ts(_g(remote, "canceled_at")) or datetime.now(timezone.utc))

    sub.subscription_status = "canceled"
    _log.info("subscription cancelled: org=%s subscription=%s",
              sub.organization_id, sub.stripe_subscription_id)
    # This handler writes the status directly rather than going through
    # apply_subscription_state, so it needs its own projection.
    notify_state_applied(db, sub, source="customer.subscription.deleted")
    return sub.organization_id


def handle_invoice_paid(db: Session, data: Any, stripe_client) -> Optional[int]:
    """Payment succeeded — recover whatever non-terminal state Stripe now reports.

    The old handler only cleared ``past_due``, so the Phase 2.3 ``incomplete``
    statuses could never be cleared by payment. Deriving from the re-fetched
    subscription recovers any state Stripe considers resolved, without a
    hand-maintained list of which states payment may clear.
    """
    subscription_id = _invoice_subscription_id(data)
    if not subscription_id:
        # One-off invoice with no subscription — nothing to mirror.
        return None
    sub = _sub_by_stripe_id(db, subscription_id)
    remote = _retrieve_subscription(stripe_client, sub.stripe_subscription_id)
    apply_subscription_state(db, sub, remote, event_type="invoice.paid")
    _sync_dunning(db, sub, "invoice.paid")
    _mirror_invoices(db, sub.organization_id, stripe_client, "invoice.paid")
    return sub.organization_id


def handle_invoice_payment_failed(db: Session, data: Any, stripe_client) -> Optional[int]:
    """Payment failed. Status comes from Stripe, not from the event's existence.

    Writing ``past_due`` unconditionally is what let a late failure event
    resurrect an already-cancelled tenant; ``apply_subscription_state`` refuses
    that transition, and the re-fetch means an already-recovered subscription is
    not dragged back to past_due by a stale event.
    """
    subscription_id = _invoice_subscription_id(data)
    if not subscription_id:
        return None
    sub = _sub_by_stripe_id(db, subscription_id)
    remote = _retrieve_subscription(stripe_client, sub.stripe_subscription_id)
    apply_subscription_state(db, sub, remote, event_type="invoice.payment_failed")
    _sync_dunning(db, sub, "invoice.payment_failed")
    _mirror_invoices(db, sub.organization_id, stripe_client, "invoice.payment_failed")
    return sub.organization_id


_HANDLERS = {
    "checkout.session.completed":     handle_checkout_session_completed,
    "customer.subscription.updated":  handle_subscription_updated,
    "customer.subscription.deleted":  handle_subscription_deleted,
    "invoice.paid":                   handle_invoice_paid,
    "invoice.payment_failed":         handle_invoice_payment_failed,
}


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════
def process_event(db: Session, event: Any, *, stripe_client=None) -> Dict[str, Any]:
    """Claim, process, and record one Stripe event. Never raises.

    The claim row and the business update share a single transaction, so an event
    is never recorded as handled unless its effects also landed — and a crash
    part-way leaves no claim at all, letting Stripe's retry process it cleanly
    rather than stranding it in a half-claimed state.
    """
    if stripe_client is None:                     # pragma: no cover - runtime path
        import stripe as stripe_client            # noqa: PLC0415

    event_id = _g(event, "id")
    if not event_id:
        _log.error("webhook has no event id; refusing to process")
        return {"status": "rejected", "reason": "event has no id"}
    event_type = _g(event, "type") or "unknown"

    # ── Prior outcome? A processed or dead-lettered event is never re-run. ────
    prior = _load(db, event_id)
    if prior is not None and not is_retryable(prior):
        _log.info("webhook replay ignored: event=%s ledger_status=%s attempts=%s",
                  event_id, prior.status, prior.attempts)
        return {"status": "duplicate", "event_id": event_id,
                "ledger_status": prior.status,
                "organization_id": prior.organization_id}

    if prior is None:
        row, claimed = claim_event(db, event)
        if not claimed:
            # A concurrent delivery holds the claim; it will do the work.
            return {"status": "duplicate", "event_id": event_id,
                    "ledger_status": getattr(row, "status", None)}
    else:
        row = prior
        _log.info("webhook retry: event=%s attempts=%s", event_id, prior.attempts)

    data = _g(_g(event, "data"), "object")
    org_id = None
    try:
        if event_type in HANDLED_EVENTS:
            org_id = _HANDLERS[event_type](db, data, stripe_client)
        else:
            # Claimed and acknowledged so Stripe stops retrying, but no business
            # logic exists for it (K3 — deliberately unhandled).
            _log.info("webhook type not handled, acknowledged only: event=%s type=%s",
                      event_id, event_type)

        row.status = PROCESSED
        row.processed_at = datetime.now(timezone.utc)
        row.last_error = None
        if org_id is not None:
            row.organization_id = org_id
        db.commit()
    except WebhookRejected as exc:
        # Terminal: re-delivery cannot make this valid, so do not ask Stripe to
        # retry it. Recorded as dead_letter for an operator to look at.
        db.rollback()
        record_failure(event, str(exc), org_id=org_id, terminal=True)
        return {"status": "rejected", "reason": str(exc), "event_id": event_id}
    except WebhookNotReady as exc:
        # Retryable: the event is valid, the state it needs just is not here yet.
        # Handled explicitly rather than falling through to the generic clause below,
        # because on a money path the difference between "retry this" and "give up on
        # this" should be visible at the call site, not inferred from class hierarchy.
        # record_failure without terminal=True leaves the row FAILED (retryable until
        # MAX_ATTEMPTS), and returning "failed" makes the router answer 500 so Stripe
        # re-delivers.
        db.rollback()
        recorded = record_failure(event, str(exc), org_id=org_id)
        _log.warning("webhook not ready, asking Stripe to retry: event=%s type=%s %s "
                     "(attempt %s/%s)", event_id, event_type, exc,
                     getattr(recorded, "attempts", "?"), MAX_ATTEMPTS)
        return {"status": "failed", "reason": str(exc), "event_id": event_id,
                "retryable": True,
                "attempts": getattr(recorded, "attempts", None)}
    except Exception as exc:
        db.rollback()
        recorded = record_failure(event, f"{type(exc).__name__}: {exc}", org_id=org_id)
        return {"status": "failed", "reason": str(exc), "event_id": event_id,
                "attempts": getattr(recorded, "attempts", None)}

    _log.info("webhook processed: event=%s type=%s org=%s attempts=%s",
              event_id, event_type, row.organization_id, row.attempts)
    return {"status": "processed" if event_type in HANDLED_EVENTS else "ignored",
            "event_id": event_id, "organization_id": org_id}
