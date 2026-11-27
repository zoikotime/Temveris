"""
Append-only enforcement DDL for the governed jurisdiction mapping.
==================================================================

Owner ruling B (effective-dated/versioned mapping) and B8 (corrections
supersede; historical assignments are not mutated).

ONE definition of the trigger SQL, imported by BOTH emitters -- exactly as the
ten existing ``append_only_ddl.py`` modules already do:

* ``models.py`` attaches it to each table's ``after_create`` event, so a
  ``Base.metadata.create_all()`` schema carries it;
* the Alembic revision emits the SAME statements with ``op.execute``, because
  ``op.create_table`` builds its own ``Table`` object and the ORM-table
  listener does NOT fire for it.

WHY ALL THREE TABLES
--------------------
B7 requires a historical assessment to resolve the mapping in force on the
relevant date. A classification or an assignment that could be edited in place
would make every past assessment unreproducible -- the same reason
``config_version`` and ``policy_version`` are versioned rather than mutable.
B8 says it outright: "Corrections supersede; historical assignments are not
mutated."

The child country rows are covered too. An assignment's meaning IS its country
set, so a mutable child table would leave the parent immutable and its content
editable, which is immutability in name only. It is also what makes B9 work:
an assignment with zero children is a DECLARATION of emptiness, and a declared
empty set that could later gain a row would not be a declaration at all.

NO IMPORTS
----------
Imported by an Alembic revision, so it holds string constants and nothing else.

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

#: The platform-governed, effective-dated country classification.
CLASSIFICATION_TABLE = "jurisdiction_classification"

#: One Tenant's applicable jurisdiction set, as of an effective date.
ASSIGNMENT_TABLE = "tenant_jurisdiction_assignment"

#: The countries in one assignment. ZERO rows is a declared empty set (B9).
ASSIGNMENT_COUNTRY_TABLE = "tenant_jurisdiction_assignment_country"

#: NO APOSTROPHES. These strings are embedded in a single-quoted SQLite
#: RAISE(ABORT, ...) literal, so an apostrophe terminates the literal and the
#: CREATE TRIGGER fails at schema-creation time. A test below asserts it.
_REFUSALS = {
    CLASSIFICATION_TABLE: (
        "jurisdiction_classification is append-only (owner ruling B): the "
        "legal classification of a country is effective-dated, and a change "
        "is a NEW record superseding the prior one"
    ),
    ASSIGNMENT_TABLE: (
        "tenant_jurisdiction_assignment is append-only (owner ruling B8): "
        "corrections supersede, and historical assignments are never mutated"
    ),
    ASSIGNMENT_COUNTRY_TABLE: (
        "tenant_jurisdiction_assignment_country is append-only (owner ruling "
        "B8): the country set of an assignment is fixed when it is recorded, "
        "because a declared empty set that could later gain a row would not "
        "be a declaration"
    ),
}

TABLES = tuple(_REFUSALS)


def _sqlite(table: str):
    refusal = _REFUSALS[table]
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{refusal}'); END;",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{refusal}'); END;",
    )


def _postgres(table: str):
    function = f"{table}_append_only"
    return (
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$ "
        f"BEGIN RAISE EXCEPTION '{table} is append-only (owner rulings B / "
        f"B8): % on % refused', TG_OP, TG_TABLE_NAME; "
        f"END; $$ LANGUAGE plpgsql",
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    )


def _sqlite_drop(table: str):
    return (f"DROP TRIGGER IF EXISTS {table}_no_update",
            f"DROP TRIGGER IF EXISTS {table}_no_delete")


def _postgres_drop(table: str):
    return (f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}",
            f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}",
            f"DROP FUNCTION IF EXISTS {table}_append_only()")


APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": {t: _sqlite(t) for t in TABLES},
    "postgresql": {t: _postgres(t) for t in TABLES},
}

DROP_DDL_BY_DIALECT = {
    "sqlite": {t: _sqlite_drop(t) for t in TABLES},
    "postgresql": {t: _postgres_drop(t) for t in TABLES},
}

#: The dialects this repository supports. A dialect absent from this set gets
#: NO database-level protection -- asserted by test rather than assumed.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str, table: str):
    """Append-only statements for one table, or () if the dialect is unknown."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, {}).get(table, ()))


def drop_statements(dialect_name: str, table: str):
    """Teardown for one table's triggers (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, {}).get(table, ()))


def all_append_only_statements(dialect_name: str):
    """Every table's statements, in creation order."""
    return tuple(s for t in TABLES
                 for s in append_only_statements(dialect_name, t))


def all_drop_statements(dialect_name: str):
    """Every table's teardown, in reverse creation order."""
    return tuple(s for t in reversed(TABLES)
                 for s in drop_statements(dialect_name, t))


__all__ = [
    "APPEND_ONLY_DDL_BY_DIALECT",
    "ASSIGNMENT_COUNTRY_TABLE",
    "ASSIGNMENT_TABLE",
    "CLASSIFICATION_TABLE",
    "DROP_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "TABLES",
    "all_append_only_statements",
    "all_drop_statements",
    "append_only_statements",
    "drop_statements",
]
