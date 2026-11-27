"""Billing environment safety: Stripe mode, job gating, and a readiness check.

WHY THIS EXISTS
---------------
Three ways a developer's laptop could reach real customers, all of them live before
this module:

1. ``stripe.api_key`` is bound at import from whatever ``backend/.env`` holds. That file
   is tracked in git, so a live key placed in it would travel to every clone — and the
   app would use it with no indication that anything unusual was happening.
2. Nothing checked that the secret key, the six price ids and the webhook secret were
   the same mode. A live key with a test webhook secret is the worst of these: Stripe
   takes the money and every event fails signature verification, so the customer pays
   and is never activated. It fails *silently*.
3. All six billing sweeps start wherever the app boots. Only ``DISABLE_EMAILS=true`` in
   a local ``.env`` stood between a developer and real dunning mail — and about thirty
   real emails were once sent exactly that way.

DESIGN
------
Declared, not sniffed. ``STRIPE_MODE`` states the intent and the key must agree with it,
so a mismatch is a boot-time error rather than a discovery made from an invoice.

Deliberately no network calls: price ids do not encode their mode, so validating them
would mean a Stripe round-trip on every boot — a new startup dependency and a new
failure mode. The prefix of the secret key is enough to catch the realistic mistake
(a half-finished cutover), and the readiness check covers the rest on demand.

Fails closed in one direction only. Live credentials in a non-production environment
raise; test credentials in production warn loudly but still boot, because refusing to
start would turn a configuration slip into an outage for a product that is mostly not
billing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

_log = logging.getLogger("zoiko.billing.environment")

MODE_TEST = "test"
MODE_LIVE = "live"

# Deployment environments. Only PRODUCTION may hold live Stripe credentials or run the
# billing sweeps.
ENV_PRODUCTION = "production"
ENV_STAGING = "staging"
ENV_DEVELOPMENT = "development"

_TRUE = {"1", "true", "yes", "on"}

# Every price variable the runtime resolves. Named here so the readiness check can
# report a missing one instead of a customer discovering it at checkout.
REQUIRED_PRICE_VARS = tuple(
    f"STRIPE_PRICE_{plan}_{cycle}"
    for plan in ("VERIFIED", "GOVERNED", "SOVEREIGN")
    for cycle in ("MONTHLY", "ANNUAL")
)


class BillingConfigurationError(RuntimeError):
    """The billing configuration is unsafe to run. Raised at import of the guard."""


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def environment() -> str:
    """The deployment environment, defaulting to development.

    Defaulting to *development* is the safe direction: an unset variable then means
    "no live keys, no billing sweeps" rather than "act like production".
    """
    raw = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if raw in (ENV_PRODUCTION, "prod"):
        return ENV_PRODUCTION
    if raw in (ENV_STAGING, "stage"):
        return ENV_STAGING
    return ENV_DEVELOPMENT


def is_production() -> bool:
    return environment() == ENV_PRODUCTION


def key_mode(secret_key: Optional[str] = None) -> Optional[str]:
    """The mode implied by the secret key's prefix, or None if unrecognisable."""
    key = secret_key if secret_key is not None else (os.getenv("STRIPE_SECRET_KEY") or "")
    key = key.strip()
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        return MODE_LIVE
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return MODE_TEST
    return None


def declared_mode() -> Optional[str]:
    """What STRIPE_MODE says the operator intends, if set."""
    raw = (os.getenv("STRIPE_MODE") or "").strip().lower()
    return raw if raw in (MODE_TEST, MODE_LIVE) else None


def effective_mode() -> Optional[str]:
    """The mode actually in force: the declaration if present, else the key's prefix."""
    return declared_mode() or key_mode()


def billing_jobs_enabled() -> bool:
    """Whether the billing sweeps may run in this process.

    Off outside production unless explicitly forced, because the sweeps send dunning and
    trial mail and can modify Stripe seat quantities. ``BILLING_JOBS_ENABLED`` can turn
    them on for a deliberate staging exercise; it is never on by accident.
    """
    if _flag("BILLING_JOBS_ENABLED"):
        return True
    if _flag("BILLING_JOBS_DISABLED"):
        return False
    return is_production()


def emails_enabled() -> bool:
    """Mirrors app.email_service's own switch, for reporting purposes only."""
    return not _flag("DISABLE_EMAILS")


def check() -> Dict[str, Any]:
    """Assess the billing configuration. Never raises, never calls Stripe.

    Returns a report with ``errors`` (unsafe) and ``warnings`` (suspicious). Used by the
    boot guard and by the readiness endpoint, so both judge the configuration by exactly
    the same rules.
    """
    env = environment()
    declared = declared_mode()
    key = key_mode()
    effective = declared or key
    errors: List[str] = []
    warnings: List[str] = []

    if key is None:
        (errors if env == ENV_PRODUCTION else warnings).append(
            "STRIPE_SECRET_KEY is missing or has an unrecognised prefix"
        )
    if declared and key and declared != key:
        errors.append(
            f"STRIPE_MODE={declared} but STRIPE_SECRET_KEY is a {key} key — "
            "refusing to guess which one is right"
        )
    if declared is None and key is not None:
        warnings.append(
            f"STRIPE_MODE is not set; inferring {key} mode from the key prefix. "
            "Set it explicitly so a mismatch becomes an error rather than a surprise"
        )

    # The rule that matters: live credentials must not exist outside production.
    if effective == MODE_LIVE and env != ENV_PRODUCTION:
        errors.append(
            f"LIVE Stripe credentials in a {env} environment. Real cards would be "
            "charged from a non-production process. Set APP_ENV=production only where "
            "that is genuinely intended, and use test keys everywhere else"
        )
    if effective == MODE_TEST and env == ENV_PRODUCTION:
        warnings.append(
            "production is running TEST Stripe credentials — no real payment can be "
            "collected (expected before the live cutover)"
        )

    missing_prices = [v for v in REQUIRED_PRICE_VARS if not (os.getenv(v) or "").strip()]
    if missing_prices:
        (errors if env == ENV_PRODUCTION else warnings).append(
            f"missing price variables: {', '.join(missing_prices)} — an upgrade to the "
            "affected plan fails with 'No such price'"
        )
    if not (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip():
        (errors if env == ENV_PRODUCTION else warnings).append(
            "STRIPE_WEBHOOK_SECRET is missing — every webhook is refused, so a paid "
            "customer is never activated"
        )

    if env == ENV_PRODUCTION and not billing_jobs_enabled():
        warnings.append("billing background jobs are disabled in production")
    if env != ENV_PRODUCTION and billing_jobs_enabled():
        warnings.append(
            f"billing jobs are force-enabled in a {env} environment — dunning and trial "
            "mail may be sent to real recipients"
        )
    if env != ENV_PRODUCTION and emails_enabled() and billing_jobs_enabled():
        errors.append(
            f"{env} environment has billing jobs enabled AND email sending enabled — "
            "this is how real customers were emailed from a developer machine"
        )

    return {
        "environment": env,
        "stripe_mode_declared": declared,
        "stripe_mode_from_key": key,
        "stripe_mode_effective": effective,
        "billing_jobs_enabled": billing_jobs_enabled(),
        "emails_enabled": emails_enabled(),
        "price_vars_present": len(REQUIRED_PRICE_VARS) - len(missing_prices),
        "price_vars_required": len(REQUIRED_PRICE_VARS),
        "missing_price_vars": missing_prices,
        "webhook_secret_present": bool((os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()),
        "errors": errors,
        "warnings": warnings,
        "safe": not errors,
    }


def assert_safe() -> Dict[str, Any]:
    """Raise if the configuration is unsafe; log the mode either way.

    Called once from the lifespan handler. Secrets are never logged — only the mode,
    which is exactly the fact that was previously impossible to see.
    """
    report = check()
    banner = (report["stripe_mode_effective"] or "UNKNOWN").upper()
    for warning in report["warnings"]:
        _log.warning("billing configuration: %s", warning)
    if report["errors"]:
        for error in report["errors"]:
            _log.error("billing configuration: %s", error)
        raise BillingConfigurationError("; ".join(report["errors"]))
    _log.warning("Stripe %s mode | environment=%s | billing jobs=%s | emails=%s",
                 banner, report["environment"],
                 "on" if report["billing_jobs_enabled"] else "off",
                 "on" if report["emails_enabled"] else "off")
    return report


def readiness(db=None) -> Dict[str, Any]:
    """Configuration readiness for an operator. No secrets, no Stripe calls.

    ``db`` is optional so the check still works without a database; when supplied it
    also confirms the billing schema and plan catalogue are present, since a correct
    Stripe configuration on a half-migrated database is not ready either.
    """
    report = check()
    report["schema"] = {}
    if db is not None:
        from sqlalchemy import text                     # noqa: PLC0415
        try:
            version = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            report["schema"]["alembic_version"] = version
            for table in ("billing_subscriptions", "billing_webhook_events",
                          "billing_dunning_notices", "billing_plans"):
                present = db.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
                ).scalar()
                report["schema"][table] = bool(present)
                if not present:
                    report["errors"].append(f"table {table} is missing — migrations "
                                            "have not been applied")
            plans = db.execute(
                text("SELECT count(*) FROM billing_plans WHERE active_status")
            ).scalar()
            report["schema"]["active_plans"] = plans
            if not plans:
                report["errors"].append("no active billing plans — the catalogue is empty")
        except Exception as exc:                        # noqa: BLE001
            report["errors"].append(f"could not inspect the billing schema: "
                                    f"{type(exc).__name__}")
    report["safe"] = not report["errors"]
    report["ready"] = report["safe"] and not report["missing_price_vars"]
    return report
