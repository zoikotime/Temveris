"""
Append-only enforcement DDL for the producer cutover history (D18-F1).
======================================================================

ONE definition of the trigger SQL, imported by BOTH emitters:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits it with ``op.execute``, because ``op.create_table``
  builds its own ``Table`` object and the ORM-table listener does NOT fire for
  it (verified). Without an explicit emission the migrated schema would lack
  the triggers while the test schema had them, and every below-ORM test would
  pass vacuously against production's real shape.

WHY THIS MODULE HAS NO IMPORTS
------------------------------
It is imported by an Alembic revision. A migration must not drag application
models, metadata or a database session into its own runtime, so this module
holds string constants and nothing else — no ``Base``, no models, no SQLAlchemy.
That keeps the "one shared constant" requirement satisfiable without making a
revision depend on the ORM.

WHY TRIGGERS AND NOT LISTENERS
------------------------------
Owner ruling D18-F1. The post-implementation audit demonstrated that ORM
listeners are bypassed by SQLAlchemy Core UPDATE, ORM bulk ``query.update()``,
ORM ``delete()`` and Core DELETE — the same class of gap that moved five
history tables to database-level RESTRICT under Ruling M ("a Core bulk delete
never fires the ORM listeners"). Only the database can refuse a statement the
ORM never sees.

DIALECT SUPPORT
---------------
This repository supports exactly two dialects: PostgreSQL (production,
``postgresql://`` via psycopg2) and SQLite (the in-process test database,
``database/database.py::_IS_SQLITE``). No other dialect is referenced anywhere,
and none is claimed here.

The SQLite statements are verified at runtime by the test suite. The PostgreSQL
statements are verified only by offline compilation and by asserting their
presence in the emitted DDL: **no PostgreSQL database is reachable in this
environment and connecting to one is prohibited**, so their runtime behaviour
remains externally unverified. That limitation is stated in the tests and in
the increment report rather than papered over.
"""

from __future__ import annotations

#: The append-only history table (D18-F1).
CUTOVER_HISTORY_TABLE = "work_ledger_producer_cutover_history"

#: Trigger names, shared so the migration's drop path and the presence tests
#: cannot drift from what is created.
SQLITE_NO_UPDATE_TRIGGER = "wl_cutover_history_no_update"
SQLITE_NO_DELETE_TRIGGER = "wl_cutover_history_no_delete"
POSTGRES_NO_UPDATE_TRIGGER = "wl_cutover_history_no_update"
POSTGRES_NO_DELETE_TRIGGER = "wl_cutover_history_no_delete"
POSTGRES_GUARD_FUNCTION = "wl_cutover_history_append_only"

_REFUSAL = (
    "work_ledger_producer_cutover_history is append-only "
    "(owner ruling D18-F1): the producer watermark may never move earlier"
)

# ── SQLite ──────────────────────────────────────────────────────────────────
# RAISE(ABORT, ...) in a BEFORE trigger refuses the statement. Verified to
# block Core UPDATE, Core DELETE, raw UPDATE, raw DELETE, ORM bulk update and
# ORM delete. SQLite enforces triggers unconditionally — there is no pragma to
# turn them off, unlike foreign keys.
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE_TRIGGER} "
    f"BEFORE UPDATE ON {CUTOVER_HISTORY_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE_TRIGGER} "
    f"BEFORE DELETE ON {CUTOVER_HISTORY_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_UPDATE_TRIGGER}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_DELETE_TRIGGER}",
)

# ── PostgreSQL ──────────────────────────────────────────────────────────────
# A trigger needs a function, so this is three statements rather than two.
# ``EXECUTE FUNCTION`` is the PostgreSQL 11+ spelling; production runs a
# modern server. RAISE EXCEPTION aborts the statement and its transaction.
POSTGRES_APPEND_ONLY_DDL = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION '{_REFUSAL}: % refused', TG_OP; END; "
    f"$$ LANGUAGE plpgsql",
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE_TRIGGER} "
    f"BEFORE UPDATE ON {CUTOVER_HISTORY_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE_TRIGGER} "
    f"BEFORE DELETE ON {CUTOVER_HISTORY_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE_TRIGGER} "
    f"ON {CUTOVER_HISTORY_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE_TRIGGER} "
    f"ON {CUTOVER_HISTORY_TABLE}",
    f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()",
)

#: Dialect name -> the statements that make the table append-only.
APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_APPEND_ONLY_DDL,
}

#: Dialect name -> the statements that remove it again (downgrade only).
DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_DROP_DDL,
    "postgresql": POSTGRES_DROP_DDL,
}

#: The dialects this repository actually supports. A dialect absent from this
#: set gets NO database-level protection, so the producer must never be run
#: against one — asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """The append-only statements for a dialect, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """The teardown statements for a dialect, or () if unsupported."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "CUTOVER_HISTORY_TABLE",
    "SQLITE_APPEND_ONLY_DDL",
    "SQLITE_DROP_DDL",
    "POSTGRES_APPEND_ONLY_DDL",
    "POSTGRES_DROP_DDL",
    "APPEND_ONLY_DDL_BY_DIALECT",
    "DROP_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "SQLITE_NO_UPDATE_TRIGGER",
    "SQLITE_NO_DELETE_TRIGGER",
    "POSTGRES_NO_UPDATE_TRIGGER",
    "POSTGRES_NO_DELETE_TRIGGER",
    "POSTGRES_GUARD_FUNCTION",
    "append_only_statements",
    "drop_statements",
]
