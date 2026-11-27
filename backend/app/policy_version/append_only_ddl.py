"""
Append-only enforcement DDL for the §11 Policy artifact.
=========================================================

ONE definition of the trigger SQL, imported by BOTH emitters, exactly as
``alert/append_only_ddl.py``, ``eval_event/append_only_ddl.py`` and
``work_ledger_producer/append_only_ddl.py`` already do:

* ``models.py`` attaches it to the table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits the SAME statements with ``op.execute``, because
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
ORM listeners are bypassed by SQLAlchemy Core UPDATE, ORM bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees. A policy version is the evidence for
what rule governed a past day; one that can be quietly rewritten would make
every historical evaluation unreproducible, which is the §17/INV-8 failure the
artifact exists to prevent.
"""

from __future__ import annotations

POLICY_TABLE = "policy_version"

SQLITE_NO_UPDATE = "policy_version_no_update"
SQLITE_NO_DELETE = "policy_version_no_delete"

POSTGRES_NO_UPDATE = "policy_version_no_update"
POSTGRES_NO_DELETE = "policy_version_no_delete"

POSTGRES_GUARD_FUNCTION = "policy_version_append_only"

_REFUSAL = (
    "policy_version is append-only: a captured policy is never rewritten and "
    "never removed — capture a LATER version instead, so the rule that "
    "governed any past day stays reproducible"
)

# ── SQLite ──────────────────────────────────────────────────────────────────
SQLITE_APPEND_ONLY_DDL = (
    f"CREATE TRIGGER {SQLITE_NO_UPDATE} "
    f"BEFORE UPDATE ON {POLICY_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
    f"CREATE TRIGGER {SQLITE_NO_DELETE} "
    f"BEFORE DELETE ON {POLICY_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_REFUSAL}'); END;",
)

SQLITE_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_UPDATE}",
    f"DROP TRIGGER IF EXISTS {SQLITE_NO_DELETE}",
)

SQLITE_GUARD_DROP_DDL: tuple = ()

# ── PostgreSQL ──────────────────────────────────────────────────────────────
_PG_FUNCTION = (
    f"CREATE OR REPLACE FUNCTION {POSTGRES_GUARD_FUNCTION}() "
    f"RETURNS trigger AS $$ "
    f"BEGIN RAISE EXCEPTION 'policy_version is append-only: % on % refused', "
    f"TG_OP, TG_TABLE_NAME; END; "
    f"$$ LANGUAGE plpgsql"
)

POSTGRES_APPEND_ONLY_DDL = (
    _PG_FUNCTION,
    f"CREATE TRIGGER {POSTGRES_NO_UPDATE} "
    f"BEFORE UPDATE ON {POLICY_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
    f"CREATE TRIGGER {POSTGRES_NO_DELETE} "
    f"BEFORE DELETE ON {POLICY_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}()",
)

POSTGRES_DROP_DDL = (
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_UPDATE} ON {POLICY_TABLE}",
    f"DROP TRIGGER IF EXISTS {POSTGRES_NO_DELETE} ON {POLICY_TABLE}",
)

#: Dropped SEPARATELY and LAST — PostgreSQL refuses with
#: ``DependentObjectsStillExist`` while any trigger still references it.
POSTGRES_GUARD_DROP_DDL = (
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

GUARD_DROP_DDL_BY_DIALECT = {
    "sqlite": SQLITE_GUARD_DROP_DDL,
    "postgresql": POSTGRES_GUARD_DROP_DDL,
}


def append_only_statements(dialect: str) -> tuple:
    """The statements for one dialect, or none for an unknown engine.

    An unrecognised dialect yields NOTHING rather than raising: a database this
    module has no guard for must not silently appear guarded.
    """
    return APPEND_ONLY_DDL_BY_DIALECT.get(dialect, ())


def drop_statements(dialect: str) -> tuple:
    return DROP_DDL_BY_DIALECT.get(dialect, ())


def guard_drop_statements(dialect: str) -> tuple:
    return GUARD_DROP_DDL_BY_DIALECT.get(dialect, ())
