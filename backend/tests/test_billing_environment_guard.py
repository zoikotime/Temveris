"""Environment safety: Stripe mode, job gating, and configuration readiness.

Three ways a developer's laptop could reach real customers, all live before this:

1. ``stripe.api_key`` is bound at import from ``backend/.env`` — a tracked file, so a
   live key placed in it would travel to every clone with no sign anything was unusual.
2. Nothing checked that the secret key, the six price ids and the webhook secret were
   the same mode. A live key with a test webhook secret is the worst case: Stripe takes
   the money and every event fails signature verification, so the customer pays and is
   never activated. It fails *silently*.
3. All six billing sweeps started wherever the app booted. Only ``DISABLE_EMAILS=true``
   in a local ``.env`` stood between a developer and real dunning mail — and about
   thirty real emails were once sent exactly that way.

Every test manipulates only environment variables. No Stripe call, no database write,
no mail.
"""

from __future__ import annotations

import pytest

from app.billing import environment as env

STRIPE_VARS = ("STRIPE_SECRET_KEY", "STRIPE_MODE", "STRIPE_WEBHOOK_SECRET",
               "APP_ENV", "ENVIRONMENT", "BILLING_JOBS_ENABLED",
               "BILLING_JOBS_DISABLED", "DISABLE_EMAILS")


@pytest.fixture
def clean_env(monkeypatch):
    """Start from a blank slate so a developer's real .env cannot skew a result."""
    for var in STRIPE_VARS + env.REQUIRED_PRICE_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _all_prices(mp):
    for var in env.REQUIRED_PRICE_VARS:
        mp.setenv(var, "price_fake")


# ══════════════════════════════════════════════════════════════════════════════
# Mode detection
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key,expected", [
    ("sk_test_abc", env.MODE_TEST),
    ("rk_test_abc", env.MODE_TEST),
    ("sk_live_abc", env.MODE_LIVE),
    ("rk_live_abc", env.MODE_LIVE),
    ("nonsense", None),
    ("", None),
])
def test_key_mode_is_read_from_the_prefix(clean_env, key, expected):
    clean_env.setenv("STRIPE_SECRET_KEY", key)
    assert env.key_mode() == expected


def test_environment_defaults_to_development(clean_env):
    """The safe default: an unset variable means no live keys and no billing jobs."""
    assert env.environment() == env.ENV_DEVELOPMENT
    assert env.is_production() is False


@pytest.mark.parametrize("raw,expected", [
    ("production", env.ENV_PRODUCTION), ("prod", env.ENV_PRODUCTION),
    ("staging", env.ENV_STAGING), ("development", env.ENV_DEVELOPMENT),
    ("nonsense", env.ENV_DEVELOPMENT),
])
def test_environment_parsing(clean_env, raw, expected):
    clean_env.setenv("APP_ENV", raw)
    assert env.environment() == expected


# ══════════════════════════════════════════════════════════════════════════════
# The rule that matters: live credentials only in production
# ══════════════════════════════════════════════════════════════════════════════
def test_live_key_on_a_developer_machine_is_an_error(clean_env):
    """The scenario the guard exists for: a live key pulled onto a laptop."""
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    report = env.check()
    assert report["safe"] is False
    assert any("LIVE Stripe credentials in a development environment" in e
               for e in report["errors"])
    with pytest.raises(env.BillingConfigurationError):
        env.assert_safe()


def test_live_key_in_staging_is_also_an_error(clean_env):
    clean_env.setenv("APP_ENV", "staging")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    assert env.check()["safe"] is False


def test_live_key_in_production_is_allowed(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "live")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    _all_prices(clean_env)
    report = env.check()
    assert report["safe"] is True, report["errors"]
    assert report["stripe_mode_effective"] == env.MODE_LIVE


def test_declared_mode_disagreeing_with_the_key_is_an_error(clean_env):
    """Half-finished cutover: STRIPE_MODE says one thing, the key says another."""
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "test")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    errors = env.check()["errors"]
    assert any("refusing to guess which one is right" in e for e in errors)


def test_test_key_in_production_warns_but_still_boots(clean_env):
    """Refusing to start would take attendance and payroll down over a billing setting."""
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "test")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    _all_prices(clean_env)
    report = env.check()
    assert report["safe"] is True
    assert any("no real payment can be collected" in w for w in report["warnings"])
    env.assert_safe()          # must not raise


def test_an_unset_declared_mode_warns_so_the_next_mismatch_is_an_error(clean_env):
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    assert any("STRIPE_MODE is not set" in w for w in env.check()["warnings"])


# ══════════════════════════════════════════════════════════════════════════════
# Required configuration
# ══════════════════════════════════════════════════════════════════════════════
def test_missing_price_variables_are_errors_in_production(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "test")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "whsec_abc")
    report = env.check()
    assert report["safe"] is False
    assert report["missing_price_vars"] == list(env.REQUIRED_PRICE_VARS)
    assert any("No such price" in e for e in report["errors"])


def test_a_missing_webhook_secret_is_an_error_in_production(clean_env):
    """The silent failure: money taken, every event refused, customer never activated."""
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "test")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    _all_prices(clean_env)
    errors = env.check()["errors"]
    assert any("never activated" in e for e in errors)


def test_all_six_price_variables_are_required(clean_env):
    assert len(env.REQUIRED_PRICE_VARS) == 6
    for plan in ("VERIFIED", "GOVERNED", "SOVEREIGN"):
        for cycle in ("MONTHLY", "ANNUAL"):
            assert f"STRIPE_PRICE_{plan}_{cycle}" in env.REQUIRED_PRICE_VARS


# ══════════════════════════════════════════════════════════════════════════════
# Job gating
# ══════════════════════════════════════════════════════════════════════════════
def test_billing_jobs_are_off_outside_production(clean_env):
    assert env.billing_jobs_enabled() is False
    clean_env.setenv("APP_ENV", "staging")
    assert env.billing_jobs_enabled() is False


def test_billing_jobs_are_on_in_production(clean_env):
    clean_env.setenv("APP_ENV", "production")
    assert env.billing_jobs_enabled() is True


def test_jobs_can_be_force_enabled_for_a_deliberate_exercise(clean_env):
    clean_env.setenv("BILLING_JOBS_ENABLED", "true")
    assert env.billing_jobs_enabled() is True
    assert any("force-enabled" in w for w in env.check()["warnings"])


def test_jobs_can_be_force_disabled_in_production(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("BILLING_JOBS_DISABLED", "true")
    assert env.billing_jobs_enabled() is False


def test_jobs_plus_email_outside_production_is_an_error(clean_env):
    """Exactly how thirty real emails were once sent from a developer machine."""
    clean_env.setenv("BILLING_JOBS_ENABLED", "true")
    clean_env.setenv("DISABLE_EMAILS", "false")
    errors = env.check()["errors"]
    assert any("real customers were emailed from a developer machine" in e
               for e in errors)


@pytest.mark.parametrize("module_name", ["seats", "reconcile", "invoices",
                                         "payment_methods", "dunning", "trial"])
def test_every_sweep_consults_the_gate(module_name):
    """Gated inside each job, so the task still exists for the shutdown handler."""
    import importlib, inspect
    mod = importlib.import_module(f"app.billing.{module_name}")
    src = inspect.getsource(mod.run_forever)
    assert "billing_jobs_enabled" in src, module_name
    assert "return" in src, module_name


# ══════════════════════════════════════════════════════════════════════════════
# Secrets must never be reported
# ══════════════════════════════════════════════════════════════════════════════
def test_the_report_never_contains_a_secret_value(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("STRIPE_MODE", "live")
    clean_env.setenv("STRIPE_SECRET_KEY", "sk_live_SUPERSECRETVALUE")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "whsec_ANOTHERSECRET")
    for var in env.REQUIRED_PRICE_VARS:
        clean_env.setenv(var, "price_SECRETID")
    blob = repr(env.check())
    for secret in ("SUPERSECRETVALUE", "ANOTHERSECRET", "SECRETID"):
        assert secret not in blob
    # Only presence and mode are reported.
    assert env.check()["webhook_secret_present"] is True


def test_readiness_reports_schema_state(db):
    """Correct Stripe configuration on a half-migrated database is not ready either."""
    report = env.readiness(db)
    assert "schema" in report
    assert "ready" in report and "safe" in report


def test_the_guard_cannot_call_stripe_at_all():
    """The strongest form of "no network calls at startup": it never imports stripe.

    Asserted on the module's imports rather than by grepping the source, because the
    docstring legitimately mentions ``stripe.api_key`` when explaining what it guards
    against — a substring check flagged that as a violation.
    """
    import ast, inspect
    tree = ast.parse(inspect.getsource(env))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "stripe" not in imported, imported
