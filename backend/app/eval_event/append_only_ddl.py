"""
Append-only enforcement DDL for the ECO threshold event history (D1).
=====================================================================

ONE definition of the trigger SQL, imported by BOTH emitters, exactly as
``work_ledger_producer/append_only_ddl.py`` already does for the producer
cutover history:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits it with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it. Without the explicit emission a migrated
  database would lack the triggers while a test database had them, and every
  below-ORM test would pass vacuously against production's real shape.

WHY THIS MODULE HAS NO IMPORTS
------------------------------
It is imported by an Alembic revision. A migration must not drag application
models, metadata or a session into its own runtime, so this module holds string
constants and nothing else.

WHY TRIGGERS AND NOT LISTENERS
------------------------------
Ruling M and D18-F1: ORM listeners are bypassed by SQLAlchemy Core UPDATE, ORM
bulk ``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees, and an event history that can be quietly
rewritten is not evidence.

DIALECT SUPPORT
---------------
PostgreSQL (production) and SQLite (tests) — the two dialects this repository
supports. The SQLite statements are verified at runtime by the test suite. The
PostgreSQL statements are verified by offline emission and shape only: no
PostgreSQL is reachable in the ordinary suite, so their runtime behaviour is
reported as unverified rather than claimed.
"""

from __future__ import annotations

#: The append-only event history table (D1).
EVENT_TABLE = "eco_threshold_event"

#: Trigger names, shared so the migration's drop path and the presence tests
#: cannot drift from what is created.
SQLITE_NO_UPDATE_TRIGGER = "eco_threshold_event_no_update"
SQLITE_NO_DELETE_TRIGGER = "eco_threshold_event_no_delete"
POSTGRES_NO_UPDATE_TRIGGER = "eco_threshold_event_no_update"
POSTGRES_NO_DELETE_TRIGGER = "eco_threshold_event_no_delete"
POSTGRES_GUARD_FUNCTION = "eco_threshold_event_append_only"

_REFUSAL = (
    "eco_threshold_event is append-only (owner ruling D1): a recorded "
    "threshold event is never rewritten and never removed"
)

# ── SQLite ──────────────────────────────────────────────────────────────────
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE_TRIGGER} "
    f"BEFORE UPDATE ON {EVENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE_TRIGGER} "
    f"BEFORE DELETE ON {EVENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_UPDATE_TRIGGER}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_DELETE_TRIGGER}",
)

# ── PostgreSQL ──────────────────────────────────────────────────────────────
POSTGRES_APPEND_ONLY_DDL = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION '{_REFUSAL}: % refused', TG_OP; END; "
    f"$$ LANGUAGE plpgsql",
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE_TRIGGER} "
    f"BEFORE UPDATE ON {EVENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE_TRIGGER} "
    f"BEFORE DELETE ON {EVENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE_TRIGGER} ON {EVENT_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE_TRIGGER} ON {EVENT_TABLE}",
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

#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection, so the event store must never be run against
#: one — asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """The append-only statements for a dialect, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """The teardown statements for a dialect, or () if unsupported."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "EVENT_TABLE",
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
