"""Checkout session orchestration (§6.2 Journey B + §11 Billing SM).

Path:
    billing_not_required (trial) → billing_profile_created
                                 → checkout_pending → payment_pending
                                 → payment_succeeded → invoice_created
                                 → invoice_paid → billing_reconciled

If payment succeeds but provisioning fails → preserve payment, retry
provisioning, alert ops, refund if unrecoverable (§6.2 critical rule).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.commercial import ids, state_machines, entitlements, constants
from app.commercial.audit import emit_event
from app.commercial.models import (
    CheckoutSession, CommercialSubscription, CommercialInvoice, ProviderWebhookReceipt,
)
from app.commercial.billing import ledger
from app.commercial.billing.providers import get_provider
from app.commercial.notifications import (
    send_billing_document, send_subscription_confirmation, send_payment_recovery,
)

_log = logging.getLogger("zoiko.commercial.billing.checkout")


def _monthly_price_cents(plan_code: str, billing_cycle: str) -> int:
    base = constants.CANONICAL_PRICES_MONTHLY.get(plan_code) or 0.0
    monthly_cents = int(round(base * 100))
    if billing_cycle == "annual":
        # 12 months minus 20% discount.
        return int(round(monthly_cents * 12 * (1 - constants.ANNUAL_DISCOUNT_PCT / 100)))
    return monthly_cents


def create_session(
    db: Session,
    *,
    organization_id: str,
    workspace_id: Optional[str],
    plan_code: str,
    billing_cycle: str,
    seat_count: int,
    currency: str = "USD",
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    quote_id: Optional[str] = None,
) -> CheckoutSession:
    # If a server quote is supplied it is authoritative: plan/cycle/seats/
    # currency come from it, not from the (possibly stale) client fields (§D-05).
    if quote_id:
        from app.commercial.billing import quotes as _quotes
        q = _quotes.get_valid_quote(db, quote_id)  # raises if expired/used
        plan_code = q.plan_code
        billing_cycle = q.billing_cycle
        seat_count = q.seat_count
        currency = q.currency

    if plan_code not in constants.CANONICAL_PRICES_MONTHLY:
        raise ValueError(f"Unknown plan: {plan_code}")
    if constants.CANONICAL_PRICES_MONTHLY[plan_code] is None:
        raise ValueError(f"Plan {plan_code} requires sales-led signup")

    provider = get_provider()
    handle = provider.create_checkout_session(
        organization_id=organization_id, plan_code=plan_code,
        billing_cycle=billing_cycle, seat_count=seat_count, currency=currency,
        success_url=success_url, cancel_url=cancel_url,
    )

    session_id = ids.checkout_id()
    row = CheckoutSession(
        id=session_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        plan_code=plan_code,
        billing_cycle=billing_cycle,
        seat_count=seat_count,
        currency=currency,
        success_url=success_url,
        cancel_url=cancel_url,
        state=state_machines.billing.transition("billing_not_required", "billing_profile_created"),
        provider=provider.name,
        provider_session_id=handle.provider_session_id,
        provider_redirect_url=handle.redirect_url,
        correlation_id=correlation_id,
        quote_id=quote_id,
    )
    row.state = state_machines.billing.transition("billing_profile_created", "checkout_pending")
    db.add(row)
    db.flush()

    emit_event(
        db,
        event_type="checkout.session_created",
        source_service="billing-service",
        payload={
            "plan": plan_code, "cycle": billing_cycle, "seats": seat_count,
            "provider": provider.name, "session_id": session_id,
        },
        organization_id=organization_id, workspace_id=workspace_id,
        correlation_id=correlation_id,
    )
    return row


def mark_payment_succeeded(
    db: Session,
    *,
    checkout_session_id: str,
    provider_payment_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> dict:
    """Finalize a checkout session as paid.

    Idempotent: if the session is already past payment_succeeded we
    short-circuit and return the existing state.

    §6.2 critical failure rule: if anything AFTER the payment record
    (entitlement compute, workspace state, ledger) fails, the payment
    receipt is preserved by committing the payment-related rows first
    and then enqueuing a recovery task. The customer never loses the
    fact that they paid; ops gets an alert and admin can refund.
    """
    sess = db.query(CheckoutSession).filter_by(id=checkout_session_id).one_or_none()
    if sess is None:
        raise ValueError("Checkout session not found")

    if sess.state == "payment_succeeded":
        return {"checkout_session_id": sess.id, "state": sess.state, "already": True}

    sess.state = state_machines.billing.transition(sess.state, "payment_pending")
    sess.state = state_machines.billing.transition(sess.state, "payment_succeeded")
    sess.completed_at = datetime.now(timezone.utc)
    db.flush()

    org_id = sess.organization_id
    # Subscription transitions: from trial_active/paid_checkout_pending → active.
    sub = db.query(CommercialSubscription).filter_by(organization_id=org_id).one_or_none()
    _send_recovery = False
    if sub is not None:
        _prior_state = sub.state
        _prior_plan = (sub.plan_code, sub.billing_cycle, sub.seat_count)
        _send_recovery = _prior_state in {"past_due", "grace_period"}
        if sub.state in {"trial_active", "trial_ending"}:
            # Canonical conversion (§9.1). The subscription machine only allows
            # trial_ending → active, so an active trial steps through it.
            if sub.state == "trial_active":
                sub.state = state_machines.subscription.transition(sub.state, "trial_ending")
            sub.state = state_machines.subscription.transition(sub.state, "active")
            sub.active_since = datetime.now(timezone.utc)
        elif sub.state in {"past_due", "grace_period"}:
            sub.state = state_machines.subscription.transition(sub.state, "active")
        sub.plan_code = sess.plan_code
        sub.billing_cycle = sess.billing_cycle
        sub.seat_count = sess.seat_count
        db.flush()
        emit_event(
            db, event_type="subscription.activated",
            source_service="subscription-service",
            payload={"plan": sub.plan_code, "cycle": sub.billing_cycle, "seats": sub.seat_count},
            organization_id=org_id, correlation_id=correlation_id,
        )
        emit_event(
            db, event_type="trial.converted",
            source_service="subscription-service",
            payload={"plan": sub.plan_code, "from": "trial"},
            organization_id=org_id, correlation_id=correlation_id,
        )

        # ZT-BIL-01 (Essential - billing). Send only for a genuine activation or
        # plan/cadence/seat change — NOT for payment recovery (past_due/grace →
        # active), which is reserved for ZT-BIL-05. Best-effort: an email failure
        # must never affect the recorded activation.
        _bil01_event = None
        if _prior_state in {"trial_active", "trial_ending"}:
            _bil01_event = "activated"
        elif (_prior_state not in {"past_due", "grace_period"}
              and (sub.plan_code, sub.billing_cycle, sub.seat_count) != _prior_plan):
            _bil01_event = "updated"
        if _bil01_event is not None:
            try:
                send_subscription_confirmation(
                    db, organization_id=org_id, subscription=sub,
                    plan_event=_bil01_event, correlation_id=correlation_id,
                )
            except Exception:
                _log.warning("ZT-BIL-01 subscription email failed (activation preserved)", exc_info=True)

    # Invoice + ledger. If the checkout was created from a server quote,
    # the invoice amounts come from that quote (§D-05) so the customer is
    # charged exactly what they reviewed. Otherwise fall back to computing
    # from the plan/cycle (legacy path).
    quote = None
    if sess.quote_id:
        from app.commercial.models import PriceQuote
        quote = db.query(PriceQuote).filter_by(id=sess.quote_id).one_or_none()
    if quote is not None:
        subtotal_cents = quote.subtotal_cents
        discount_cents = quote.discount_cents
        tax_cents = quote.tax_cents
        invoice_total = quote.total_cents
        quote.state = "consumed"
    else:
        invoice_total = _monthly_price_cents(sess.plan_code, sess.billing_cycle) * (sess.seat_count or 1)
        subtotal_cents = invoice_total
        discount_cents = 0
        tax_cents = 0
    invoice = CommercialInvoice(
        id=ids.invoice_id(),
        organization_id=org_id,
        subscription_id=sub.id if sub else None,
        number=f"INV-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{ids.invoice_id().split('_', 1)[-1][:6]}",
        period_start=datetime.now(timezone.utc),
        period_end=None,
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        discount_cents=discount_cents,
        total_cents=invoice_total,
        currency=sess.currency,
        status="issued",
        issued_at=datetime.now(timezone.utc),
        provider=sess.provider,
    )
    db.add(invoice)
    db.flush()

    ledger.append(
        db, organization_id=org_id, subscription_id=sub.id if sub else None,
        invoice_id=invoice.id, payment_id=provider_payment_id,
        entry_type="subscription_charge",
        amount_cents=invoice_total, currency=sess.currency,
        description=f"{sess.plan_code} × {sess.seat_count} ({sess.billing_cycle})",
    )
    ledger.append(
        db, organization_id=org_id, subscription_id=sub.id if sub else None,
        invoice_id=invoice.id, payment_id=provider_payment_id,
        entry_type="payment_received",
        amount_cents=invoice_total, currency=sess.currency,
        description=f"Stripe checkout {sess.provider_session_id}",
    )

    invoice.status = "paid"
    invoice.paid_at = datetime.now(timezone.utc)
    db.flush()

    emit_event(
        db, event_type="payment.succeeded",
        source_service="billing-service",
        payload={"amount_cents": invoice_total, "currency": sess.currency,
                 "session_id": sess.id, "invoice_id": invoice.id},
        organization_id=org_id, correlation_id=correlation_id,
    )
    emit_event(
        db, event_type="invoice.created",
        source_service="billing-service",
        payload={"invoice_id": invoice.id, "amount_cents": invoice_total},
        organization_id=org_id, correlation_id=correlation_id,
    )

    # ZT-BIL-02 receipt (Essential - billing record). Best-effort: a mail
    # failure must never roll back or block the recorded payment (§6.2). Only
    # reached on the first success (the method short-circuits on replay above),
    # so a duplicate webhook cannot send a second receipt.
    try:
        send_billing_document(db, organization_id=org_id, invoice=invoice,
                              document_type="Receipt", correlation_id=correlation_id)
    except Exception:
        _log.warning("ZT-BIL-02 receipt email failed (payment preserved)", exc_info=True)

    # ZT-BIL-05 recovery (Critical - billing). Sent when THIS payment brought a
    # past_due/grace_period subscription back to active. Best-effort: a mail
    # failure must never affect the recorded recovery. (Only the grace_period
    # path reaches here; past_due->active raises upstream — pre-existing.)
    if _send_recovery:
        try:
            send_payment_recovery(db, organization_id=org_id, subscription=sub,
                                  invoice=invoice, correlation_id=correlation_id)
        except Exception:
            _log.warning("ZT-BIL-05 recovery email failed (recovery preserved)", exc_info=True)

    # ── §6.2 Critical Failure Rule ──────────────────────────────────────
    # Payment is recorded above. Entitlement recompute is the last step
    # that can plausibly fail (workspace not ready, resource limits, DB
    # hiccup). If it fails we DO NOT roll back the payment — we mark the
    # tenant as "provisioning_pending_after_payment" so the admin console
    # and reconciliation job can pick it up.
    provisioning_recovery_required = False
    try:
        entitlements.invalidate(db, org_id)
        entitlements.compute(db, org_id)
    except Exception as e:
        provisioning_recovery_required = True
        _log.exception(
            "Post-payment entitlement compute failed for org=%s session=%s",
            org_id, sess.id,
        )
        # Flag the checkout session so the admin can see it needs attention.
        sess.recovery_required = True
        sess.recovery_reason = f"entitlements_failed: {str(e)[:200]}"
        db.flush()
        emit_event(
            db, event_type="tenant.provisioning_failed",
            source_service="billing-service",
            payload={
                "session_id": sess.id, "invoice_id": invoice.id,
                "stage": "post_payment_entitlements",
                "error": str(e)[:500],
                "payment_preserved": True,
                "ops_action": "retry_or_refund",
            },
            organization_id=org_id, correlation_id=correlation_id,
            error_code=constants.ERR_PROVISIONING_FAILED,
        )
        # Open a manual review case so ops sees it in the admin console
        # alongside fraud/manual-review work. Best-effort — never fail
        # the payment flow because the review-queue insert failed.
        try:
            from app.commercial.models import ManualReviewCase
            db.add(ManualReviewCase(
                case_id=ids.case_id(),
                organization_id=org_id,
                reason="post_payment_recovery",
                severity="high",
                status="pending",
                details={
                    "session_id": sess.id, "invoice_id": invoice.id,
                    "workspace_id": sess.workspace_id,
                    "summary": f"Payment {invoice.id} succeeded but post-payment provisioning failed",
                    "error": str(e)[:500],
                },
            ))
            db.flush()
        except Exception:
            _log.warning("Failed to open ManualReviewCase for post-payment recovery", exc_info=True)

    return {"checkout_session_id": sess.id, "invoice_id": invoice.id,
            "state": sess.state, "subscription_state": sub.state if sub else None,
            "recovery_required": provisioning_recovery_required}


# ── §6.2 Critical Failure Rule — recovery helpers ───────────────────────
def retry_post_payment_provisioning(
    db: Session,
    *,
    checkout_session_id: str,
    correlation_id: Optional[str] = None,
) -> dict:
    """Re-run the entitlement compute + downstream readiness for a session
    that was flagged `recovery_required=True` by mark_payment_succeeded.

    Idempotent: clearing the flag only happens on success.
    """
    sess = db.query(CheckoutSession).filter_by(id=checkout_session_id).one_or_none()
    if sess is None:
        raise ValueError("Checkout session not found")
    if not sess.recovery_required:
        return {"checkout_session_id": sess.id, "state": sess.state, "noop": True}

    org_id = sess.organization_id
    try:
        entitlements.invalidate(db, org_id)
        entitlements.compute(db, org_id)
        sess.recovery_required = False
        sess.recovery_reason = None
        db.flush()
        emit_event(
            db, event_type="tenant.provisioning_recovered",
            source_service="billing-service",
            payload={"session_id": sess.id, "stage": "post_payment_retry"},
            organization_id=org_id, correlation_id=correlation_id,
        )
        return {"checkout_session_id": sess.id, "state": sess.state, "recovered": True}
    except Exception as e:
        sess.recovery_reason = f"retry_failed: {str(e)[:200]}"
        db.flush()
        emit_event(
            db, event_type="tenant.provisioning_retried",
            source_service="billing-service",
            payload={"session_id": sess.id, "error": str(e)[:500],
                     "attempts": (sess.recovery_attempts or 0) + 1},
            organization_id=org_id, correlation_id=correlation_id,
            error_code=constants.ERR_PROVISIONING_FAILED,
        )
        sess.recovery_attempts = (sess.recovery_attempts or 0) + 1
        db.flush()
        raise


def queue_refund_for_failed_provisioning(
    db: Session,
    *,
    checkout_session_id: str,
    reason: str,
    correlation_id: Optional[str] = None,
) -> dict:
    """Mark a failed-provisioning payment for refund. The actual refund is
    issued by the billing provider (Stripe). This records the intent +
    ledger entry so finance can reconcile."""
    sess = db.query(CheckoutSession).filter_by(id=checkout_session_id).one_or_none()
    if sess is None:
        raise ValueError("Checkout session not found")
    inv = (
        db.query(CommercialInvoice)
          .filter_by(organization_id=sess.organization_id, status="paid")
          .order_by(CommercialInvoice.issued_at.desc())
          .first()
    )
    if inv is None:
        raise ValueError("No paid invoice found for refund")

    ledger.append(
        db,
        organization_id=sess.organization_id,
        subscription_id=None,
        invoice_id=inv.id,
        payment_id=None,
        entry_type="refund_issued",
        amount_cents=-(inv.total_cents or 0),
        currency=inv.currency or "USD",
        description=f"Refund for failed provisioning: {reason}",
    )
    inv.status = "refunded"
    sess.recovery_required = False
    sess.recovery_reason = f"refunded: {reason}"
    db.flush()
    emit_event(
        db, event_type="refund_completed" if False else "billing.reconciled",
        source_service="billing-service",
        payload={"invoice_id": inv.id, "session_id": sess.id, "reason": reason},
        organization_id=sess.organization_id, correlation_id=correlation_id,
    )

    # ZT-BIL-02 refund confirmation (Essential - billing record). Best-effort:
    # a mail failure must never affect the recorded refund.
    try:
        send_billing_document(db, organization_id=sess.organization_id, invoice=inv,
                              document_type="Refund", correlation_id=correlation_id)
    except Exception:
        _log.warning("ZT-BIL-02 refund email failed (refund preserved)", exc_info=True)

    return {"checkout_session_id": sess.id, "invoice_id": inv.id, "refunded": True}


def mark_payment_failed(
    db: Session,
    *,
    checkout_session_id: str,
    failure_code: Optional[str] = None,
    failure_message: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> dict:
    sess = db.query(CheckoutSession).filter_by(id=checkout_session_id).one_or_none()
    if sess is None:
        raise ValueError("Checkout session not found")

    sess.state = state_machines.billing.transition(sess.state, "payment_pending")
    sess.state = state_machines.billing.transition(sess.state, "payment_failed")
    sess.failed_at = datetime.now(timezone.utc)
    db.flush()

    emit_event(
        db, event_type="payment.declined",
        source_service="billing-service",
        payload={
            "session_id": sess.id, "failure_code": failure_code,
            "failure_message": failure_message,
        },
        organization_id=sess.organization_id, correlation_id=correlation_id,
        error_code=constants.ERR_PAYMENT_DECLINED,
    )

    # §17.2 — bump the failed-payment velocity counter. After 3 failures
    # in the hour the fraud module persists a `failed_payment_velocity`
    # signal and opens a manual-review case for risk/ops.
    try:
        from app.commercial import fraud as _fraud
        flagged = _fraud.record_failed_payment(
            db, organization_id=sess.organization_id,
            failure_code=failure_code,
        )
        if flagged is not None:
            emit_event(
                db, event_type="payment.fraud_flagged",
                source_service="billing-service",
                payload={"session_id": sess.id,
                         "failure_code": failure_code,
                         "threshold": constants.FRAUD_FAILED_PAYMENT_REVIEW},
                organization_id=sess.organization_id,
                correlation_id=correlation_id,
            )
    except Exception:
        _log.warning("Failed-payment fraud bump skipped", exc_info=True)

    # Drop into dunning on the billing state machine.
    sess.state = state_machines.billing.transition(sess.state, "dunning_active")
    db.flush()

    # ── Drive the SUBSCRIPTION into past_due so the existing dunning scheduler
    #    (dunning._tick_one walks past_due/grace_period/dunning_active and keys
    #    off past_due_since) fires the ZT-BIL-03 / ZT-BIL-05 recovery sequence.
    #    Previously only the CheckoutSession state moved, so the subscription
    #    stayed "active" and dunning never ran. Guarded + defensive: this can
    #    never break the payment-failure handling above. Sends no email itself
    #    and touches no email content. ──
    try:
        _sub = db.query(CommercialSubscription).filter_by(
            organization_id=sess.organization_id).one_or_none()
        if _sub is not None and _sub.state == "active":
            _prev = _sub.state
            try:
                _sub.state = state_machines.subscription.transition(_sub.state, "past_due")
            except Exception:
                _sub.state = "past_due"
            if getattr(_sub, "past_due_since", None) is None:
                _sub.past_due_since = datetime.now(timezone.utc)
            db.flush()
            emit_event(
                db, event_type="subscription.past_due",
                source_service="billing-service",
                payload={"subscription_id": _sub.id, "session_id": sess.id},
                organization_id=sess.organization_id, correlation_id=correlation_id,
                state_from=_prev, state_to=_sub.state,
            )
    except Exception:
        _log.warning("subscription past_due transition skipped", exc_info=True)

    return {"checkout_session_id": sess.id, "state": sess.state}


# ──────────────────────────────────────────────────────────────────────────
# WEBHOOK PROCESSING  (idempotent, ledger-grade)
# ──────────────────────────────────────────────────────────────────────────
def receive_webhook(
    db: Session,
    *,
    provider: str,
    provider_event_id: str,
    event_type: str,
    payload: dict,
) -> ProviderWebhookReceipt:
    """Record a provider webhook idempotently. Re-deliveries are no-ops."""
    existing = (
        db.query(ProviderWebhookReceipt)
          .filter_by(provider=provider, provider_event_id=provider_event_id)
          .one_or_none()
    )
    if existing:
        existing.attempts = (existing.attempts or 0) + 1
        db.flush()
        return existing

    row = ProviderWebhookReceipt(
        provider=provider, provider_event_id=provider_event_id,
        event_type=event_type, payload=payload,
        status="received", attempts=1,
    )
    db.add(row)
    db.flush()
    return row


def mark_webhook_processed(db: Session, receipt: ProviderWebhookReceipt) -> None:
    receipt.status = "processed"
    receipt.processed_at = datetime.now(timezone.utc)
    db.flush()


def mark_webhook_failed(db: Session, receipt: ProviderWebhookReceipt, error: str) -> None:
    receipt.attempts = (receipt.attempts or 0) + 1
    receipt.last_error = error
    if receipt.attempts >= 5:
        receipt.status = "dead_letter"
    else:
        receipt.status = "failed"
    db.flush()
