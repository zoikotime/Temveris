"""activities.interval_seconds — store the tracker's real per-tick length

The active-time aggregators (`/me/day-summary`, `/employees/team`) used to
reconstruct each Activity row's duration from the configured
`screenshot_interval` setting. That over-credited every "last" row (and any
sparsely-spaced row) the full configured interval — so an employee with a
single recent active tick showed exactly `screenshot_interval` seconds of
active time (the "every employee shows 300/360" bug). The tracker already
reports the true wall-clock tick length as `interval_seconds`; this column
persists it so the aggregators can use the real value instead.

NULL for legacy rows written before this column existed — the aggregators
fall back to the old configured-interval/gap heuristic for those.

Uses ADD COLUMN IF NOT EXISTS so the migration is idempotent and cannot
collide with a column that bootstrap's metadata create_all may already have
materialised from the model.

Revision ID: f3b8c2a91d07
Revises: e1f7a2c4d093
Create Date: 2026-06-03
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f3b8c2a91d07"
down_revision: Union[str, Sequence[str], None] = "e1f7a2c4d093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS interval_seconds INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE activities DROP COLUMN IF EXISTS interval_seconds")
