"""
Append-only enforcement DDL for the ECO notification history (R5).
==================================================================

ONE definition of the trigger SQL, imported by BOTH emitters, exactly as
``eval_event/append_only_ddl.py`` and ``work_ledger_producer/append_only_ddl.py``
already do:

* ``models.py`` attaches it to each table's ``after_create`` event, so a
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
refuse a statement the ORM never sees. A notification record is
compliance-sensitive history; one that can be quietly rewritten is not evidence.

BOTH TABLES ARE APPEND-ONLY
---------------------------
The notification is immutable by R5. The delivery-attempt table is append-only
for the same reason: an attempt is a historical fact about what was tried, and
a retry appends a NEW attempt rather than editing the last one.
"""

from __future__ import annotations

#: The immutable ECO notification record (R5).
NOTIFICATION_TABLE = "eco_notification"

#: The append-only delivery-attempt history (R5). Deliberately INERT in this
#: increment: the schema exists so the immutable contract is complete, but no
#: application path writes to it and nothing is delivered.
ATTEMPT_TABLE = "eco_notification_delivery_attempt"

SQLITE_NOTIFICATION_NO_UPDATE = "eco_notification_no_update"
SQLITE_NOTIFICATION_NO_DELETE = "eco_notification_no_delete"
SQLITE_ATTEMPT_NO_UPDATE = "eco_notification_attempt_no_update"
SQLITE_ATTEMPT_NO_DELETE = "eco_notification_attempt_no_delete"

POSTGRES_NOTIFICATION_NO_UPDATE = "eco_notification_no_update"
POSTGRES_NOTIFICATION_NO_DELETE = "eco_notification_no_delete"
POSTGRES_ATTEMPT_NO_UPDATE = "eco_notification_attempt_no_update"
POSTGRES_ATTEMPT_NO_DELETE = "eco_notification_attempt_no_delete"

POSTGRES_GUARD_FUNCTION = "eco_notification_append_only"

_NOTIFICATION_REFUSAL = (
    "eco_notification is append-only (owner ruling R5): a recorded ECO "
    "notification is never rewritten and never removed"
)
_ATTEMPT_REFUSAL = (
    "eco_notification_delivery_attempt is append-only (owner ruling R5): a "
    "delivery attempt is a historical fact; a retry appends a new attempt"
)

# ── SQLite ──────────────────────────────────────────────────────────────────
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NOTIFICATION_NO_UPDATE} "
    f"BEFORE UPDATE ON {NOTIFICATION_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_NOTIFICATION_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NOTIFICATION_NO_DELETE} "
    f"BEFORE DELETE ON {NOTIFICATION_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_NOTIFICATION_REFUSAL}'); END;",
)

SQLITE_ATTEMPT_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_ATTEMPT_NO_UPDATE} "
    f"BEFORE UPDATE ON {ATTEMPT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_ATTEMPT_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_ATTEMPT_NO_DELETE} "
    f"BEFORE DELETE ON {ATTEMPT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_ATTEMPT_REFUSAL}'); END;",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NOTIFICATION_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NOTIFICATION_NO_DELETE}",
)
SQLITE_ATTEMPT_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_ATTEMPT_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_ATTEMPT_NO_DELETE}",
)

# ── PostgreSQL ──────────────────────────────────────────────────────────────
# One guard function serves both tables: it raises unconditionally and names
# the operation and the relation the server itself reports.
_PG_FUNCTION = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION 'ECO notification history is append-only "
    f"(owner ruling R5): % on % refused', TG_OP, TG_TABLE_NAME; END; "
    f"$$ LANGUAGE plpgsql"
)

POSTGRES_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_NOTIFICATION_NO_UPDATE} "
    f"BEFORE UPDATE ON {NOTIFICATION_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NOTIFICATION_NO_DELETE} "
    f"BEFORE DELETE ON {NOTIFICATION_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
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

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NOTIFICATION_NO_UPDATE} "
    f"ON {NOTIFICATION_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NOTIFICATION_NO_DELETE} "
    f"ON {NOTIFICATION_TABLE}",
)
POSTGRES_ATTEMPT_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_ATTEMPT_NO_UPDATE} ON {ATTEMPT_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_ATTEMPT_NO_DELETE} ON {ATTEMPT_TABLE}",
)

#: The shared guard function, dropped SEPARATELY and LAST.
#:
#: One function serves both tables, so it cannot be dropped with either one:
#: PostgreSQL refuses with ``DependentObjectsStillExist`` while any trigger
#: still references it. SQLite has no function to drop at all, which is why
#: only a PostgreSQL run surfaces this — and one did.
POSTGRES_GUARD_DROP_DDL = (
    f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()",
)
SQLITE_GUARD_DROP_DDL: tuple = ()

APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_APPEND_ONLY_DDL,
}
ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SQLITE_ATTEMPT_APPEND_ONLY_DDL,
    "postgresql": POSTGRES_ATTEMPT_APPEND_ONLY_DDL,
}
DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_DROP_DDL,
    "postgresql": POSTGRES_DROP_DDL,
}
ATTEMPT_DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_ATTEMPT_DROP_DDL,
    "postgresql": POSTGRES_ATTEMPT_DROP_DDL,
}
GUARD_DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_GUARD_DROP_DDL,
    "postgresql": POSTGRES_GUARD_DROP_DDL,
}

#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection, so the notification store must never be run
#: against one — asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """Append-only statements for the NOTIFICATION table, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def attempt_append_only_statements(dialect_name: str):
    """Append-only statements for the ATTEMPT table, or () if unsupported."""
    return tuple(ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """Teardown for the NOTIFICATION table (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


def attempt_drop_statements(dialect_name: str):
    """Teardown for the ATTEMPT table's triggers (downgrade only)."""
    return tuple(ATTEMPT_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


def guard_drop_statements(dialect_name: str):
    """Drop the SHARED guard function. Must run AFTER both tables are gone.

    PostgreSQL refuses ``DROP FUNCTION`` while any trigger still depends on
    it, and one function serves both tables — so it belongs to neither
    table's teardown.
    """
    return tuple(GUARD_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "NOTIFICATION_TABLE",
    "ATTEMPT_TABLE",
    "SQLITE_APPEND_ONLY_DDL",
    "SQLITE_ATTEMPT_APPEND_ONLY_DDL",
    "POSTGRES_APPEND_ONLY_DDL",
    "POSTGRES_ATTEMPT_APPEND_ONLY_DDL",
    "APPEND_ONLY_DDL_BY_DIALECT",
    "ATTEMPT_APPEND_ONLY_DDL_BY_DIALECT",
    "DROP_DDL_BY_DIALECT",
    "ATTEMPT_DROP_DDL_BY_DIALECT",
    "GUARD_DROP_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "POSTGRES_GUARD_FUNCTION",
    "append_only_statements",
    "attempt_append_only_statements",
    "drop_statements",
    "attempt_drop_statements",
    "guard_drop_statements",
]
