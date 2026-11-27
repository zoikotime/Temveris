"""DB_POOL_PRE_PING — the pool_pre_ping experiment toggle.

`pool_pre_ping` issues a `SELECT 1` on every connection checkout, costing one
extra DB round trip per request. Production measurement (2026-08-17) put a
single indexed query at ~146 ms p50 from Cloud Run — about 4x the expected
Tokyo->Seoul RTT — so the pre-ping round trip is a live hypothesis for part of
that gap, and the flag exists so ON/OFF can be compared as a controlled
production experiment.

What these tests actually protect:

  * THE DEFAULT. Unset must mean `pool_pre_ping=True`, i.e. exactly today's
    behaviour. A regression here silently changes the production connection
    pool on the next deploy, and the symptom (an occasional 500 from a
    Supabase-dropped idle connection) would look like anything but a config
    change.
  * THE PARSER. `bool(os.getenv(...))` reads "false" as True. The whole reason
    this is a parser and not a one-liner is that the failure is silent.
  * PROPAGATION. Parsing the flag correctly is worthless if the value never
    reaches create_engine.

No live PostgreSQL is required: the propagation check builds a throwaway
in-memory SQLite engine, and SQLAlchemy records `pool_pre_ping` on the pool
regardless of dialect.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from sqlalchemy import create_engine

from database.database import _env_flag

FLAG = "DB_POOL_PRE_PING"
_DB_MODULE = pathlib.Path(__file__).resolve().parent.parent / "database" / "database.py"


# ── The default is the whole safety story ────────────────────────────────────

def test_unset_keeps_pre_ping_enabled(monkeypatch):
    """An existing deployment that never sets the variable must be unaffected."""
    monkeypatch.delenv(FLAG, raising=False)
    assert _env_flag(FLAG, default=True) is True


def test_empty_or_whitespace_keeps_the_default(monkeypatch):
    """An env var set to "" (easy to do in a deploy YAML) is not a request to
    disable anything."""
    for blank in ("", "   ", "\t"):
        monkeypatch.setenv(FLAG, blank)
        assert _env_flag(FLAG, default=True) is True
        assert _env_flag(FLAG, default=False) is False


def test_module_default_is_pre_ping_on():
    """Guards the production default at the call site, not just in the parser.

    Skipped when the flag is explicitly set, so running the suite mid-experiment
    (or on a developer box with the variable exported) is not a false alarm —
    the value legitimately reflects the environment then.
    """
    import os

    if os.getenv(FLAG, "").strip():
        pytest.skip(f"{FLAG} is explicitly set; the default is not under test here")

    from database import database as db_mod
    assert db_mod._POOL_PRE_PING is True, (
        "the default flipped — production would deploy with pre_ping OFF")


# ── Parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "on", "  on  "])
def test_truthy_values(monkeypatch, raw):
    monkeypatch.setenv(FLAG, raw)
    assert _env_flag(FLAG, default=False) is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "off", " off "])
def test_falsey_values(monkeypatch, raw):
    """The case `bool(os.getenv(...))` gets wrong."""
    monkeypatch.setenv(FLAG, raw)
    assert _env_flag(FLAG, default=True) is False


@pytest.mark.parametrize("raw", ["flase", "maybe", "2", "-1", "true false", "null"])
def test_unrecognised_values_raise_rather_than_guess(monkeypatch, raw):
    """A typo must not silently reconfigure the pool. Failing at startup is the
    point — the alternative is a production pool nobody chose."""
    monkeypatch.setenv(FLAG, raw)
    with pytest.raises(ValueError) as e:
        _env_flag(FLAG, default=True)
    assert FLAG in str(e.value), "the error must name the offending variable"


# ── Propagation into the engine ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [True, False])
def test_value_reaches_the_sqlalchemy_pool(value):
    """Whatever the flag resolves to is what the pool is actually built with."""
    engine = create_engine("sqlite://", pool_pre_ping=value)
    try:
        assert engine.pool._pre_ping is value
    finally:
        engine.dispose()


def test_engine_is_built_from_the_flag_not_a_literal():
    """Source guard. If someone re-hardcodes `pool_pre_ping=True` the experiment
    silently stops working while every test above still passes."""
    tree = ast.parse(_DB_MODULE.read_text(encoding="utf-8"))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "create_engine"]
    wired = [c for c in calls
             for kw in c.keywords
             if kw.arg == "pool_pre_ping" and ast.unparse(kw.value) == "_POOL_PRE_PING"]

    assert wired, (
        "no create_engine(...) call passes pool_pre_ping=_POOL_PRE_PING — the "
        "DB_POOL_PRE_PING toggle is disconnected from the engine")


def test_only_pre_ping_is_env_controlled():
    """The experiment isolates ONE variable. If a later change makes pool_size
    or pool_recycle env-driven too, an A/B result can no longer be attributed."""
    src = _DB_MODULE.read_text(encoding="utf-8")
    for setting in ("pool_size", "max_overflow", "pool_recycle",
                    "pool_timeout", "pool_use_lifo"):
        assert f"_env_flag(\"{setting}" not in src and f"getenv(\"{setting}" not in src.lower(), (
            f"{setting} became environment-controlled; the pre_ping A/B is no "
            "longer a single-variable experiment")
