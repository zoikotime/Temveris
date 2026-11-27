"""session-boundary provenance (§45.D-15)

Revision ID: c4e9b7d2a583
Revises: b7c4e0a92d51
Create Date: 2026-09-09 00:00:00.000000

Adds ONE table:

    clock_session_boundary_event

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
Owner ruling §45.D-15: every writer that sets ``clock_sessions.clock_in_time``
or ``clock_sessions.clock_out_time`` must record, immutably and at the moment
of the write, which of five approved origins that boundary represents
(``WORKER``, ``ADMIN_OVERRIDE``, ``STALE_AUTOCLOSE``, ``ABANDONED_AUTOCLOSE``,
``OFFBOARDING``). Nothing in the existing schema records it, and the audit
established that it cannot be reconstructed: the abandoned-session sweep closes
at ``max(last_heartbeat, clock_in)``, so a synthesized closure can land exactly
on the clock-in instant, making it indistinguishable BY VALUE from a Worker who
clocked out immediately.

WHY A TABLE AND NOT A COLUMN (owner decision D15-3-A)
-----------------------------------------------------
``POST /employees/time-adjustments`` rewrites both boundaries IN PLACE on a
session the Worker may already have closed. A single mutable column on
``clock_sessions`` has no correct value in that case — leaving ``WORKER``
attributes an admin's timestamp to the Worker, overwriting it to
``ADMIN_OVERRIDE`` erases that the Worker's close ever happened and makes the
column mutable. One immutable row per boundary write records both facts.

No column is added to ``clock_sessions``. It is not altered by this revision.

RETENTION (owner decision D15-4-A)
----------------------------------
Both foreign keys are ``RESTRICT``:

* ``session_id -> clock_sessions.id`` — ``clock_sessions`` children are
  ordinarily CASCADE (``break_sessions``, ``offline_intervals``), but those are
  operational detail. This is protected historical evidence and a cascade would
  let an ordinary deletion destroy the provenance this increment exists to
  preserve.
* ``user_id -> users.id`` — the SUBJECT Worker, matching the five existing
  protected history tables, all of which are RESTRICT after ``a2f9d61c4b83``
  and ``b7c4e0a92d51``.

``employees.router._PROTECTED_HISTORY_TABLES`` gains
``("clock_session_boundary_event", "user_id")`` in the same increment, so a
Worker with boundary history is refused with a clean ``409
EMPLOYEE_HAS_PROTECTED_HISTORY`` rather than reaching the database and
surfacing an IntegrityError as a 500. The RESTRICT above is the backstop.

``actor_label`` carries NO foreign key, deliberately (D15-4-A). Every FK action
would break the ruling: CASCADE deletes the event, RESTRICT would make every
admin who ever corrected a timestamp permanently undeletable, and SET NULL
*writes to the row* when an actor is deleted — mutating an event required to be
immutable, and by a path that bypasses the ORM listeners entirely (the defect
ruling F documents for ``config_version.captured_by_user_id``). With no FK,
deleting an actor touches this table in no way at all. It follows ruling F's
``config_version.captured_by_label`` and its cited precedent
``rollout.RolloutControl.last_transition_by``: an id where one exists, SYSTEM
otherwise. An identifying label, not a profile.

STRUCTURAL BOUNDARY/PROVENANCE PAIRING (owner decision D15-2-B)
---------------------------------------------------------------
``ck_csbe_boundary_provenance_pairing`` makes a closure-only origin on the
``clock_in`` boundary UNREPRESENTABLE rather than merely discouraged: a
``clock_in`` row can only satisfy the constraint with ``WORKER`` or
``ADMIN_OVERRIDE``. A clock-in is not a closure, so ``STALE_AUTOCLOSE``,
``ABANDONED_AUTOCLOSE`` and ``OFFBOARDING`` would attribute an event that has
not happened. SQLite and PostgreSQL both enforce CHECK, so the guarantee holds
in the test suite as well as in production.

The vocabulary is the existing Work Ledger ``Provenance`` enum, reused
verbatim: no new enum, no duplicate enum, no sixth value. ``SYSTEM`` appears
only in ``actor_label`` and is never a provenance value.

NO ROWS CREATED. NO BACKFILL.
-----------------------------
The table is created empty and stays empty until live writers append to it.
Nothing is derived from existing timestamps, endpoint names, session state,
``work_events``, break records or auto-close heuristics — every one of those
would be the inference §45.D-15 forbids. There is no server default on
``provenance``, so no row can acquire an origin it was not given.

Sessions predating this revision therefore hold zero boundary events and are
permanently unattributable, by ruling rather than by omission. Deciding what a
future Work Ledger producer does with them is NOT part of this revision.

PURELY ADDITIVE
---------------
One ``create_table`` plus its indexes. No existing table is created, altered,
renamed or dropped: ``clock_sessions``, ``users``, ``break_sessions``,
``offline_intervals``, ``work_day_ledger``, ``work_state_interval``,
``config_value``, ``config_version``, ``eco_day_record`` and every rollout,
leave-interval and worker-context table are untouched.

MIGRATION PARENT
----------------
``down_revision = 'b7c4e0a92d51'`` — verified offline as the single head of a
70-revision graph with a single base (``81c63dee733a``) and no dangling
parents. An ordinary single-parent child: no new head, no merge revision, no
history modified.

REVERSIBLE
----------
``downgrade`` drops the table it created and nothing else.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so
no command executing ``env.py`` was run. Verified offline by compiling the DDL
for both the SQLite and PostgreSQL dialects, and by creating the table on a
private in-memory SQLite database only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e9b7d2a583'
down_revision: Union[str, Sequence[str], None] = 'b7c4e0a92d51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "clock_session_boundary_event"

#: Kept as module constants so ``tests/test_clock_session_boundary.py`` can pin
#: them against ``app/clock_session_boundary/models.py``. A migration that
#: drifted from the model would produce a database whose constraint disagrees
#: with the code's validation — the two are asserted equal.
BOUNDARY_VOCABULARY_SQL = "boundary IN ('clock_in', 'clock_out')"

PAIRING_SQL = (
    "(boundary = 'clock_in' AND provenance IN "
    "('ADMIN_OVERRIDE', 'WORKER'))"
    " OR "
    "(boundary = 'clock_out' AND provenance IN "
    "('ABANDONED_AUTOCLOSE', 'ADMIN_OVERRIDE', 'OFFBOARDING', "
    "'STALE_AUTOCLOSE', 'WORKER'))"
)

SEQUENCE_SQL = "sequence >= 1"

#: Guarded the same way every revision in this programme is guarded: a database
#: built by ``create_all`` already has the table from the ORM model, so neither
#: branch may raise.
INDEXES = (
    ("ix_clock_session_boundary_event_id", ["id"]),
    ("ix_clock_session_boundary_event_session_id", ["session_id"]),
    ("ix_clock_session_boundary_event_user_id", ["user_id"]),
    ("ix_clock_session_boundary_event_provenance", ["provenance"]),
    ("ix_csbe_session_boundary_seq", ["session_id", "boundary", "sequence"]),
    ("ix_csbe_user_boundary", ["user_id", "boundary"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE in set(inspector.get_table_names()):
        # Already present — created from the ORM model by ``create_all``.
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("boundary", sa.String(length=16), nullable=False),
        sa.Column("boundary_at", sa.DateTime(timezone=True), nullable=False),
        # No server_default: an origin is stated by the writer or refused.
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("actor_label", sa.String(length=64), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["clock_sessions.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            BOUNDARY_VOCABULARY_SQL, name="ck_csbe_boundary_vocabulary",
        ),
        sa.CheckConstraint(
            PAIRING_SQL, name="ck_csbe_boundary_provenance_pairing",
        ),
        sa.CheckConstraint(SEQUENCE_SQL, name="ck_csbe_sequence_positive"),
        sa.UniqueConstraint(
            "session_id", "boundary", "sequence",
            name="uq_csbe_session_boundary_sequence",
        ),
    )

    for name, columns in INDEXES:
        op.create_index(name, TABLE, columns, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return

    existing = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    for name, _columns in INDEXES:
        if name in existing:
            op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
