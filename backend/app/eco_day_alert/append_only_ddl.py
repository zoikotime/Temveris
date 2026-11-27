"""
Append-only enforcement DDL for the day-scoped administrator alert (R5).
========================================================================

ONE definition of the trigger SQL, imported by BOTH emitters — exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py`` and
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
refuse a statement the ORM never sees. An administrator alert is a record of
what a Tenant was told about its own configuration; one that can be quietly
rewritten is not evidence.
"""

from __future__ import annotations

#: The immutable day-scoped administrator alert (R5).
DAY_ALERT_TABLE = "eco_day_administrator_alert"

#: The append-only delivery history for those alerts (R5).
ATTEMPT_TABLE = "eco_day_alert_delivery_attempt"

SQLITE_NO_UPDATE = "eco_day_administrator_alert_no_update"
SQLITE_NO_DELETE = "eco_day_administrator_alert_no_delete"

SQLITE_ATTEMPT_NO_UPDATE = "eco_day_alert_attempt_no_update"
SQLITE_ATTEMPT_NO_DELETE = "eco_day_alert_attempt_no_delete"

POSTGRES_ATTEMPT_NO_UPDATE = "eco_day_alert_attempt_no_update"
POSTGRES_ATTEMPT_NO_DELETE = "eco_day_alert_attempt_no_delete"

POSTGRES_NO_UPDATE = "eco_day_administrator_alert_no_update"
POSTGRES_NO_DELETE = "eco_day_administrator_alert_no_delete"
POSTGRES_GUARD_FUNCTION = "eco_day_administrator_alert_append_only"

_REFUSAL = (
    "eco_day_administrator_alert is append-only (owner ruling R5): a raised "
    "administrator alert is a historical fact and is never rewritten or removed"
)

# ── SQLite ──────────────────────────────────────────────────────────────────
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {DAY_ALERT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {DAY_ALERT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
)

_ATTEMPT_REFUSAL = (
    "eco_day_alert_delivery_attempt is append-only (owner ruling R5): a "
    "delivery attempt is a historical fact; a retry appends a new attempt"
)

SQLITE_ATTEMPT_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_ATTEMPT_NO_UPDATE} "
    f"BEFORE UPDATE ON {ATTEMPT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_ATTEMPT_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_ATTEMPT_NO_DELETE} "
    f"BEFORE DELETE ON {ATTEMPT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_ATTEMPT_REFUSAL}'); END;",
)

SQLITE_ATTEMPT_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_ATTEMPT_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_ATTEMPT_NO_DELETE}",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_DELETE}",
)

# ── PostgreSQL ──────────────────────────────────────────────────────────────
_PG_FUNCTION = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION 'ECO day administrator alert history is "
    f"append-only (owner ruling R5): % on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

POSTGRES_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE} "
    f"BEFORE UPDATE ON {DAY_ALERT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE} "
    f"BEFORE DELETE ON {DAY_ALERT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE} ON {DAY_ALERT_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE} ON {DAY_ALERT_TABLE}",
    f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_ATTEMPT_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_ATTEMPT_NO_UPDATE} "
    f"BEFORE UPDATE ON {ATTEMPT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_ATTEMPT_NO_DELETE} "
    f"BEFORE DELETE ON {ATTEMPT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_ATTEMPT_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_ATTEMPT_NO_UPDATE} ON {ATTEMPT_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_ATTEMPT_NO_DELETE} ON {ATTEMPT_TABLE}",
)

ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_ATTEMPT_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_ATTEMPT_APPEND_ONLY_DDL,
}

ATTEMPT_DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_ATTEMPT_DROP_DDL,
    "postgresql": POSTGRES_ATTEMPT_DROP_DDL,
}

APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_APPEND_ONLY_DDL,
}

DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_DROP_DDL,
    "postgresql": POSTGRES_DROP_DDL,
}

#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection, so the alert store must never be run against
#: one — asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """Append-only statements for the day-alert table, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def attempt_append_only_statements(dialect_name: str):
    """Append-only statements for the ATTEMPT table, or () if unsupported."""
    return tuple(ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """Teardown for the day-alert table's triggers (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


def attempt_drop_statements(dialect_name: str):
    """Teardown for the ATTEMPT table's triggers (downgrade only)."""
    return tuple(ATTEMPT_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "APPEND_ONLY_DDL_BY_DIALECT",
    "ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT",
    "ATTEMPT_TABLE",
    "DAY_ALERT_TABLE",
    "DROP_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "attempt_append_only_statements",
    "attempt_drop_statements",
    "drop_statements",
]
