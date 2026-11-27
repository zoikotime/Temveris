"""Add the 6-digit verification code to commercial email verification tokens

The activation flow's Verify step asks the visitor to type a code instead of
clicking the emailed link. Both credentials belong to the same verification
event, so they live on the same row and share one ``consumed_at`` — whichever
is used first completes the verification and the other reports
ALREADY_CONSUMED.

Deliberately NOT a new table, and deliberately not ``password_reset_otps``:
rows in that table are what ``POST /auth/reset-password`` checks for
eligibility, so a signup code stored there would double as a password-reset
code.

``code_hash`` is nullable so rows minted before this migration stay valid as
link-only tokens until they expire. ``code_attempts`` carries a server default
so existing rows and any insert that predates the model change both land at 0
rather than NULL.

Revision ID: e4f7a2b91c60
Revises: c7f4a1b83d95
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa


revision = "e4f7a2b91c60"
down_revision = "m7n2p5q8r1t4"
branch_labels = None
depends_on = None


TABLE = "commercial_email_verification_tokens"


def _has_column(bind, table: str, column: str) -> bool:
    """Whether the column already exists.

    This table is also created by ``app.commercial.bootstrap.ensure_tables()``
    via ``create_all``, which reads the current model — so on a database that
    booted the new code before this migration ran, the columns are already
    there. Checking makes the migration idempotent against both paths.
    """
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return True          # nothing to do; ensure_tables() will create it whole
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, TABLE, "code_hash"):
        op.add_column(TABLE, sa.Column("code_hash", sa.String(length=64), nullable=True))

    if not _has_column(bind, TABLE, "code_attempts"):
        op.add_column(
            TABLE,
            sa.Column("code_attempts", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    if "code_attempts" in existing:
        op.drop_column(TABLE, "code_attempts")
    if "code_hash" in existing:
        op.drop_column(TABLE, "code_hash")
