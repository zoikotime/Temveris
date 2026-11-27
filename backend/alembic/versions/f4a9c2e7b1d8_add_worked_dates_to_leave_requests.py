"""leave_requests.worked_dates — store the days already worked for comp-off

Compensatory Leave is earned by working overtime / weekends / public holidays.
Employees now list those already-worked dates when they apply, so HR can verify
the claim. This column persists that list as a JSON array of "YYYY-MM-DD"
strings. NULL for every non-comp leave type and for rows written before the
column existed.

Uses ADD COLUMN IF NOT EXISTS so the migration is idempotent and cannot collide
with a column that bootstrap's metadata create_all may already have
materialised from the model.

Revision ID: f4a9c2e7b1d8
Revises: z9f8e7d6c5b4
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f4a9c2e7b1d8"
down_revision: Union[str, Sequence[str], None] = "z9f8e7d6c5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS worked_dates TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE leave_requests DROP COLUMN IF EXISTS worked_dates")
