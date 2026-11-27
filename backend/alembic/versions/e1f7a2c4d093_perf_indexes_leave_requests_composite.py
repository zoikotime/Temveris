"""performance index: leave_requests 4-column composite

Adds the composite index that backs the most common report/dashboard predicate
against leave requests: "this user, in this date window, in these statuses".

  leave_requests (user_id, from_date, to_date, status)

The existing two indexes (from_date, to_date) and (user_id, status) handle
the "leave covering today" overlap scan and the "this user's pending
requests" lookup respectively, but neither can serve a query that fixes
user_id and filters on the date range simultaneously — those queries fall
back to a Seq Scan + filter on large tables. The 4-column composite lets
Postgres satisfy the full predicate from the index.

Statements use IF NOT EXISTS so the migration is idempotent and cannot
collide with indexes that bootstrap's metadata create_all may already have
materialised.

Revision ID: e1f7a2c4d093
Revises: d4b2a8c6e081
Create Date: 2026-05-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e1f7a2c4d093"
down_revision: Union[str, Sequence[str], None] = "d4b2a8c6e081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_leave_requests_user_dates_status"
_TABLE = "leave_requests"
_COLS = "user_id, from_date, to_date, status"


def upgrade() -> None:
    op.execute(f'CREATE INDEX IF NOT EXISTS {_INDEX_NAME} ON {_TABLE} ({_COLS})')


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {_INDEX_NAME}')
