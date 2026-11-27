"""
Append-only enforcement DDL for the immutable leave input snapshot (Owner Decision 1A).
====================================================================

ONE definition of the trigger SQL, imported by BOTH emitters -- exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py``,
``policy_version/append_only_ddl.py`` and
``work_ledger_producer/append_only_ddl.py`` already do:

* ``leave_snapshot.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits the SAME statements with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it. Without the explicit emission a migrated
  database would lack the triggers while a test database had them, and every
  below-ORM test would pass vacuously against production's real shape.

WHY THIS TABLE
--------------
Owner Ruling 2: the leave input an evaluation used is frozen into a
content-addressed snapshot so a replay reads the same evidence the
original evaluation did. Its ORM guard already refuses every UPDATE and
every DELETE -- there is no authorized mutation at all -- which is what
makes the unconditional trigger below exactly right for it.

``leave_input_contribution``, its child rows, carries the same ORM
guard and is NOT covered here: Decision 1A named five tables and this
package holds one of them. Extending to the child is a separate
authorization, not an inference.

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


#: The immutable leave input one evaluation used (Ruling 2).
SNAPSHOT_TABLE = "leave_input_snapshot"

SNAPSHOT_SQLITE_NO_UPDATE = "leave_input_snapshot_no_update"
SNAPSHOT_SQLITE_NO_DELETE = "leave_input_snapshot_no_delete"
SNAPSHOT_PG_NO_UPDATE = "leave_input_snapshot_no_update"
SNAPSHOT_PG_NO_DELETE = "leave_input_snapshot_no_delete"
SNAPSHOT_PG_FUNCTION = "leave_input_snapshot_append_only"

_SNAPSHOT_REFUSAL = (
    "leave_input_snapshot is append-only (owner decision 1A): the leave input an evaluation used is frozen so a replay reads the same evidence"
)

SNAPSHOT_SQLITE_DDL = (
    f"CREATE TRIGGER {SNAPSHOT_SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {SNAPSHOT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_SNAPSHOT_REFUSAL}'); END;",
    f"CREATE TRIGGER {SNAPSHOT_SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {SNAPSHOT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_SNAPSHOT_REFUSAL}'); END;",
)

SNAPSHOT_SQLITE_DROP = (
    f"DROP TRIGGER IF EXISTS {SNAPSHOT_SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SNAPSHOT_SQLITE_NO_DELETE}",
)

_SNAPSHOT_PG_FN = (
    f"CREATE OR REPLACE FUNCTION {SNAPSHOT_PG_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION '{SNAPSHOT_TABLE} is append-only (owner decision 1A): "
    f"% on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

SNAPSHOT_PG_DDL = (
    _SNAPSHOT_PG_FN,
    f"CREATE TRIGGER {SNAPSHOT_PG_NO_UPDATE} "
    f"BEFORE UPDATE ON {SNAPSHOT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_PG_FUNCTION}()",
    f"CREATE TRIGGER {SNAPSHOT_PG_NO_DELETE} "
    f"BEFORE DELETE ON {SNAPSHOT_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_PG_FUNCTION}()",
)

SNAPSHOT_PG_DROP = (
    f"DROP TRIGGER IF EXISTS {SNAPSHOT_PG_NO_UPDATE} ON {SNAPSHOT_TABLE}",
    f"DROP TRIGGER IF EXISTS {SNAPSHOT_PG_NO_DELETE} ON {SNAPSHOT_TABLE}",
    f"DROP FUNCTION IF EXISTS {SNAPSHOT_PG_FUNCTION}()",
)

SNAPSHOT_APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": SNAPSHOT_SQLITE_DDL,
    "postgresql": SNAPSHOT_PG_DDL,
}

SNAPSHOT_DROP_DDL_BY_DIALECT = {
    "sqlite": SNAPSHOT_SQLITE_DROP,
    "postgresql": SNAPSHOT_PG_DROP,
}


def snapshot_append_only_statements(dialect_name: str):
    """Append-only statements for ``leave_input_snapshot``, or () if unsupported."""
    return tuple(SNAPSHOT_APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def snapshot_drop_statements(dialect_name: str):
    """Teardown for ``leave_input_snapshot``'s triggers (downgrade only)."""
    return tuple(SNAPSHOT_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection -- asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(SNAPSHOT_APPEND_ONLY_DDL_BY_DIALECT)

__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_APPEND_ONLY_DDL_BY_DIALECT",
    "SNAPSHOT_DROP_DDL_BY_DIALECT",
    "snapshot_append_only_statements",
    "snapshot_drop_statements",
    "SUPPORTED_DIALECTS",
]
