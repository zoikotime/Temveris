"""
Append-only enforcement DDL for the sealed interval set (Owner Decision 1A).
============================================================================

ONE definition of the trigger SQL, imported by BOTH emitters — exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py``,
``policy_version/append_only_ddl.py``, ``eco_day_alert/append_only_ddl.py``,
``eco_ledger_incident/append_only_ddl.py`` and
``work_ledger_producer/append_only_ddl.py`` already do:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits the SAME statements with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it. Without the explicit emission a migrated
  database would lack the triggers while a test database had them, and every
  below-ORM test would pass vacuously against production's real shape.

WHY ONLY ``work_state_interval``, AND NOT ``work_day_ledger``
--------------------------------------------------------------
The two tables in this package have DIFFERENT lifecycles, and the append-only
readiness audit established the difference rather than assuming it.

``work_state_interval`` has NO authorized UPDATE. An interval is written once,
with the ledger version it belongs to, and a changed measurement is a new
version of the day — never a rewritten row. So the unconditional trigger below
is exactly right for it.

``work_day_ledger`` has ONE: ``current.superseded_by = new_row.id`` in
``service.py::supersede_day``, which is how §23 / INV-3 links a correction
chain. The successor has no id until it is inserted, so the predecessor's
forward pointer NECESSARILY has to be an UPDATE. An unconditional trigger here
would refuse it and break supersession — the mechanism the specification
requires. The owner therefore declined database triggers for that table
(Decision 2C) and authorized strengthening its ORM frozen-field guard instead.
That guard lives in ``models.py``; this module deliberately says nothing about
it.

NO IMPORTS
----------
Imported by an Alembic revision, so it holds string constants and nothing else.
A migration must not drag application models, metadata or a session into its
own runtime.

WHY TRIGGERS AND NOT LISTENERS
------------------------------
Ruling M and D18-F1: ORM listeners are bypassed by Core UPDATE, bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees. The readiness audit demonstrated this
empirically against this very table's sibling.

DIALECT SUPPORT
---------------
Two dialects, as everywhere else in this repository: PostgreSQL (production)
and SQLite (the in-process test database). The SQLite statements are verified
at runtime by the test suite; the PostgreSQL statements are verified by offline
compilation and by asserting their presence in the emitted DDL, because no
PostgreSQL database is reachable in this environment and connecting to one is
prohibited. That limitation is stated rather than papered over.
"""

from __future__ import annotations

#: The immutable sealed interval set.
INTERVAL_TABLE = "work_state_interval"

SQLITE_NO_UPDATE = "work_state_interval_no_update"
SQLITE_NO_DELETE = "work_state_interval_no_delete"

POSTGRES_NO_UPDATE = "work_state_interval_no_update"
POSTGRES_NO_DELETE = "work_state_interval_no_delete"
POSTGRES_GUARD_FUNCTION = "work_state_interval_append_only"

_REFUSAL = (
    "work_state_interval is append-only (owner decision 1A): a sealed interval "
    "is evidence of what happened, and a correction is a NEW ledger version, "
    "never a rewritten row"
)

# -- SQLite -----------------------------------------------------------------
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {INTERVAL_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {INTERVAL_TABLE} "
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
    f"BEGIN RAISE EXCEPTION 'work_state_interval is append-only (owner "
    f"decision 1A): % on % refused', TG_OP, TG_TABLE_NAME; "
    f"END; $$ LANGUAGE plpgsql"
)

POSTGRES_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE} "
    f"BEFORE UPDATE ON {INTERVAL_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE} "
    f"BEFORE DELETE ON {INTERVAL_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE} ON {INTERVAL_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE} ON {INTERVAL_TABLE}",
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
#: NO database-level protection, so the ledger must never be run against one --
#: asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str):
    """Append-only statements for the interval table, or () if unsupported."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, ()))


def drop_statements(dialect_name: str):
    """Teardown for the interval table's triggers (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, ()))


__all__ = [
    "APPEND_ONLY_DDL_BY_DIALECT",
    "DROP_DDL_BY_DIALECT",
    "INTERVAL_TABLE",
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "drop_statements",
]
