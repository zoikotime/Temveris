"""
Append-only enforcement DDL for the versioned configuration snapshot (Owner Decision 1A).
==============================================================

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

WHY ONLY ``config_value``, AND NOT ``config_version``
------------------------------------------------------
A ConfigVersion is a COMPLETE snapshot of the configuration in force at an
effective date, and it is never appended to: a change captures a NEW version
carrying everything forward. On that reasoning BOTH tables were authorized
under Decision 1A, and for ``config_value`` the reasoning holds -- every
foreign key is RESTRICT, the ORM guard freezes every column that matters, and
no production path writes one after insert.

``config_version`` turned out NOT to be append-only, for two reasons the
audit's AST scan could not see:

* ``note`` is DELIBERATELY writable. The model calls it "administrator prose,
  not configuration": it sits outside the content hash, nothing resolves
  against it, and correcting a typo cannot change what any date resolves to.

* ``captured_by_user_id`` is ``ondelete="SET NULL"``. Deleting a user
  therefore issues a DATABASE-LEVEL UPDATE against this table -- performed by
  the foreign key, not by any statement in the codebase, which is precisely
  why no scan of application code found it. That SET NULL is Ruling M's own
  mechanism for "removing a person must never remove configuration history",
  and an unconditional trigger refuses it, making every user who has ever
  captured configuration permanently undeletable.

It is the only table in the enforced set, or among the six that already carry
append-only triggers, with a SET NULL foreign key. Enforcement for it needs an
owner ruling, not an inference.

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


#: One frozen value inside one version. Never appended to.
VALUE_TABLE = "config_value"

VALUE_SQLITE_NO_UPDATE = "config_value_no_update"
VALUE_SQLITE_NO_DELETE = "config_value_no_delete"
VALUE_PG_NO_UPDATE = "config_value_no_update"
VALUE_PG_NO_DELETE = "config_value_no_delete"
VALUE_PG_FUNCTION = "config_value_append_only"

_VALUE_REFUSAL = (
    "config_value is append-only (owner decision 1A): a value belongs to one immutable version and is changed by capturing a new version, never in place"
)

VALUE_SQLITE_DDL = (
    f"CREATE TRIGGER {VALUE_SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {VALUE_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_VALUE_REFUSAL}'); END;",
    f"CREATE TRIGGER {VALUE_SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {VALUE_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_VALUE_REFUSAL}'); END;",
)

VALUE_SQLITE_DROP = (
    f"DROP TRIGGER IF EXISTS {VALUE_SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {VALUE_SQLITE_NO_DELETE}",
)

_VALUE_PG_FN = (
    f"CREATE OR REPLACE FUNCTION {VALUE_PG_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION '{VALUE_TABLE} is append-only (owner decision 1A): "
    f"% on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

VALUE_PG_DDL = (
    _VALUE_PG_FN,
    f"CREATE TRIGGER {VALUE_PG_NO_UPDATE} "
    f"BEFORE UPDATE ON {VALUE_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {VALUE_PG_FUNCTION}()",
    f"CREATE TRIGGER {VALUE_PG_NO_DELETE} "
    f"BEFORE DELETE ON {VALUE_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {VALUE_PG_FUNCTION}()",
)

VALUE_PG_DROP = (
    f"DROP TRIGGER IF EXISTS {VALUE_PG_NO_UPDATE} ON {VALUE_TABLE}",
    f"DROP TRIGGER IF EXISTS {VALUE_PG_NO_DELETE} ON {VALUE_TABLE}",
    f"DROP FUNCTION IF EXISTS {VALUE_PG_FUNCTION}()",
)

VALUE_APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": VALUE_SQLITE_DDL,
    "postgresql": VALUE_PG_DDL,
}

VALUE_DROP_DDL_BY_DIALECT = {
    "sqlite": VALUE_SQLITE_DROP,
    "postgresql": VALUE_PG_DROP,
}


def value_append_only_statements(dialect_name: str):
    """Append-only statements for ``config_value``, or () if unsupported."""
    return tuple(VALUE_APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def value_drop_statements(dialect_name: str):
    """Teardown for ``config_value``'s triggers (downgrade only)."""
    return tuple(VALUE_DROP_DDL_BY_DIALECT.get(dialect_name, ()))


#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection -- asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(VALUE_APPEND_ONLY_DDL_BY_DIALECT)

__all__ = [
    "VALUE_TABLE",
    "VALUE_APPEND_ONLY_DDL_BY_DIALECT",
    "VALUE_DROP_DDL_BY_DIALECT",
    "value_append_only_statements",
    "value_drop_statements",
    "SUPPORTED_DIALECTS",
]
