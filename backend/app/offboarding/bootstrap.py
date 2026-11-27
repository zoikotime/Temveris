"""Offboarding module bootstrap (migrations + router wiring).

Called from main.py lifespan startup. Idempotent — safe to run on every boot.
Mirrors the commercial layer's bootstrap contract (ensure_tables + install).
"""
from __future__ import annotations

import logging

from database.database import engine
from app.offboarding import models as _models  # noqa: F401 — registers tables on Base

_log = logging.getLogger("zoiko.offboarding.bootstrap")


def ensure_tables() -> None:
    """Create offboarding_* tables if they don't exist. Targets only this
    module's tables — no ALTER on legacy tables happens here."""
    try:
        with engine.begin() as conn:
            for model in _models.ALL_MODELS:
                model.__table__.create(bind=conn, checkfirst=True)
        _log.info("Offboarding tables ensured (%d)", len(_models.ALL_MODELS))
    except Exception:
        _log.exception("Failed to ensure offboarding tables")


def install_router(app) -> None:
    """Register every offboarding router onto the FastAPI app:

      • lifecycle router      (case CRUD + closure workflow)
      • security router       (revoke-access, retry-revocation, reconciliation,
                              security-events — Tier-0 enforcement API)
      • governance router     (former-access, legal-hold, retention, certificate,
                              evidence-package, deletion-review)
      • former-worker portal  (isolated; no admin/analytics surface)
    """
    from app.offboarding.router import router as offboarding_router
    from app.offboarding.security_router import router as security_router
    from app.offboarding.governance_router import router as governance_router
    from app.offboarding.portal_router import router as portal_router
    from app.offboarding.ops_router import router as ops_router
    app.include_router(offboarding_router)
    app.include_router(security_router)
    app.include_router(governance_router)
    app.include_router(portal_router)
    app.include_router(ops_router)
    _log.info("Offboarding routers installed at /api/offboarding "
              "(+ security, governance, former-worker portal, ops/metrics)")


def seed_retention_policies(tenant_id: str = "default") -> int:
    """Seed the bootstrap library of retention policies (§5 governance spec).
    Idempotent — call on startup; a code that already has an active version is
    left untouched."""
    from database.database import SessionLocal
    from app.offboarding import retention_engine
    db = SessionLocal()
    try:
        n = retention_engine.seed_default_policies(db, tenant_id=tenant_id)
        if n:
            db.commit()
        return n
    finally:
        db.close()
