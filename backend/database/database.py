from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv
import os

# ==============================
# ✅ LOAD ENV VARIABLES
# ==============================
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL is not set! Check your .env file.")

print("[OK] DATABASE_URL loaded:", DATABASE_URL[:40], "...")  # hide full URL

# ==============================
# ✅ CREATE DATABASE ENGINE
# ==============================
_USING_POOLER = ":6543" in (DATABASE_URL or "") or "-pooler." in (DATABASE_URL or "")


def _env_flag(name: str, default: bool) -> bool:
    """Boolean environment variable, same token set as the rest of the codebase
    (cf. app/offboarding/flags.py, app/schedule_policy/resolver.py).

    Unset or empty keeps `default`. An UNRECOGNISED value raises rather than
    guessing: `bool(os.getenv(...))` would read "false" as True, and a typo like
    "flase" must never silently reconfigure the connection pool in production.
    Raising here matches this module's existing behaviour for a missing
    DATABASE_URL — a misconfigured pool is caught at startup, not in prod
    latency graphs.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"❌ {name}={raw!r} is not a valid boolean. Use true/1/yes/on or "
        f"false/0/no/off, or leave it unset to keep the default ({default})."
    )


# pool_pre_ping issues a `SELECT 1` on every connection checkout, which costs one
# extra DB round trip per request. Production measurement (2026-08-17) puts a
# single indexed query at ~146 ms p50 from Cloud Run, roughly 4x the expected
# Tokyo->Seoul RTT, so the pre-ping round trip is a live hypothesis for part of
# that gap. This flag exists ONLY so the ON/OFF comparison can be run as a
# controlled production experiment.
#
# DEFAULT IS True — unset means exactly today's behaviour. Do NOT disable
# permanently without production measurements: pre_ping is what stops a
# Supabase-dropped idle connection surfacing as a request-level 500.
_POOL_PRE_PING = _env_flag("DB_POOL_PRE_PING", default=True)

# With Supabase transaction pooler (port 6543 / PgBouncer), SQLAlchemy keeps
# its own pool SMALL — PgBouncer does the real multiplexing. Large SQLAlchemy
# pools + PgBouncer = connection storms.
_connect_args: dict = {
    "connect_timeout": 5,   # TCP connect timeout — fail fast on network issues
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}

# IMPORTANT: PgBouncer in transaction-pooling mode REJECTS startup parameters
# in the `options` field with `FATAL: unsupported startup parameter in options`,
# which makes every DB-backed endpoint 500. Only set the per-statement timeout
# when we're talking to Postgres directly (port 5432). For pooled connections
# we fall back to the application-side `pool_timeout` (10 s) and `connect_timeout`
# above; per-query timeouts can be issued with SET LOCAL inside a transaction
# if individual endpoints want stricter bounds.
if not _USING_POOLER:
    _connect_args["options"] = "-c statement_timeout=15000"   # 15s query timeout

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

if _IS_SQLITE:
    # SQLite (used by the unit test suite) doesn't accept the connection-pool
    # tuning we need for Supabase Postgres, and it can't take psycopg-specific
    # connect_args like keepalives. Stand up a minimal engine instead.
    engine = create_engine(DATABASE_URL, future=True)
else:
    # Pool sizing strategy:
    #
    #   PgBouncer / Supabase transaction pooler (port 6543):
    #     PgBouncer handles the server→Postgres multiplexing, so there is no
    #     risk of a connection storm at the DB level from a larger client pool.
    #     The real danger is the OPPOSITE: a tiny SQLAlchemy pool (e.g. 5 slots)
    #     creates a Python-side queue bottleneck — with 120 concurrent Cloud Run
    #     requests, 115 threads block and time out even though PgBouncer is idle.
    #     Safe formula: pool_size=15, max_overflow=10 → 25 max client connections
    #     per container. With 5 containers: 125 total → trivial for PgBouncer.
    #
    #   Direct Postgres (port 5432 — Supabase direct connection or local dev):
    #     No external multiplexer; SQLAlchemy IS the pool. Size it large enough
    #     to serve concurrent requests without QueuePool timeouts.
    if _USING_POOLER:
        # PgBouncer handles real multiplexing — keep SQLAlchemy pool small.
        # 5 instances × 4 workers × (5+10) = 300 max → well within Supabase Pro limits.
        _pool_kwargs = dict(pool_size=5, max_overflow=10)
    else:
        _pool_kwargs = dict(pool_size=10, max_overflow=20)
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=_POOL_PRE_PING,   # default True — see _POOL_PRE_PING above
        pool_recycle=180,           # Recycle every 3min — Supabase can drop idle conns
        pool_use_lifo=True,
        pool_timeout=30,
        connect_args=_connect_args,
        # We don't use Postgres hstore anywhere. By default the psycopg2 dialect
        # runs an extra `_hstore_oids` introspection query on EVERY new
        # connection (in the on_connect event). Against the Supabase pooler that
        # round-trip is both wasteful and a fragile failure surface — a connection
        # dropped mid-introspection ("SSL connection has been closed unexpectedly")
        # surfaces from deep inside pool checkout. Disabling it removes the query.
        use_native_hstore=False,
        **_pool_kwargs,
    )

# ==============================
# ✅ SESSION CONFIG
# ==============================
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# ==============================
# ✅ BASE CLASS
# ==============================
Base = declarative_base()

# ==============================
# ✅ DB DEPENDENCY (FastAPI)
# ==============================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()