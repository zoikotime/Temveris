"""work_day_ledger.shift_attribution_rule — R-12.3.4 / §35 provenance

§35 lists ``shift_attribution_rule`` in the day record's provenance block, and
R-12.3.4 requires the rule in force to be persisted there so that "Changing it
MUST NOT retroactively re-attribute sealed weeks". A row that does not carry
its own rule cannot answer which rule produced its ``work_date``, so a replay
would have to assume whatever the code says today — the drift §17 forbids.

THE BACKFILL IS PROVEN, NOT ASSUMED
-----------------------------------
``server_default='START_DATE'`` also fills rows that already exist, so it is
only admissible if START_DATE is provably the rule those rows were sealed
under. It is:

* ``work_ledger.service._write_version`` is the ONLY construction of a
  ``WorkDayLedger`` anywhere in the application, and ``seal_day``,
  ``supersede_day`` and ``record_integrity_fault`` are its only callers;
* the only caller of those, in turn, is
  ``work_ledger_producer.produce_and_seal_work_day``;
* that producer selects a day's sessions by ``clock_in_time`` inside the
  organization-local day and then re-verifies each one with
  ``context.contains(resolved.clock_in_at)``, refusing the day with
  ``WORK_DATE_MISMATCH`` otherwise.

Selection and verification are both on the session START, which is START_DATE
attribution by construction. No other rule has ever been implemented, and no
code path can write a ledger row without going through that one. So the value
is recovered, not invented — the distinction the owner ruling draws.

The column is NOT NULL because every row has a knowable answer. Were that not
so, the honest column would be nullable and the unknown rows would stay NULL
rather than be given a guess.

Revision ID: b2e6d9f04a17
Revises: a8f4c2e91b07
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa

revision = "b2e6d9f04a17"
down_revision = "a8f4c2e91b07"
branch_labels = None
depends_on = None

# The literal, restated rather than imported: a migration must keep running
# against the schema of its own moment, and importing the model would couple it
# to whatever that model becomes several revisions later. The single source of
# truth is ``app/work_ledger/attribution.py``; a test asserts the two agree.
_START_DATE = "START_DATE"

_TABLE = "work_day_ledger"
_COLUMN = "shift_attribution_rule"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=32), nullable=False,
                  server_default=_START_DATE),
    )
    op.create_index(
        f"ix_{_TABLE}_{_COLUMN}", _TABLE, [_COLUMN], unique=False,
    )


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_{_COLUMN}", table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
