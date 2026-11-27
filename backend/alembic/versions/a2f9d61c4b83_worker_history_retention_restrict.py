"""worker history retention: user_id ON DELETE RESTRICT (Owner Ruling M)

Revision ID: a2f9d61c4b83
Revises: d3a1c8b57f92
Create Date: 2026-09-08 00:00:00.000000

Ruling M says historical records and snapshots must NEVER be deleted or
overwritten. Five tables held ``user_id -> users.id ON DELETE CASCADE``, which
contradicted that at the database level:

    work_day_ledger
    eco_day_record
    leave_input_snapshot
    leave_interval_set
    worker_context_timeline

WHY THE ORM LISTENERS WERE NOT ENOUGH
-------------------------------------
Four of these models refuse deletion with a ``before_delete`` listener, and
``work_day_ledger`` never had one. None of that matters to a cascade: the
existing ``DELETE /employees/{emp_id}`` removes the ``users`` row with
``Query.delete(synchronize_session=False)`` — a Core bulk delete — so no mapper
event fires and the database performs the cascade silently. The invariant has
to live where the deletion actually happens.

WHAT THE OLD SCHEMA ACTUALLY DID
--------------------------------
Not one thing, three, depending on the Worker's data:

* a Worker with a leave-bearing snapshot already FAILED to delete, blocked by
  ``leave_input_contribution.snapshot_id`` RESTRICT stopping the cascaded
  snapshot delete — an opaque IntegrityError;
* a Worker with only empty snapshots deleted CLEANLY, erasing the ledger, its
  intervals (via the ledger's internal cascade), the day records, the worker
  context and those snapshots;
* a Worker with any leave interval set failed even earlier, on the endpoint's
  own pre-delete of ``leave_requests`` against ``leave_interval_set`` RESTRICT.

So the schema was inconsistent with Ruling M and with itself. RESTRICT on all
five makes the answer one thing: refused.

PRECEDENT
---------
``config_version.captured_by_user_id`` is already ``SET NULL``, commented
"removing a person must never remove configuration history". Ruling M is the
same principle applied to Worker history. SET NULL is not available here:
``user_id`` is NOT NULL on all five tables, and nulling it would itself be a
rewrite of an immutable row.

SCOPE
-----
``user_id`` only. Deliberately untouched: ``recorded_by_user_id`` on
``worker_context_timeline`` (SET NULL — an actor reference, not the subject),
every ``organization_id`` (already RESTRICT), the internal
``work_state_interval.ledger_id`` cascade (unreachable once the Worker cannot
be deleted), and ``config_value.scope_user_id``, which is CASCADE and reported
separately for an explicit owner ruling rather than changed here.

CONSTRAINT NAMES
----------------
All five foreign keys were created UNNAMED, so PostgreSQL assigned
``<table>_user_id_fkey``. This revision does not assume that: it reads the
actual constraint name from ``information_schema`` for each table and drops
what it finds, so a differently-named constraint is handled rather than
missed. Idempotent — re-applying finds RESTRICT already in place and does
nothing.

SQLITE
------
SQLite cannot alter a foreign key, and rebuilding these tables would drop the
expression-based ``uq_eco_day_record_snapshot`` index it cannot reflect. A
SQLite database is only ever built by ``create_all``, which takes the FK from
the model — where this ruling is also declared. So this revision is a no-op
there, exactly like the CHECK constraints added in ``d3a1c8b57f92``.

PURELY ADDITIVE
---------------
No table, column or index is created or dropped. No row is read, written,
rewritten or backfilled. Only five foreign-key constraints change their delete
action.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so
no command executing ``env.py`` was run. Verified offline against private
throwaway SQLite databases only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a2f9d61c4b83'
down_revision: Union[str, Sequence[str], None] = 'd3a1c8b57f92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The Worker-history boundary Ruling M names. Order is irrelevant — each is an
#: independent constraint replacement — but it is kept stable for readability.
HISTORY_TABLES = (
    "work_day_ledger",
    "eco_day_record",
    "leave_input_snapshot",
    "leave_interval_set",
    "worker_context_timeline",
)

COLUMN = "user_id"


def _user_fk_names(table: str) -> list:
    """The actual FK constraint names on ``table.user_id`` -> ``users``.

    Read from the catalogue rather than assumed: the constraints were created
    unnamed, and guessing ``<table>_user_id_fkey`` would silently skip a
    database where the name differs.
    """
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT tc.constraint_name
              FROM information_schema.table_constraints AS tc
              JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
               AND tc.table_schema = kcu.table_schema
              JOIN information_schema.constraint_column_usage AS ccu
                ON tc.constraint_name = ccu.constraint_name
               AND tc.table_schema = ccu.table_schema
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND tc.table_name = :table
               AND kcu.column_name = :column
               AND ccu.table_name = 'users'
            """
        ),
        {"table": table, "column": COLUMN},
    ).all()
    return [r[0] for r in rows]


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _repoint(table: str, ondelete: str) -> None:
    """Drop whatever FK governs ``table.user_id`` and recreate it with ``ondelete``."""
    for name in _user_fk_names(table):
        op.execute(sa.text(
            f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"'))
    op.execute(sa.text(
        f"ALTER TABLE {table} "
        f"ADD CONSTRAINT {table}_{COLUMN}_fkey "
        f"FOREIGN KEY ({COLUMN}) REFERENCES users (id) "
        f"ON DELETE {ondelete}"
    ))


def _apply(ondelete: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # See the module docstring: SQLite cannot alter a foreign key, and a
        # create_all database already carries the model's declaration.
        return

    present = _existing_tables()
    for table in HISTORY_TABLES:
        if table not in present:
            # The table's own revision has not run in this database. Its FK
            # will be created with the model's RESTRICT when it does.
            continue
        _repoint(table, ondelete)


def upgrade() -> None:
    _apply("RESTRICT")


def downgrade() -> None:
    """Restore CASCADE, which is what the previous revisions declared.

    Safe: the constraint is replaced in place, no row is touched, and a Worker
    who could not be deleted under RESTRICT simply becomes deletable again —
    the pre-Ruling-M behaviour.
    """
    _apply("CASCADE")
