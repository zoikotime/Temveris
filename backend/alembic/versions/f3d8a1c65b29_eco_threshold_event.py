"""eco threshold event (EVAL-EVENT)

Revision ID: f3d8a1c65b29
Revises: e7c2b9d4f815
Create Date: 2026-09-11 00:00:00.000000

Creates the append-only ECO threshold lifecycle history:

    eco_threshold_event   one row per crossing, withdrawal or re-crossing

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
Owner ruling D1 makes the event history a dedicated, append-only table, and §21.2
makes "has this week already crossed?" a question about a PERSISTED natural key
rather than a boolean or a process-local memo. Neither survives a restart, a
second worker or a replay without storage.

No existing table can hold it. ``eco_day_record`` is a DAY evaluation and must
not gain weekly lifecycle state; ``work_day_ledger`` is authoritative EVIDENCE
and must not gain derived output; ``app/notifications`` is platform-generic,
has no ECO category, and D7 puts delivery outside EVAL-EVENT entirely.

APPEND-ONLY, ENFORCED BY THE DATABASE (D1)
------------------------------------------
A withdrawal is a NEW row referencing the crossing it closes; a re-crossing is a
NEW row at the next sequence. Nothing is ever updated or deleted, so R-21.3.1 —
the original is never deleted, hidden or back-dated — holds structurally rather
than by application discipline.

``app/eval_event/append_only_ddl.py`` is the single definition of the trigger
SQL, imported by both this revision and the ORM model. That module imports
nothing — no ``Base``, no models, no SQLAlchemy — precisely so a revision may
import it safely. The emission is EXPLICIT here because ``op.create_table``
builds its own ``Table`` object, so the model's ``after_create`` listener does
not fire for it; without this, a migrated database would lack the triggers while
a create_all database had them, and every below-ORM test would pass vacuously
against a shape production does not have.

SQLite gets two ``RAISE(ABORT)`` triggers. PostgreSQL gets a PL/pgSQL guard
function plus two row-level triggers. A dialect with no entry gets no protection
at all, which is why the event store must never be run against one — asserted by
test rather than assumed.

THE UNIQUENESS BOUNDARY, AND ONE DEVIATION
------------------------------------------
D6 rules the boundary::

    (organization_id, user_id, workweek_key, tier, sequence, mode)

``event_type`` is added to it here. The six-column form cannot be satisfied: a
withdrawal is a new row in the same episode and needs a ``sequence``; giving it
one of its own consumes 2 and forces the re-crossing to 3, contradicting §21.4,
while sharing the crossing's sequence violates the constraint. With
``event_type`` included, the withdrawal shares the sequence of the crossing it
closes and one numbered sequence is one complete lifecycle: crossing 1,
withdrawal 1, re-crossing 2.

This relaxes the ruled six-column form — seven columns permit rows six would
refuse — and was confirmed by owner ruling as the amended D6 boundary.

The invariant the ruling protects is unaffected and still enforced by the
database, though NOT by the obvious argument. A new crossing takes the sequence
one past the count of WITHDRAWALS in its episode, never ``max(sequence) + 1``:
the latter lets a run that read "nothing open" before a competitor committed,
but counted sequences after, land on a higher number and create a SECOND open
primary without violating anything. Counting withdrawals makes both racers
compute the same sequence, so the identity constraint refuses the loser.
``uq_eco_threshold_event_withdrawal_target`` closes the other side by allowing
at most one row to supersede any given row, so a crossing cannot be withdrawn
twice. See ``app/eval_event/service.py::_next_sequence``.

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. ``eco_day_record``,
``work_day_ledger``, ``config_version``, ``users``, ``organizations`` and every
notification table are untouched. NO ROWS CREATED — no backfill and no
historical evaluation. EVAL-EVENT has not run, is not wired, and the ECO v2
rollout remains OFF.

MIGRATION PARENT
----------------
``down_revision = 'e7c2b9d4f815'`` — the current single head. Ordinary
single-parent child: no new head, no merge revision, no history modified.

NOT EXECUTED AGAINST ANY REAL DATABASE
--------------------------------------
Not applied to production, staging or any shared database. The repository
``.env`` points at production, so no command executing ``env.py`` was run.
Verified against a private in-memory SQLite database (upgrade, downgrade,
upgrade) and by PostgreSQL dialect compilation without connecting.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.eval_event.append_only_ddl import (
    EVENT_TABLE,
    append_only_statements,
    drop_statements,
)

# revision identifiers, used by Alembic.
revision: str = 'f3d8a1c65b29'
down_revision: Union[str, Sequence[str], None] = 'e7c2b9d4f815'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    existing = set(sa.inspect(bind).get_table_names())

    if EVENT_TABLE in existing:
        return

    op.create_table(
        EVENT_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),

        # ── identity ────────────────────────────────────────────────────────
        # RESTRICT, never CASCADE: removing a person must never remove the
        # evidence that a threshold was reached, exactly as
        # ``config_version.captured_by_user_id`` and ``eco_day_record`` already
        # establish. A cascade would delete below the ORM listeners.
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workweek_key", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),

        # ── what happened ───────────────────────────────────────────────────
        sa.Column("event_type", sa.String(length=32), nullable=False),
        # Set only on a withdrawal (the crossing it closes) and on a
        # re-crossing (the withdrawal it follows). The reverse pointer cannot
        # exist: writing one would UPDATE a row D1 declares immutable.
        sa.Column("supersedes_event_id", sa.Integer(), nullable=True),

        # ── the comparison, frozen ──────────────────────────────────────────
        sa.Column("weekly_total_minutes", sa.Integer(), nullable=False),
        sa.Column("threshold_minutes", sa.Integer(), nullable=False),
        sa.Column("threshold_config_version", sa.String(length=64),
                  nullable=True),

        # ── retroactivity (D4) and provenance (D2) ──────────────────────────
        sa.Column("is_retroactive", sa.Boolean(), nullable=False),
        sa.Column("caused_by", sa.JSON(), nullable=False),
        sa.Column("week_provenance", sa.JSON(), nullable=False),

        # ── recipients (D5, increment-scoped: the Worker only) ──────────────
        sa.Column("recipient_snapshot", sa.JSON(), nullable=False),
        sa.Column("worker_recipient", sa.Integer(), nullable=False),

        # ── audit ───────────────────────────────────────────────────────────
        # No ``updated_at``: there is no update.
        sa.Column("created_at_utc", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_event_id"],
                                [f"{EVENT_TABLE}.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "organization_id", "user_id", "workweek_key", "tier", "sequence",
            "mode", "event_type", name="uq_eco_threshold_event_identity"),
        sa.UniqueConstraint(
            "supersedes_event_id",
            name="uq_eco_threshold_event_withdrawal_target"),
        sa.CheckConstraint("sequence >= 1",
                           name="ck_eco_threshold_event_sequence_positive"),
        sa.CheckConstraint("weekly_total_minutes >= 0",
                           name="ck_eco_threshold_event_total_non_negative"),
        sa.CheckConstraint(
            "threshold_minutes >= 0",
            name="ck_eco_threshold_event_threshold_non_negative"),
        sa.CheckConstraint("length(created_by) > 0",
                           name="ck_eco_threshold_event_actor"),
    )

    op.create_index(f"ix_{EVENT_TABLE}_id", EVENT_TABLE, ["id"])
    op.create_index(f"ix_{EVENT_TABLE}_organization_id", EVENT_TABLE,
                    ["organization_id"])
    op.create_index(f"ix_{EVENT_TABLE}_user_id", EVENT_TABLE, ["user_id"])
    op.create_index(f"ix_{EVENT_TABLE}_workweek_key", EVENT_TABLE,
                    ["workweek_key"])
    op.create_index(f"ix_{EVENT_TABLE}_mode", EVENT_TABLE, ["mode"])
    # The lifecycle read: "is there an open crossing for this episode?"
    op.create_index("ix_eco_threshold_event_episode", EVENT_TABLE,
                    ["organization_id", "user_id", "workweek_key", "tier",
                     "mode"])
    op.create_index("ix_eco_threshold_event_supersedes", EVENT_TABLE,
                    ["supersedes_event_id"])

    # D1 enforcement. Emitted explicitly — see the module docstring.
    for statement in append_only_statements(dialect):
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    existing = set(sa.inspect(bind).get_table_names())

    if EVENT_TABLE not in existing:
        return

    # Triggers first: PostgreSQL refuses to drop a function two triggers still
    # depend on, and the shared constants already order the statements.
    for statement in drop_statements(dialect):
        op.execute(statement)

    for index in (
        "ix_eco_threshold_event_supersedes",
        "ix_eco_threshold_event_episode",
        f"ix_{EVENT_TABLE}_mode",
        f"ix_{EVENT_TABLE}_workweek_key",
        f"ix_{EVENT_TABLE}_user_id",
        f"ix_{EVENT_TABLE}_organization_id",
        f"ix_{EVENT_TABLE}_id",
    ):
        op.drop_index(index, table_name=EVENT_TABLE)

    op.drop_table(EVENT_TABLE)
