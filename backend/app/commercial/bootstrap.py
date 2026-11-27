"""Commercial layer bootstrap (migrations + router wiring).

Called from main.py lifespan startup. Idempotent: safe to run on every
boot.
"""

from __future__ import annotations

import logging
from sqlalchemy import text

from database.database import engine, SessionLocal
from app.commercial import models as _commercial_models  # noqa: F401 — registers tables
# Registers the billing -> commercial state observer on import, so entitlements
# follow Stripe without app/billing having to know this package exists.
from app.commercial import billing_bridge as _billing_bridge  # noqa: F401

_log = logging.getLogger("zoiko.commercial.bootstrap")


def _session():
    """Open a session using the CURRENT session factory.

    ``from database.database import SessionLocal`` binds at import time, and the
    test harness rebinds ``database.database.SessionLocal`` to an in-memory
    engine afterwards. A module-level reference therefore keeps pointing at
    whatever the application was configured with when this module first
    imported — which in a test run is the developer's real database. Resolving
    the attribute per call makes these helpers honour the rebinding, so they can
    be tested against the harness rather than silently operating elsewhere.
    """
    from database import database as _database  # noqa: PLC0415
    return _database.SessionLocal()


def ensure_internal_identity() -> None:
    """Give Temveris's own tenant a commercial identity so it resolves.

    The entitlement resolver is keyed on a CommercialOrganization. The vendor
    tenant never had one — it is not a customer — so ``resolve_commercial_org``
    returned None, enforcement skipped it, and the resolver was never reached.
    That is a bypass: once capabilities are enforced it would silently pass every
    check, including the globally denied ones.

    This row is an IDENTITY, not a billing record. No CommercialSubscription, no
    Stripe customer, no Stripe subscription, no invoice, no price. The tenant
    stays excluded from every billing path by the existing ``is_internal_tenant``
    guards, which are untouched; ``entitlements.compute`` recognises an internal
    identity with no subscription and resolves it against the internal manifest.

    Idempotent and safe to run on every boot, matching ``ensure_tables`` and
    ``billing.service.ensure_plans``. Does nothing at all when no organization is
    marked ``billing_type='internal'``, so a deployment without a vendor tenant
    is unaffected.
    """
    from app.commercial import ids                       # noqa: PLC0415
    from app.commercial.models import CommercialOrganization  # noqa: PLC0415
    from database.models import Organization             # noqa: PLC0415

    db = _session()
    try:
        internals = (db.query(Organization)
                       .filter(Organization.billing_type == "internal").all())
        for org in internals:
            existing = (db.query(CommercialOrganization)
                          .filter(CommercialOrganization.operational_org_id == org.id)
                          .first())
            if existing is not None:
                continue
            db.add(CommercialOrganization(
                id=ids.org_id(),
                name=org.name,
                country=None,
                # `active` so lifecycle narrowing leaves it alone. It cannot be
                # past-due or cancelled: there is nothing to bill.
                commercial_state="active",
                operational_org_id=org.id,
            ))
            _log.info("internal commercial identity created for org=%s (%s)",
                      org.id, org.name)
        db.commit()
    except Exception:                                    # noqa: BLE001
        db.rollback()
        # Never block startup over this. Without the row the tenant simply keeps
        # today's behaviour rather than gaining the internal profile.
        _log.exception("could not ensure the internal commercial identity")
    finally:
        db.close()


def ensure_tables() -> None:
    """Create commercial_* tables if they don't exist.

    We use SQLAlchemy create_all targeted at our model tables only —
    no ALTER on existing legacy tables happens here.
    """
    from app.commercial.models import (
        CommercialOrganization, CommercialWorkspace, CommercialSignup,
        CommercialEvent, CommercialConsent, CommercialSubscription,
        EntitlementsCache, BillingLedgerEntry, CommercialInvoice,
        CheckoutSession, ProviderWebhookReceipt, FraudSignal,
        ManualReviewCase, CrmLead, EmailVerificationToken,
        TrialEmailSent, SignupRateBucket, OnboardingChecklist,
        CommercialIdempotencyKey,
    )
    tables = [
        CommercialOrganization.__table__, CommercialWorkspace.__table__,
        CommercialSignup.__table__, CommercialEvent.__table__,
        CommercialConsent.__table__, CommercialSubscription.__table__,
        EntitlementsCache.__table__, BillingLedgerEntry.__table__,
        CommercialInvoice.__table__, CheckoutSession.__table__,
        ProviderWebhookReceipt.__table__, FraudSignal.__table__,
        ManualReviewCase.__table__, CrmLead.__table__,
        EmailVerificationToken.__table__, TrialEmailSent.__table__,
        SignupRateBucket.__table__, OnboardingChecklist.__table__,
        CommercialIdempotencyKey.__table__,
    ]

    try:
        with engine.begin() as conn:
            for t in tables:
                t.create(bind=conn, checkfirst=True)
        _log.info("Commercial tables ensured (%d)", len(tables))
    except Exception:
        _log.exception("Failed to ensure commercial tables")


def install_routers(app) -> None:
    """Register all v1 routers onto the FastAPI app."""
    from app.commercial.routers.signup import router as signup_router
    from app.commercial.routers.verification import router as verification_router
    from app.commercial.routers.entitlements import router as entitlements_router
    from app.commercial.routers.leads import router as leads_router
    from app.commercial.routers.checkout import router as checkout_router
    from app.commercial.routers.webhooks import router as webhooks_router
    from app.commercial.routers.admin import router as admin_router
    from app.commercial.routers.onboarding import router as onboarding_router
    from app.commercial.routers.consent import router as consent_router
    from app.commercial.routers.activation import router as activation_router
    from app.commercial.routers.subscription import router as subscription_router

    for r in (
        signup_router, verification_router, entitlements_router,
        leads_router, checkout_router, webhooks_router,
        admin_router, onboarding_router, consent_router,
        activation_router, subscription_router,
    ):
        app.include_router(r)
    _log.info("Commercial v1 routers installed")
