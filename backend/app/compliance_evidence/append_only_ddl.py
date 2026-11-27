"""
Append-only enforcement DDL for ECO compliance evidence (D2, D3, D4).
=====================================================================

ONE definition of the trigger SQL, imported by BOTH emitters -- as the eleven
existing ``append_only_ddl.py`` modules already do.

WHY ALL THREE
-------------
Each of D2, D3 and D4 says the same thing in its own words: append-only,
corrections supersede, no UPDATE-in-place of substantive evidence. Compliance
evidence that could be edited after the fact is not evidence -- it is a current
opinion wearing a timestamp.

NO APOSTROPHES IN THE REFUSAL TEXT
----------------------------------
These strings are embedded in a single-quoted SQLite ``RAISE(ABORT, ...)``
literal. An apostrophe terminates the literal and the CREATE TRIGGER fails at
schema-creation time. A test asserts it, because the failure mode is a broken
schema rather than a wrong answer.

NO IMPORTS
----------
Imported by an Alembic revision, so it holds string constants and nothing else.

DIALECT SUPPORT
---------------
PostgreSQL (production) and SQLite (the in-process test database). SQLite is
verified at runtime by the test suite; PostgreSQL by offline compilation and by
asserting presence in the emitted DDL, because no PostgreSQL database is
reachable here and connecting to one is prohibited.
"""

from __future__ import annotations

DPIA_TABLE = "eco_dpia_evidence"
CONSULTATION_TABLE = "eco_consultation_attestation"
WORKER_NOTICE_TABLE = "eco_worker_notice_confirmation"

_REFUSALS = {
    DPIA_TABLE: (
        "eco_dpia_evidence is append-only (owner ruling D2): a correction is "
        "a NEW record superseding the prior one, never an edit"
    ),
    CONSULTATION_TABLE: (
        "eco_consultation_attestation is append-only (owner ruling D3): "
        "Temveris records the attestation and never rewrites it"
    ),
    WORKER_NOTICE_TABLE: (
        "eco_worker_notice_confirmation is append-only (owner ruling D4): a "
        "later confirmation supersedes the earlier one, never replaces it in "
        "place"
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
        f"BEGIN RAISE EXCEPTION '{table} is append-only (ECO compliance "
        f"evidence): % on % refused', TG_OP, TG_TABLE_NAME; "
        f"END; $$ LANGUAGE plpgsql",
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    )


APPEND_ONLY_DDL_BY_DIALECT = {
    "sqlite": {t: _sqlite(t) for t in TABLES},
    "postgresql": {t: _postgres(t) for t in TABLES},
}

DROP_DDL_BY_DIALECT = {
    "sqlite": {t: (f"DROP TRIGGER IF EXISTS {t}_no_update",
                   f"DROP TRIGGER IF EXISTS {t}_no_delete") for t in TABLES},
    "postgresql": {t: (f"DROP TRIGGER IF EXISTS {t}_no_update ON {t}",
                       f"DROP TRIGGER IF EXISTS {t}_no_delete ON {t}",
                       f"DROP FUNCTION IF EXISTS {t}_append_only()")
                   for t in TABLES},
}

#: The dialects this repository supports. An absent dialect gets NO protection
#: rather than partial protection -- asserted by test.
SUPPORTED_DIALECTS = frozenset(APPEND_ONLY_DDL_BY_DIALECT)


def append_only_statements(dialect_name: str, table: str):
    """Append-only statements for one table, or () if the dialect is unknown."""
    return tuple(APPEND_ONLY_DDL_BY_DIALECT.get(dialect_name, {}).get(table, ()))


def drop_statements(dialect_name: str, table: str):
    """Teardown for one table (downgrade only)."""
    return tuple(DROP_DDL_BY_DIALECT.get(dialect_name, {}).get(table, ()))


def all_append_only_statements(dialect_name: str):
    return tuple(s for t in TABLES
                 for s in append_only_statements(dialect_name, t))


def all_drop_statements(dialect_name: str):
    return tuple(s for t in reversed(TABLES)
                 for s in drop_statements(dialect_name, t))


__all__ = [
    "APPEND_ONLY_DDL_BY_DIALECT",
    "CONSULTATION_TABLE",
    "DPIA_TABLE",
    "DROP_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "TABLES",
    "WORKER_NOTICE_TABLE",
    "all_append_only_statements",
    "all_drop_statements",
    "append_only_statements",
    "drop_statements",
]
