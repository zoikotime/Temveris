"""
Append-only enforcement DDL for the session-boundary provenance history (Owner Decision 1A).
==================================================================

ONE definition of the trigger SQL, imported by BOTH emitters -- exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py``,
``policy_version/append_only_ddl.py`` and
``work_ledger_producer/append_only_ddl.py`` already do:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits the SAME statements with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it. Without the explicit emission a migrated
  database would lack the triggers while a test database had them, and every
  below-ORM test would pass vacuously against production's real shape.

WHY THIS TABLE
--------------
Owner ruling §45.D-15: one immutable row per boundary write, and a
later boundary write is recorded as a NEW row rather than by editing
the earlier one. Its ORM guard already refuses every UPDATE and every
DELETE, with no authorized exception, so the unconditional trigger
below matches the contract exactly.

This is the provenance the Work Ledger reads to decide whether a
departure is VERIFIED. A boundary event that could be rewritten would
make every sealed day downstream of it unverifiable.

NO IMPORTS
----------
Imported by an Alembic revision, so it holds string constants and nothing else.
A migration must not drag application models, metadata or a session into its
own runtime.

WHY TRIGGERS AND NOT LISTENERS
------------------------------
Ruling M and D18-F1: ORM listeners are bypassed by Core UPDATE, bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees.

DIALECT SUPPORT
---------------
PostgreSQL (production) and SQLite (the in-process test database). The SQLite
statements are verified at runtime by the test suite; the PostgreSQL statements
are verified by offline compilation and by asserting their presence in the
emitted DDL, because no PostgreSQL database is reachable in this environment
and connecting to one is prohibited.
"""

from __future__ import annotations


#: One immutable record of one boundary write (§45.D-15).
BOUNDARY_EVENT_TABLE = "clock_session_boundary_event"

BOUNDARY_SQLITE_NO_UPDATE = "clock_session_boundary_event_no_update"
BOUNDARY_SQLITE_NO_DELETE = "clock_session_boundary_event_no_delete"
BOUNDARY_PG_NO_UPDATE = "clock_session_boundary_event_no_update"
BOUNDARY_PG_NO_DELETE = "clock_session_boundary_event_no_delete"
BOUNDARY_PG_FUNCTION = "clock_session_boundary_event_append_only"

_BOUNDARY_REFUSAL = (
    "clock_session_boundary_event is append-only (owner decision 1A): a later boundary write is recorded as a NEW row, never by editing the earlier one"
)

BOUNDARY_SQLITE_DDL = (
    f"CREATE TRIGGER {BOUNDARY_SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {BOUNDARY_EVENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_BOUNDARY_REFUSAL}'); END;",
    f"CREATE TRIGGER {BOUNDARY_SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {BOUNDARY_EVENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_BOUNDARY_REFUSAL}'); END;",
)

BOUNDARY_SQLITE_DROP = (
    f"DROP TRIGGER IF EXISTS {BOUNDARY_SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {BOUNDARY_SQLITE_NO_DELETE}",
)

_BOUNDARY_PG_FN = (
    f"CREATE OR REPLACE FUNCTION {BOUNDARY_PG_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION '{BOUNDARY_EVENT_TABLE} is append-only (owner decision 1A): "
    f"% on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

BOUNDARY_PG_DDL = (
    _BOUNDARY_PG_FN,
    f"CREATE TRIGGER {BOUNDARY_PG_NO_UPDATE} "
    f"BEFORE UPDATE ON {BOUNDARY_EVENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {BOUNDARY_PG_FUNCTION}()",
    f"CREATE TRIGGER {BOUNDARY_PG_NO_DELETE} "
    f"BEFORE DELETE ON {BOUNDARY_EVENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {BOUNDARY_PG_FUNCTION}()",
)

BOUNDARY_PG_DROP = (
    f"DROP TRIGGER IF EXISTS {BOUNDARY_PG_NO_UPDATE} ON {BOUNDARY_EVENT_TABLE}",
    f"DROP TRIGGER IF EXISTS {BOUNDARY_PG_NO_DELETE} ON {BOUNDARY_EVENT_TABLE}",
    f"DROP FUNCTION IF EXISTS {BOUNDARY_PG_FUNCTION}()",
)

BOUNDARY_APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": BOUNDARY_SQLITE_DDL,
    "postgresql": BOUNDARY_PG_DDL,
}

BOUNDARY_DROP_DDL_BY_DIALECT = {
    "sqlite": BOUNDARY_SQLITE_DROP,
    "postgresql": BOUNDARY_PG_DROP,
}


def boundary_append_only_statements(dialect_name: str):
    """Append-only statements for ``clock_session_boundary_event``, or () if unsupported."""
    return tuple(BOUNDARY_APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def boundary_drop_statements(dialect_name: str):
    """Teardown for ``clock_session_boundary_event``'s triggers (downgrade only)."""
    return tuple(BOUNDARY_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection -- asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(BOUNDARY_APPEND_ONLY_DDL_BY_DIALECT)

__all__ = [
    "BOUNDARY_EVENT_TABLE",
    "BOUNDARY_APPEND_ONLY_DDL_BY_DIALECT",
    "BOUNDARY_DROP_DDL_BY_DIALECT",
    "boundary_append_only_statements",
    "boundary_drop_statements",
    "SUPPORTED_DIALECTS",
]
