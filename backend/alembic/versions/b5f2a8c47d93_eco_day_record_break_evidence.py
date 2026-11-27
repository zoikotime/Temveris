"""ECO day record: observed and qualifying break seconds (INV-4, §35)

§35 stores both halves for each break state:

    brk_m_seconds              observed meal duration
    brk_m_qualifying_seconds   meal admitted toward the requirement
    brk_r_seconds              observed rest duration
    brk_r_qualifying_seconds   rest admitted, after the cumulative daily cap

WHY BOTH HALVES
---------------
They differ wherever a Tenant treats meal as unpaid, which §13.2 calls the
typical case: 45 seconds observed, 0 qualifying. The arithmetic already
honoured INV-4 -- ``break_classification`` and ``break_treatment`` have always
evaluated BRK_M and BRK_R as legally distinct states -- but the day RECORD
carried only the combined qualifying contribution, so the distinction was
invisible on the artifact that outlives the computation.

§26.1 is explicit about what that costs: the separately-labelled break lines
are "the Worker's only opportunity to notice that a rest break was
misclassified as a meal break before the day seals". A record that cannot
show them cannot support that surface, and INV-4 is one of the four canon
violations that triggered the v1.0 rebuild (defect D-01).

A RELATED CORRECTION IN THE SAME CHANGE
----------------------------------------
``BreakTreatmentOutcome`` accumulated rest and meal seconds only INSIDE the
``rest_qualifies`` / ``meal_qualifies`` branches, so a non-qualifying meal was
never counted at all. The observed totals are now accumulated unconditionally;
the qualifying totals and the cap are untouched, so no computed figure moves.

NULLABLE, NOT BACK-FILLED
-------------------------
``eco_day_record`` is append-only. A row written before these columns existed
can never acquire them, and deriving them now from today's classification
would attribute a present-day determination to a past evaluation. NULL is the
truthful record, exactly as for the AC-16 provenance in a4d9e7b3c216.

Revision ID: b5f2a8c47d93
Revises: a4d9e7b3c216
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "b5f2a8c47d93"
down_revision = "a4d9e7b3c216"
branch_labels = None
depends_on = None

TABLE = "eco_day_record"

COLUMNS = (
    "brk_m_seconds",
    "brk_m_qualifying_seconds",
    "brk_r_seconds",
    "brk_r_qualifying_seconds",
)


def _existing(bind):
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    """Additive only, guarded per column: a ``create_all`` database already
    carries them from the ORM model, so neither branch may raise."""
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return

    present = _existing(bind)
    for name in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Refused while any row carries the evidence. Dropping these would
    discard the only record that the meal/rest distinction was applied to a
    given day, and the rows are append-only so it could not be restored."""
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return

    present = _existing(bind)
    named = [n for n in COLUMNS if n in present]
    if named:
        checks = " OR ".join(f"{n} IS NOT NULL" for n in named)
        held = bind.execute(
            sa.text(f"SELECT count(*) FROM {TABLE} WHERE {checks}")  # noqa: S608
        ).scalar() or 0
        if held:
            raise RuntimeError(
                f"cannot downgrade: {held} day record(s) carry INV-4 break "
                f"evidence. Dropping these columns would discard the observed "
                f"and qualifying meal and rest seconds those evaluations were "
                f"made against.")

    for name in reversed(COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
