"""Payment provider interface + Stripe-shaped stub.

The real Stripe integration calls Stripe's checkout/customers API. To
keep the code testable and runnable without a Stripe key, we ship a
``StubProvider`` that records the same call shape locally and lets the
billing flow exercise the full state machine. Switch by setting
BILLING_PROVIDER=stripe in env (and STRIPE_SECRET_KEY).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

from app.commercial import ids

_log = logging.getLogger("zoiko.commercial.billing.providers")


def _allow_unverified_webhooks() -> bool:
    """Opt-in escape hatch for local dev / e2e where there is no signing
    secret. Default OFF so an unauthenticated webhook is rejected in any
    environment that hasn't explicitly enabled it (Build Contracts §7.3)."""
    return (os.getenv("ZT_ALLOW_UNVERIFIED_WEBHOOKS") or "").strip().lower() in {"1", "true", "yes"}


@dataclass
class CheckoutHandle:
    provider_session_id: str
    redirect_url: str
    raw: Dict[str, Any]


@dataclass
class CustomerHandle:
    provider_customer_id: str
    raw: Dict[str, Any]


@dataclass
class PaymentResult:
    succeeded: bool
    provider_payment_id: Optional[str]
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


class BillingProvider:
    name = "base"

    def create_customer(self, *, organization_id: str, email: str,
                        company_name: str, country: Optional[str]) -> CustomerHandle:
        raise NotImplementedError

    def create_checkout_session(self, *, organization_id: str, plan_code: str,
                                billing_cycle: str, seat_count: int,
                                currency: str, success_url: Optional[str],
                                cancel_url: Optional[str]) -> CheckoutHandle:
        raise NotImplementedError

    def verify_webhook(self, *, payload: bytes, signature: Optional[str]) -> bool:
        # Fail closed. A provider that cannot authenticate a webhook must not
        # accept it — otherwise a misconfigured deploy silently trusts forged
        # events. Concrete providers override with real verification.
        return False


class StubProvider(BillingProvider):
    """In-process billing provider — no external calls.

    All "provider IDs" are minted locally. ``create_checkout_session``
    returns a redirect_url that points back to a local success URL so
    the end-to-end flow can be exercised without a real PSP.
    """
    name = "stub"

    def verify_webhook(self, *, payload, signature):
        # The stub has no signing secret, so it can never authenticate a
        # webhook. Accept only when explicitly opted in (local dev / e2e);
        # otherwise reject, so a stub left active in production is safe.
        if _allow_unverified_webhooks():
            return True
        _log.warning(
            "stub provider rejected webhook — set ZT_ALLOW_UNVERIFIED_WEBHOOKS=1 "
            "for local testing, or configure a real provider"
        )
        return False

    def create_customer(self, *, organization_id, email, company_name, country):
        return CustomerHandle(
            provider_customer_id=f"cus_stub_{ids.org_id().split('_', 1)[-1][:10]}",
            raw={"email": email, "name": company_name, "country": country},
        )

    def create_checkout_session(self, *, organization_id, plan_code, billing_cycle,
                                seat_count, currency, success_url, cancel_url):
        session_id = f"cs_stub_{ids.checkout_id().split('_', 1)[-1][:14]}"
        redirect = success_url or "/billing/success"
        # Stub immediately "succeeds" — the test path can finalize by
        # calling the webhook endpoint with this session_id.
        return CheckoutHandle(
            provider_session_id=session_id,
            redirect_url=redirect,
            raw={
                "plan": plan_code, "cycle": billing_cycle, "seats": seat_count,
                "currency": currency, "success_url": success_url, "cancel_url": cancel_url,
            },
        )


class StripeProvider(BillingProvider):
    """Real Stripe integration. Requires STRIPE_SECRET_KEY at runtime.

    The Stripe SDK is imported lazily so the rest of the system can run
    without it installed.
    """
    name = "stripe"

    def __init__(self):
        try:
            import stripe  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "BILLING_PROVIDER=stripe but the `stripe` package is not installed. "
                "Add it to requirements.txt or fall back to BILLING_PROVIDER=stub."
            ) from e
        self.stripe = stripe
        self.stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        if not self.stripe.api_key:
            raise RuntimeError("STRIPE_SECRET_KEY not set")
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    def create_customer(self, *, organization_id, email, company_name, country):
        c = self.stripe.Customer.create(
            email=email, name=company_name,
            metadata={"organization_id": organization_id, "country": country or ""},
        )
        return CustomerHandle(provider_customer_id=c.id, raw=c.to_dict_recursive())

    def create_checkout_session(self, *, organization_id, plan_code, billing_cycle,
                                seat_count, currency, success_url, cancel_url):
        # In a real integration we'd look up the Stripe price id for the
        # plan+cycle combo from a price_map table. Placeholder here.
        price_id = os.getenv(f"STRIPE_PRICE_{plan_code.upper()}_{billing_cycle.upper()}", "")
        if not price_id:
            raise RuntimeError(
                f"No Stripe price configured for plan={plan_code} cycle={billing_cycle}"
            )
        s = self.stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": seat_count}],
            currency=currency.lower(),
            success_url=success_url, cancel_url=cancel_url,
            metadata={"organization_id": organization_id, "plan": plan_code,
                      "billing_cycle": billing_cycle, "seats": str(seat_count)},
        )
        return CheckoutHandle(
            provider_session_id=s.id,
            redirect_url=s.url,
            raw=s.to_dict_recursive(),
        )

    def verify_webhook(self, *, payload, signature):
        if not self.webhook_secret:
            if _allow_unverified_webhooks():
                _log.warning("STRIPE_WEBHOOK_SECRET not set — accepting unverified (opt-in)")
                return True
            _log.error("STRIPE_WEBHOOK_SECRET not set — rejecting webhook (fail closed)")
            return False
        try:
            self.stripe.Webhook.construct_event(
                payload, signature or "", self.webhook_secret,
            )
            return True
        except Exception as e:
            _log.warning("Stripe webhook verification failed: %s", e)
            return False


def get_provider() -> BillingProvider:
    name = (os.getenv("BILLING_PROVIDER") or "stub").lower()
    if name == "stripe":
        try:
            return StripeProvider()
        except Exception as e:
            _log.error("Falling back to StubProvider — Stripe init failed: %s", e)
    return StubProvider()
