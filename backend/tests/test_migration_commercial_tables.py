"""Migration validation for f0e1d2c3b4a5 (missing commercial tables).

Exercises the migration's upgrade/downgrade against a throwaway SQLite DB
(never the real DATABASE_URL) so CI catches a broken migration before deploy:

  - fresh DB: upgrade() creates all 12 tables with indexes/uniques/FKs
  - downgrade() drops them cleanly
  - existing DB (tables already present): upgrade() is a safe no-op
"""
import importlib.util
import pathlib

import sqlalchemy as sa
from sqlalchemy import inspect
from alembic.runtime.migration import MigrationContext
from alembic.operations import Operations

# Register full model metadata so FK parents (users, organizations) resolve.
import database.models        # noqa: F401
import app.commercial.models  # noqa: F401
from database.database import Base

_MIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "f0e1d2c3b4a5_add_missing_commercial_tables.py"
)

TWELVE = {
    "commercial_entitlements_cache", "commercial_billing_ledger", "commercial_invoices",
    "commercial_checkout_sessions", "commercial_webhook_receipts", "commercial_fraud_signals",
    "commercial_manual_review", "commercial_leads", "commercial_email_verification_tokens",
    "commercial_trial_emails_sent", "commercial_signup_rate_buckets", "commercial_onboarding_checklist",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_f0e1", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ops(conn):
    return Operations(MigrationContext.configure(connection=conn, opts={"target_metadata": None}))


def _create_parents(conn):
    """Create everything EXCEPT the 12 (simulates the pre-migration schema)."""
    parents = [t for n, t in Base.metadata.tables.items() if n not in TWELVE]
    Base.metadata.create_all(bind=conn, tables=parents)


def test_fresh_upgrade_then_downgrade():
    mig = _load_migration()
    eng = sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        _create_parents(conn)
        assert not (TWELVE & set(inspect(conn).get_table_names()))

        mig.op = _ops(conn)
        mig.upgrade()
        present = set(inspect(conn).get_table_names())
        assert TWELVE <= present, f"upgrade missed: {TWELVE - present}"

        # constraints/indexes landed
        assert "ix_commercial_invoices_number" in {i["name"] for i in inspect(conn).get_indexes("commercial_invoices")}
        assert "uq_webhook_provider_event" in {c["name"] for c in inspect(conn).get_unique_constraints("commercial_webhook_receipts")}
        fks = inspect(conn).get_foreign_keys("commercial_email_verification_tokens")
        assert any(f["referred_table"] == "users" for f in fks)

        mig.downgrade()
        assert not (TWELVE & set(inspect(conn).get_table_names()))


def test_upgrade_is_noop_on_existing_db():
    """If the 12 tables already exist (bootstrap create_all), upgrade must not
    error — it detects them and returns."""
    mig = _load_migration()
    eng = sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        Base.metadata.create_all(bind=conn)  # everything, including the 12
        assert TWELVE <= set(inspect(conn).get_table_names())
        mig.op = _ops(conn)
        mig.upgrade()  # must be a clean no-op, not "table already exists"
        assert TWELVE <= set(inspect(conn).get_table_names())
