"""ECO day record: AC-16 provenance and the pre-rounding value

§43 AC-16 is a release gate: "Every day record persists provenance:
requirement source, policy version, rounding profile, netting mode, ledger
version, timezone." Policy version and ledger version were already carried.
This revision adds the other four, plus three fields §35 requires alongside
them.

    qualifying_seconds        §35.1 / INV-8 — the pre-rounding value
    requirement_source        §10.2 — which rank won
    requirement_source_rank   §10.2
    requirement_source_ref    §10.2 — which object supplied it
    rounding_profile          §16.6
    netting_mode              §15
    timezone_iana             §12.2
    utc_offset_seconds        §12.2
    calculation_version       §35.1 — engine semver

WHY THIS COULD NOT WAIT FOR A LATER RELEASE
--------------------------------------------
``eco_day_record`` is append-only. A row written without these can never
acquire them: the ORM guard and the standing ruling both refuse an UPDATE, and
a correction supersedes rather than edits. Every day evaluated before this
revision would therefore be permanently unverifiable against the rounding
profile that produced it — which is INV-8, the property the product is sold
on. Adding the columns after a shadow period would leave that period's
evidence permanently incomplete.

WHY EVERY COLUMN IS NULLABLE, AND NOTHING IS BACK-FILLED
---------------------------------------------------------
Same reason, from the other side. Rows that predate this revision genuinely
have no recorded provenance, and inventing one would be fabricating exactly
the evidence §10 exists to make defensible. NULL is the truthful record of
"this evaluation predates the field", which is how owner ruling F already
treats ``policy_version.netting_decision_provenance``.

At the time of writing no ECO day record exists in any environment, so in
practice nothing is grandfathered — but the columns are nullable because the
append-only contract, not the current row count, is what governs.

THE CONTENT HASH
----------------
Seven of these join ``compute_result_hash``'s IDENTITY half and
``qualifying_seconds`` joins its RESULT half, because each governs or
constitutes the arithmetic. ``calculation_version`` deliberately does not:
§35.1 gives it the job of forcing replay, and a version inside the digest
would make a replay under a newer engine COLLIDE with the stored record
rather than supersede it.

Revision ID: a4d9e7b3c216
Revises: f1c6b28d5a73
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "a4d9e7b3c216"
down_revision = "f1c6b28d5a73"
branch_labels = None
depends_on = None

TABLE = "eco_day_record"

#: (name, type). Ordered as §35 lists them, so the schema reads like the spec.
COLUMNS = (
    ("qualifying_seconds", sa.Integer()),
    ("requirement_source", sa.String(length=32)),
    ("requirement_source_rank", sa.Integer()),
    ("requirement_source_ref", sa.String(length=128)),
    ("rounding_profile", sa.String(length=32)),
    ("netting_mode", sa.String(length=32)),
    ("timezone_iana", sa.String(length=64)),
    ("utc_offset_seconds", sa.Integer()),
    ("calculation_version", sa.String(length=32)),
)


def _existing(bind):
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    """Additive only. Guarded per column, the way every revision in this
    programme is guarded: a database built by ``create_all`` already has them
    from the ORM model, so neither branch may raise."""
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        # The creating revision has not run in this database. The columns
        # arrive with the table, from the model.
        return

    present = _existing(bind)
    for name, type_ in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Dropping these discards recorded provenance, so it is refused while any
    row carries it. §10 exists to make a figure defensible; silently removing
    the evidence of where it came from is the opposite."""
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return

    present = _existing(bind)
    named = [n for n, _ in COLUMNS if n in present]
    if named:
        checks = " OR ".join(f"{n} IS NOT NULL" for n in named)
        held = bind.execute(
            sa.text(f"SELECT count(*) FROM {TABLE} WHERE {checks}")  # noqa: S608
        ).scalar() or 0
        if held:
            raise RuntimeError(
                f"cannot downgrade: {held} day record(s) carry AC-16 "
                f"provenance. Dropping these columns would discard the "
                f"requirement source, rounding profile, netting mode and "
                f"timezone those evaluations were made under, and the rows "
                f"are append-only so it could not be restored.")

    for name, _ in reversed(COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
