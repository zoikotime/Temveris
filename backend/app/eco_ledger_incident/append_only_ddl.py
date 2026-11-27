"""
Append-only enforcement DDL for the ECO platform-incident store (R5).
=====================================================================

ONE definition of the trigger SQL, imported by BOTH emitters — exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py``,
``eco_day_alert/append_only_ddl.py`` and
``work_ledger_producer/append_only_ddl.py`` already do:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits it with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it.

Without the explicit emission a migrated database would lack the triggers while
a test database had them, and every below-ORM test would pass vacuously against
production's real shape.

NO IMPORTS
----------
Imported by an Alembic revision, so it holds string constants and nothing else.
A migration must not drag application models, metadata or a session into its
own runtime.

WHY TRIGGERS AND NOT LISTENERS
------------------------------
Ruling M and D18-F1: ORM listeners are bypassed by Core UPDATE, bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees. An incident is the evidence that a
Tenant's ledger holds a defect; one that can be quietly rewritten or removed is
not evidence, and a defect that can be erased is a defect that can be denied.
"""

from __future__ import annotations

#: The immutable ECO platform-incident store (R5).
INCIDENT_TABLE = "eco_ledger_incident"

SQLITE_NO_UPDATE = "eco_ledger_incident_no_update"
SQLITE_NO_DELETE = "eco_ledger_incident_no_delete"

POSTGRES_NO_UPDATE = "eco_ledger_incident_no_update"
POSTGRES_NO_DELETE = "eco_ledger_incident_no_delete"
POSTGRES_GUARD_FUNCTION = "eco_ledger_incident_append_only"

_REFUSAL = (
    "eco_ledger_incident is append-only (owner ruling R5): a raised platform "
    "incident is a historical fact and is never rewritten or removed"
)

# -- SQLite -----------------------------------------------------------------
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {INCIDENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {INCIDENT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_DELETE}",
)

# -- PostgreSQL -------------------------------------------------------------
_PG_FUNCTION = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION 'ECO platform incident history is append-only "
    f"(owner ruling R5): % on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

POSTGRES_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE} "
    f"BEFORE UPDATE ON {INCIDENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE} "
    f"BEFORE DELETE ON {INCIDENT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE} ON {INCIDENT_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE} ON {INCIDENT_TABLE}",
    f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()",
)

APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_APPEND_ONLY_DDL,
}

DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_DROP_DDL,
    "postgresql": POSTGRES_DROP_DDL,
}

#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection, so the incident store must never be run
#: against one -- asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """Append-only statements for the incident table, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """Teardown for the incident table's triggers (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "APPEND_ONLY_DDL_BY_DIALECT",
    "DROP_DDL_BY_DIALECT",
    "INCIDENT_TABLE",
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "drop_statements",
]
