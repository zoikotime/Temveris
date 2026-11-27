"""Shared pytest fixtures.

These tests use an in-memory SQLite database so they run with zero
external dependencies — no Supabase, no Stripe, no SMTP, no Redis.
They cover only the pure-code commercial layer (state machines, routing,
fraud, entitlements). Integration tests that hit Postgres-only features
should mark themselves with @pytest.mark.requires_postgres.
"""

from __future__ import annotations

import os
import sys
import pathlib

# Make `backend` importable when running `pytest` from the repo root.
BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Override DATABASE_URL *before* importing anything that calls load_dotenv.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest                                # noqa: E402
from sqlalchemy import create_engine         # noqa: E402
from sqlalchemy.orm import sessionmaker      # noqa: E402
from sqlalchemy.pool import StaticPool       # noqa: E402

# Importing this creates the SQLAlchemy engine — we replace it below.
from database import database as _db         # noqa: E402


@pytest.fixture(scope="session")
def engine():
    # StaticPool + check_same_thread=False so the FastAPI TestClient (which
    # serves each request from a worker thread) sees the same in-memory DB
    # as the test setup. Without this, integration tests that round-trip
    # through TestClient hit "no such table" because each thread creates
    # its own private :memory: instance.
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    _db.engine = eng
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return eng


@pytest.fixture(scope="session", autouse=True)
def _schema(engine):
    """Create commercial tables (and minimum legacy tables we depend on)."""
    from database.models import User  # legacy
    from app.commercial import models as cm

    # Legacy users table is referenced by FKs in commercial models.
    User.__table__.create(engine, checkfirst=True)

    # ── Operational tenant tables ────────────────────────────────────────────
    # Self-serve signup provisions a full operational tenant (via
    # app.org.provisioning_service), not just a commercial organization, so the
    # signup/activation tests need the operational side of the schema too.
    # `organizations` in particular is the integer-PK tenant anchor that
    # `users.organization_id` points at — without it a signup cannot complete.
    from database.models import (
        Organization, OrgSettings, AppSettings, LeavePolicy, Team, AuditLog,
    )
    for t in (Organization, OrgSettings, AppSettings, LeavePolicy, Team, AuditLog):
        t.__table__.create(engine, checkfirst=True)

    # Subset of commercial tables required by these tests.
    tables = [
        cm.CommercialOrganization, cm.CommercialWorkspace, cm.CommercialSignup,
        cm.CommercialEvent, cm.CommercialConsent, cm.CommercialSubscription,
        cm.EntitlementsCache, cm.BillingLedgerEntry, cm.CommercialInvoice,
        cm.CheckoutSession, cm.PriceQuote, cm.ProviderWebhookReceipt, cm.FraudSignal,
        cm.ManualReviewCase, cm.CrmLead, cm.EmailVerificationToken,
        cm.TrialEmailSent, cm.SignupRateBucket, cm.OnboardingChecklist,
        cm.CommercialIdempotencyKey,
    ]
    for t in tables:
        t.__table__.create(engine, checkfirst=True)

    # ── Offboarding lifecycle ────────────────────────────────────────────────
    # Legacy auth/clock/activity tables the offboarding services revoke against
    # (and the tracking-stop sweep queries against), plus the 15 offboarding
    # tables themselves.
    from database.models import Activity, ClockSession, RefreshToken, UserSession
    for t in (ClockSession, UserSession, RefreshToken, Activity):
        t.__table__.create(engine, checkfirst=True)

    from app.offboarding import models as offb
    for model in offb.ALL_MODELS:
        model.__table__.create(engine, checkfirst=True)
    yield


@pytest.fixture
def db(engine):
    SessionLocal = _db.SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
