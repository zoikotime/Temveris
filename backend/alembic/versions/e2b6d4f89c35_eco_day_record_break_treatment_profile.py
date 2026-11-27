"""P2-D4 — the governing break-treatment profile joins evaluation identity.

Revision ID: e2b6d4f89c35
Revises: d7f3b8c41a92
Create Date: 2026-09-21

ADDITIVE. One nullable column on ``eco_day_record`` and one index rebuild. No
existing column is altered, no table is dropped, no row is rewritten, and
**no backfill is performed**.

**NO BACKFILL, DELIBERATELY.**
------------------------------
Every record written before this revision was evaluated with no break-treatment
profile in force, because none existed. NULL is therefore the truthful value,
and it is a real answer rather than a missing one: it means the classification
control did not govern that day, so no break minute could qualify. Writing any
profile reference onto those rows would assert a governing rule that was never
resolved, and inventing a default profile would be worse — §13.2's attributes
have no neutral setting, and a fabricated one would silently decide whether rest
qualified for days nobody configured.

The column is NULLABLE for the same reason a NOT NULL default would be wrong: it
would force exactly that fabrication, for history and for every future day a
Tenant has not opted into.

WHY THE PROFILE IS ITS OWN IDENTITY COMPONENT
----------------------------------------------
Owner ruling P2-D4. ``EcoDayRecord`` already distinguishes governing RULES
(``policy_version``, ``config_version``) from content-addressed INPUT SETS
(``leave_input_snapshot_key``, ``classification_snapshot_key``). A
break-treatment profile is a rule: it says how a determination is TREATED, not
what was observed. Folding it into the classification snapshot would conflate
the two and invent a second meaning for one fact — the same collapse
POLICY-VERSION refused when it declined to alias ``policy_version`` to
``config_version``.

Its presence is what makes a profile correction a NEW GENERATION: a changed cap
changes the identity, so the writer supersedes instead of raising a conflict
against the answer computed under the old rule.

THE UNIQUE SNAPSHOT INDEX IS REBUILT, NOT WIDENED IN PLACE
-----------------------------------------------------------
``uq_eco_day_record_snapshot`` goes from nine components to ten, the new one
under COALESCE exactly as the four other nullable components already are. An
index cannot be redefined in place, so it is dropped and recreated. That is not
destructive: the index holds no data, and the rebuilt form is strictly wider —
every row admissible under the old rule remains admissible, because the added
component is the empty string for every existing row.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2b6d4f89c35"
down_revision = "d7f3b8c41a92"
branch_labels = None
depends_on = None

TABLE = "eco_day_record"
COLUMN = "break_treatment_profile_ref"

_NINE_COMPONENT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
    "ON eco_day_record "
    "(organization_id, user_id, work_date, ledger_version, "
    " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
    " leave_input_snapshot_key, "
    " COALESCE(classification_snapshot_key, ''), "
    " COALESCE(evaluation_deferral_reason, ''))"
)

_TEN_COMPONENT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
    "ON eco_day_record "
    "(organization_id, user_id, work_date, ledger_version, "
    " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
    " leave_input_snapshot_key, "
    " COALESCE(classification_snapshot_key, ''), "
    " COALESCE(evaluation_deferral_reason, ''), "
    " COALESCE(break_treatment_profile_ref, ''))"
)


def _columns(inspector) -> set:
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        # EVAL-DAY has not run in this database. create_all builds the table
        # with the column already present.
        return

    if COLUMN not in _columns(inspector):
        op.add_column(TABLE,
                      sa.Column(COLUMN, sa.String(length=64), nullable=True))

    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.execute(sa.text(_TEN_COMPONENT_INDEX))


def downgrade() -> None:
    """Restore the nine-component identity and drop the column.

    Safe: nothing else reads it. What is lost is the ability to tell which
    break-treatment rule governed a stored evaluation — after this runs, two
    evaluations computed under different caps become indistinguishable, and a
    profile correction would collide with its predecessor rather than
    superseding it. That is the honest consequence of removing the component,
    not a reason to keep a copy of it elsewhere.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        return

    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.execute(sa.text(_NINE_COMPONENT_INDEX))

    if COLUMN in _columns(inspector):
        op.drop_column(TABLE, COLUMN)
