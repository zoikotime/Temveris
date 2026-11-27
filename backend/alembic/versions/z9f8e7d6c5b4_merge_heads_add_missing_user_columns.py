"""merge all heads and add missing users columns

Revision ID: z9f8e7d6c5b4
Revises: t4u7v0w3x6y9, c1d2e3f4a5b6, f7a3c9d21e64
Create Date: 2026-06-11 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'z9f8e7d6c5b4'
down_revision: Union[str, Sequence[str], None] = ('t4u7v0w3x6y9', 'c1d2e3f4a5b6', 'f7a3c9d21e64')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns that exist in the model but were missing from initial_setup
    # when production was first deployed. IF NOT EXISTS makes this safe to
    # run on environments that already have the column.
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_clock_in_time VARCHAR(5) NULL"))
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_clock_in_permanent BOOLEAN NULL"))
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_clock_in_grace_min INTEGER NULL"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS default_clock_in_grace_min"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS default_clock_in_permanent"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS default_clock_in_time"))
