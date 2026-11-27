"""forward-only producer: cutover watermark + frozen working day (D-18C/D-18D)

Revision ID: d5f1a8c3b724
Revises: c4e9b7d2a583
Create Date: 2026-09-09 00:00:00.000000

Adds ONE table and TWO columns:

    work_ledger_producer_cutover                        (new table, D-18C)
    work_day_ledger.is_working_day       BOOLEAN NOT NULL   (D-18D)
    work_day_ledger.working_day_source   VARCHAR(32) NOT NULL (D-18D)

WHY (D-18C) — THE CUTOVER WATERMARK
-----------------------------------
The producer is forward-only, and that has to be enforced by durable state the
producer reads for itself rather than by a parameter a caller supplies. Owner
decision D-18C rejected hanging it off ``app/rollout``: a rollout control
transitions both ways, and a control that can be switched back on would move
the eligible window BACKWARDS on re-enable — which is how historical sealing
would re-enter through the front door. A cutover is a monotonic watermark, so
it gets its own record.

``producer_cutover_at`` has no default and no inferred value. A tenant with no
row is refused. Monotonicity is enforced by a guarded UPDATE (expected
``revision`` plus ``producer_cutover_at < :new``, with a rowcount check) and by
ORM listeners that refuse a decrease; moving it later only ever makes FEWER
days eligible.

``set_by`` carries durable attribution with NO foreign key, following ruling
F's ``config_version.captured_by_label`` and ``RolloutControl.
last_transition_by`` — an id where one exists, SYSTEM otherwise — so removing a
person never removes the record of what they set.

WHY (D-18D) — THE FROZEN WORKING DAY
------------------------------------
D-21 requires the working-day result to be resolved and FROZEN at seal, never
reconstructed from the live schedule. ``is_working_day`` is that result, and
``working_day_source`` is the resolver's own ``EffectiveSchedule.sources``
label reproduced verbatim (assignment | shift | employee | legacy_user | team |
org | system) so a sealed row can be reconciled against the resolver that
produced it. The producer refuses to seal when that label is ``system``, which
is the resolver's fail-open path and means no tier configured the working week.

Both are NOT NULL with NO SERVER DEFAULT, by owner ruling D-18D. A default
would let a day acquire a working-day status nobody determined — the same
reason ``provenance`` has no default.

NO MIGRATION FOR THE NEW PROVENANCE VALUE
-----------------------------------------
D-18A adds ``Provenance.PRODUCER_DERIVED``, and it needs no schema change:
``work_state_interval.provenance`` is ``VARCHAR(32)`` whose only CHECK is
``duration_seconds >= 0`` — the vocabulary is enforced in the application, per
the platform's existing convention. Verified before writing this revision.
D-15's ``clock_session_boundary_event`` is untouched: its pairing constraint
whitelists the five session-close values explicitly, so the ninth value is
already inadmissible there.

NO ROWS CREATED. NO BACKFILL.
-----------------------------
Nothing is inserted and nothing is derived from existing data. Because the two
new columns are NOT NULL with no default, they cannot be added to a
``work_day_ledger`` that already holds rows without inventing a working-day
status for sealed history — which D-18D forbids. So that case RAISES rather
than fabricating a value. In practice it cannot arise: ``work_day_ledger`` is
created by ``d4b7e2f9a615``, which is unapplied in every database.

PURELY ADDITIVE
---------------
One ``create_table``, two ``add_column``. No existing table is dropped or
renamed, no existing column is altered or removed, and no other table is
touched: ``clock_sessions``, ``clock_session_boundary_event``,
``work_state_interval``, ``config_value``, ``config_version``,
``eco_day_record``, ``users``, ``organizations`` and every rollout,
leave-interval and worker-context table are unchanged.

MIGRATION PARENT
----------------
``down_revision = 'c4e9b7d2a583'`` — verified offline as the single head of a
71-revision graph with a single base (``81c63dee733a``) and no dangling
parents. An ordinary single-parent child: no new head, no merge revision.

REVERSIBLE
----------
``downgrade`` drops exactly what ``upgrade`` created.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so
no command executing ``env.py`` was run. Verified offline by compiling the DDL
for both the SQLite and PostgreSQL dialects and by running it on a private
in-memory SQLite database only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5f1a8c3b724'
down_revision: Union[str, Sequence[str], None] = 'c4e9b7d2a583'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CUTOVER_TABLE = "work_ledger_producer_cutover"
LEDGER_TABLE = "work_day_ledger"
NEW_LEDGER_COLUMNS = ("is_working_day", "working_day_source")


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _columns(inspector, table: str) -> set:
    if table not in _tables(inspector):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── D-18C: the cutover watermark ────────────────────────────────────────
    # Guarded the same way every revision in this programme is guarded: a
    # database built by ``create_all`` already has the table from the ORM
    # model, so neither branch may raise.
    if CUTOVER_TABLE not in _tables(inspector):
        op.create_table(
            CUTOVER_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            # No server default: an unconfigured tenant is refused.
            sa.Column("producer_cutover_at",
                      sa.DateTime(timezone=True), nullable=False),
            sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("set_by", sa.String(length=64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
            ),
            sa.UniqueConstraint(
                "organization_id", name="uq_wl_producer_cutover_org",
            ),
            sa.CheckConstraint(
                "revision >= 1", name="ck_wl_producer_cutover_revision",
            ),
        )
        op.create_index(
            "ix_work_ledger_producer_cutover_id", CUTOVER_TABLE, ["id"],
            unique=False,
        )
        op.create_index(
            "ix_work_ledger_producer_cutover_organization_id", CUTOVER_TABLE,
            ["organization_id"], unique=False,
        )

    # ── D-18D: the frozen working-day result ────────────────────────────────
    inspector = sa.inspect(bind)
    if LEDGER_TABLE not in _tables(inspector):
        # The table is created — with these columns, from the ORM model — by
        # d4b7e2f9a615 plus this revision, or by create_all. Nothing to alter.
        return

    existing = _columns(inspector, LEDGER_TABLE)
    missing = [c for c in NEW_LEDGER_COLUMNS if c not in existing]
    if not missing:
        return

    row_count = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {LEDGER_TABLE}")  # noqa: S608 - fixed name
    ).scalar() or 0
    if row_count:
        # Loud rather than silent. Owner ruling D-18D forbids a default, and
        # there is no honest value to give a day that was sealed before the
        # working-day result was recorded — inventing one would be exactly the
        # backfill the ruling prohibits.
        raise RuntimeError(
            f"{LEDGER_TABLE} holds {row_count} row(s); {missing} are NOT NULL "
            f"with no default by owner ruling D-18D, and no working-day status "
            f"may be fabricated for already-sealed days. Those rows must be "
            f"resolved by an explicit owner-ruled decision, not by this "
            f"migration."
        )

    if "is_working_day" in missing:
        op.add_column(
            LEDGER_TABLE,
            sa.Column("is_working_day", sa.Boolean(), nullable=False),
        )
    if "working_day_source" in missing:
        op.add_column(
            LEDGER_TABLE,
            sa.Column("working_day_source", sa.String(length=32),
                      nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if LEDGER_TABLE in _tables(inspector):
        existing = _columns(inspector, LEDGER_TABLE)
        for column in reversed(NEW_LEDGER_COLUMNS):
            if column in existing:
                op.drop_column(LEDGER_TABLE, column)

    inspector = sa.inspect(bind)
    if CUTOVER_TABLE in _tables(inspector):
        for index in {ix["name"] for ix in inspector.get_indexes(CUTOVER_TABLE)}:
            op.drop_index(index, table_name=CUTOVER_TABLE)
        op.drop_table(CUTOVER_TABLE)
