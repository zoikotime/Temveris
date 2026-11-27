"""
Kairos-specific SQLAlchemy engine.

By default Kairos shares the main app's Postgres instance (the `engine`
exported from `database/database.py`). When `KAIROS_DATABASE_URL` is set
we point Kairos at a separate Postgres instance instead — used when the
chatbot's `chatbot_*` tables live in a different Supabase project than
the main `users`/`employees` schema.

Auth still happens against the main DB (via `get_current_user`); only
the Kairos chatbot tables are read/written through this engine.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.database import engine as _main_engine, SessionLocal as _MainSessionLocal


_KAIROS_URL = (os.getenv("KAIROS_DATABASE_URL") or "").strip()


def _build_kairos_engine():
    """Return the engine Kairos should use.

    If KAIROS_DATABASE_URL is set and different from DATABASE_URL we mint a
    dedicated engine so Kairos traffic can target its own project; pool
    tuning mirrors the main engine."""
    if not _KAIROS_URL:
        return _main_engine

    main_url = os.getenv("DATABASE_URL", "")
    if _KAIROS_URL == main_url:
        return _main_engine

    using_pooler = ":6543" in _KAIROS_URL
    is_sqlite = _KAIROS_URL.startswith("sqlite")

    if is_sqlite:
        return create_engine(_KAIROS_URL, future=True)

    connect_args: dict = {
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    if not using_pooler:
        connect_args["options"] = "-c statement_timeout=15000"

    return create_engine(
        _KAIROS_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=3,
        pool_recycle=180,
        pool_use_lifo=True,
        pool_timeout=10,
        connect_args=connect_args,
    )


engine = _build_kairos_engine()

# Reuse the main SessionLocal when we share the engine; mint a fresh one
# only when Kairos targets a different DB. This avoids unnecessary thread
# pools when KAIROS_DATABASE_URL is unset (the common case).
if engine is _main_engine:
    SessionLocal = _MainSessionLocal
else:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_kairos_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
