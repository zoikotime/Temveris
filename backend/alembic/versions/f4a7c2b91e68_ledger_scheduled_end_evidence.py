"""D-1.1 / D-2.1 — the scheduled end joins the ledger's frozen day evidence.

Revision ID: f4a7c2b91e68
Revises: e2b6d4f89c35
Create Date: 2026-09-21

ADDITIVE. Two nullable columns on ``work_day_ledger``. No existing column is
altered, no table is dropped, no index changes, no row is rewritten, and
**no backfill is performed**.

WHAT THESE COLUMNS ARE
----------------------
``scheduled_end_local`` is the local clock time the working day was scheduled
to end, and ``scheduled_end_source`` is the configuration tier that produced
it, carried verbatim from the resolver's own ``EffectiveSchedule.sources
["end_local"]`` label. Together they are the second operand of §18 step 14:

    variance := actual_end_local - scheduled_end_local

They are frozen at seal for the reason owner ruling D-18D froze
``is_working_day`` and ``working_day_source`` beside them: the roster is
mutable, and a later shift edit must not silently change what a sealed day was
measured against (§17 historical reproducibility).

**NO BACKFILL, DELIBERATELY.**
------------------------------
Every row sealed before this revision was sealed without a scheduled end,
because the platform had nowhere to put one. NULL is therefore the truthful
value and it is a real answer rather than a missing one: it means nobody
recorded what time that day was scheduled to end, so no schedule variance is
derivable for it and none ever will be.

Manufacturing a value would require reading the CURRENT roster and asserting it
governed a day sealed months earlier — the precise fabrication owner ruling
D-1.1 point 4 forbids, and the same reasoning under which migration
``f1a6b9c4d283`` declined to synthesise leave intervals for requests recorded at
a coarser granularity.

WHY BOTH COLUMNS ARE NULLABLE, NOT ONLY THE HISTORICAL ROWS
-----------------------------------------------------------
``scheduled_end_local`` carries a second, ongoing NULL meaning. Owner ruling
D-2.1: when the resolver reports a ``"system"`` tier for ``end_local`` — its
documented fail-open default, meaning no tier configured a shift end — the day
still seals normally and the scheduled end is recorded as NULL with the source
recorded as ``"system"``.

That asymmetry with ``working_day_source`` (where ``"system"`` refuses the
seal outright, D6-A) is deliberate and was ruled on explicitly: schedule
variance is "recorded; never drives the threshold" (§10) and "signed, never
used in comparisons" (§35), so its absence withdraws one recorded field rather
than the whole day's evaluation.

A reader can always distinguish the two NULLs by the source column: NULL
source means the row predates this revision; ``"system"`` means the tenant
configured no shift end.

NO DATABASE CHECK ON THE SOURCE VOCABULARY
-------------------------------------------
Following ``working_day_source``, which is also an unconstrained
``VARCHAR(32)``: the vocabulary is the resolver's, and a CHECK here would
freeze a copy of it that could drift out of step with the resolver it mirrors.
The producer is the enforcement point, and a test pins the admitted set.
"""
from alembic import op
import sqlalchemy as sa


revision = "f4a7c2b91e68"
down_revision = "e2b6d4f89c35"
branch_labels = None
depends_on = None

TABLE = "work_day_ledger"
COLUMNS = ("scheduled_end_local", "scheduled_end_source")


def _existing(bind) -> set:
    """Columns already present, so a partial re-run is safe."""
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)

    if "scheduled_end_local" not in present:
        op.add_column(
            TABLE,
            sa.Column("scheduled_end_local", sa.Time(), nullable=True),
        )
    if "scheduled_end_source" not in present:
        op.add_column(
            TABLE,
            sa.Column("scheduled_end_source", sa.String(32), nullable=True),
        )

    # No UPDATE. No backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)

    # Dropping loses the frozen evidence for every day sealed since the
    # upgrade, and nothing can reconstruct it — the roster it came from is
    # mutable. Guarded so a downgrade on a database that never ran the upgrade
    # is a no-op rather than an error.
    for column in reversed(COLUMNS):
        if column in present:
            op.drop_column(TABLE, column)
