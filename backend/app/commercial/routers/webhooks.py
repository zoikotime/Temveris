"""Billing provider webhooks (§11 — idempotent).

Endpoint: POST /api/v1/billing/webhooks/{provider}

We dedupe by (provider, provider_event_id) so re-deliveries are no-ops.
Failed processing is recorded with attempts; >=5 attempts → dead_letter.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database.database import get_db
from app.commercial.billing import checkout as billing_checkout
from app.commercial.billing.providers import get_provider
from app.commercial.audit import emit_event
from app.commercial import constants

_log = logging.getLogger("zoiko.commercial.routers.webhooks")
router = APIRouter(prefix="/api/v1/billing/webhooks", tags=["Commercial v1 Billing"])


@router.post("/{provider}")
async def receive(provider: str, request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("stripe-signature") or request.headers.get("x-webhook-signature")

    # Verify signature before parsing business meaning (§4.3). An unknown
    # provider path segment fails closed — never auto-verify a webhook we
    # can't authenticate.
    pv = get_provider()
    if pv.name == provider:
        verified = pv.verify_webhook(payload=body, signature=signature)
        reason = "signature_verification_failed"
    else:
        verified = False
        reason = "unknown_provider"
    if not verified:
        emit_event(db, event_type="webhook.processing_failed",
                   source_service="billing-webhooks",
                   payload={"reason": reason, "provider": provider},
                   error_code=constants.ERR_WEBHOOK_FAILED, commit=True)
        return {"received": False, "verified": False}

    import json as _json
    try:
        envelope = _json.loads(body or b"{}")
    except Exception:
        envelope = {}

    provider_event_id = envelope.get("id") or envelope.get("event_id") or "unknown"
    event_type = envelope.get("type") or envelope.get("event_type") or "unknown"

    receipt = billing_checkout.receive_webhook(
        db, provider=provider, provider_event_id=provider_event_id,
        event_type=event_type, payload=envelope,
    )
    db.commit()

    if receipt.status == "processed":
        return {"received": True, "duplicate": True}

    # Map the (small) set of provider events we currently handle.
    try:
        data = (envelope.get("data") or {}).get("object") or {}
        session_local_id = (data.get("metadata") or {}).get("checkout_session_id")
        if event_type in {"checkout.session.completed", "payment_intent.succeeded"}:
            if session_local_id:
                billing_checkout.mark_payment_succeeded(
                    db, checkout_session_id=session_local_id,
                    provider_payment_id=data.get("id"),
                )
        elif event_type in {"checkout.session.async_payment_failed", "payment_intent.payment_failed"}:
            if session_local_id:
                billing_checkout.mark_payment_failed(
                    db, checkout_session_id=session_local_id,
                    failure_code=(data.get("last_payment_error") or {}).get("code"),
                    failure_message=(data.get("last_payment_error") or {}).get("message"),
                )
        billing_checkout.mark_webhook_processed(db, receipt)
    except Exception as e:
        _log.exception("Webhook processing failed for %s/%s", provider, provider_event_id)
        billing_checkout.mark_webhook_failed(db, receipt, str(e))
        emit_event(db, event_type="webhook.processing_failed",
                   source_service="billing-webhooks",
                   payload={"error": str(e), "provider_event_id": provider_event_id},
                   error_code=constants.ERR_WEBHOOK_FAILED)
    db.commit()
    return {"received": True, "duplicate": False, "status": receipt.status}
