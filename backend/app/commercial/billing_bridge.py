"""Keeps entitlements in step with what the customer actually paid for.

Stripe writes to ``billing_subscriptions`` (app/billing). The entitlement
resolver reads ``commercial_organizations`` / ``commercial_subscriptions``
(app/commercial). Nothing joined the two, so a customer could complete a
Governed checkout and keep Verified entitlements until somebody edited the
commercial row by hand.

This module is that join. The dependency deliberately points *this* way —
commercial depends on billing, never the reverse — because live billing must
keep working unchanged if this layer is modified or removed. Billing exposes
``register_state_observer``; we register into it. Billing itself has no idea
this package exists (an invariant pinned by
``tests/test_billing_phase5_cleanup.py::test_billing_does_not_depend_on_the_commercial_layer``).

Registration happens on import, and ``app.commercial.bootstrap`` imports this
module, so the observer is live for the whole process.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.billing import webhooks as billing_webhooks
from app.commercial.subscription_ops import sync_from_billing

_log = logging.getLogger("zoiko.commercial.billing_bridge")


def on_billing_state_applied(db: Session, sub, *, plan_code=None,
                             source: str = "webhook") -> None:
    """Project a just-applied billing state onto the commercial rows.

    Runs inside the caller's transaction — for webhooks that is the same
    transaction as the idempotency claim, so the projection cannot be committed
    without the billing write, or vice versa.
    """
    sync_from_billing(
        db,
        operational_org_id=getattr(sub, "organization_id", None),
        billing_status=getattr(sub, "subscription_status", None),
        plan_code=plan_code,
        source=source,
    )


def register() -> None:
    """Idempotent — safe to call from repeated imports or test setup."""
    billing_webhooks.register_state_observer(on_billing_state_applied)


register()
