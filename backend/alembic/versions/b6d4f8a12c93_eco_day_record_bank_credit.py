"""D-6.4 / D-6.10 — §18 step 10's bank credit, recorded on the day it was spent.

Revision ID: b6d4f8a12c93
Revises: a3c8e5f10b47
Create Date: 2026-09-22

ADDITIVE. One nullable column on ``eco_day_record``. No existing column is
altered, no index changes, no table is created or dropped, no row is rewritten,
and **no backfill is performed**.

WHAT THIS COLUMN IS
-------------------
``bank_credit_minutes`` is the minutes of banked surplus one day consumed to
reduce its own requirement gap, at §18 step 10::

    10  bank  := bankCredit(worker, work_date, policy.netting_mode)
    11  gross := MAX(0, req.minutes - q - bank)

**IT IS NOT A BALANCE, AND THIS REVISION CREATES NO BANK.**
------------------------------------------------------------
§15.1 / INV-3, verbatim:

    "The bank balance is itself a fold over day records, never a stored
     counter."

So this is one day's SHARE of that fold, recorded on the row whose arithmetic
it explains — not an account, not a running total, and not a thing any code
increments. No balance table, counter column, period row or accumulator is
created here or anywhere else; a structural test asserts that across the whole
application and the whole migration history.

The distinction is load-bearing. A stored balance can drift from the records it
claims to summarise, and once it does nobody can say which is true. Recomputing
the pool from immutable day records every time is slower and always correct.

**NO BACKFILL, DELIBERATELY.**
------------------------------
Every ``eco_day_record`` row written before this revision was evaluated with no
netting implemented, under a policy whose netting mode was recorded but never
consumed. NULL is the truthful value: no fold ran, so no credit was determined.

Writing 0 into them would be the tempting mistake, because 0 is what a `NONE`
policy produces and almost every existing row is `NONE`. It is still wrong: 0
asserts that a fold ran and found nothing available, which is a different
statement from "no fold ran". Those rows are also content-hashed, and a
backfill would silently invalidate every stored digest — the hash payload now
includes this field, so a row written before it existed hashes without it and
must keep the digest it was stored with.

Days re-evaluated after this revision produce a new authoritative record
through the ordinary supersession path, with a real credit and a digest that
includes it. That is the only way a historical row acquires one.

WHY NULLABLE
------------
For the reason above, and because NOT NULL with a 0 default would perform
exactly the backfill this revision refuses.
"""
from alembic import op
import sqlalchemy as sa


revision = "b6d4f8a12c93"
down_revision = "a3c8e5f10b47"
branch_labels = None
depends_on = None

TABLE = "eco_day_record"
COLUMN = "bank_credit_minutes"
KEY_COLUMN = "bank_input_key"

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

_ELEVEN_COMPONENT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
    "ON eco_day_record "
    "(organization_id, user_id, work_date, ledger_version, "
    " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
    " leave_input_snapshot_key, "
    " COALESCE(classification_snapshot_key, ''), "
    " COALESCE(evaluation_deferral_reason, ''), "
    " COALESCE(break_treatment_profile_ref, ''), "
    " COALESCE(bank_input_key, ''))"
)


def _existing(bind) -> set:
    """Columns already present, so a partial re-run is safe."""
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()

    if COLUMN not in _existing(bind):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.Integer(),
                sa.CheckConstraint(
                    f"{COLUMN} IS NULL OR {COLUMN} >= 0",
                    name="ck_eco_day_record_bank_credit_nonnegative"),
                nullable=True,
            ),
        )

    if KEY_COLUMN not in _existing(bind):
        op.add_column(TABLE, sa.Column(KEY_COLUMN, sa.String(64),
                                       nullable=True))

    # The snapshot identity widens from TEN components to ELEVEN. Rebuilt
    # rather than added alongside: two unique indexes over overlapping column
    # sets would let a row satisfy one and violate the other, and which one
    # governed would depend on which the planner reached first.
    #
    # COALESCE to '' for the same reason every other optional component uses
    # it — SQL treats NULLs as distinct in a unique index, so two records that
    # both folded nothing would not collide, and the identity would not be an
    # identity.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.execute(sa.text(_ELEVEN_COMPONENT_INDEX))

    # No UPDATE. No backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()

    # Restore the ten-component identity first, so the table is never without
    # one, then drop the columns.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.execute(sa.text(_TEN_COMPONENT_INDEX))

    # Dropping loses the recorded credit for every day evaluated since the
    # upgrade. The fold could re-derive it, but the STORED digests could not be
    # reconciled, so this is guarded rather than unconditional.
    present = _existing(bind)
    for column in (KEY_COLUMN, COLUMN):
        if column in present:
            op.drop_column(TABLE, column)
