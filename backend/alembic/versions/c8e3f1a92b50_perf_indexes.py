"""performance indexes for dashboard/report hot paths

Adds composite indexes that back the most frequent admin-dashboard and report
queries, replacing per-row table scans on large deployments:

  clock_sessions   (user_id, clock_in_time)   — date-range aggregates per user
  clock_sessions   (user_id, clock_out_time)  — open-session ("active") lookup
  break_sessions   (user_id, break_start)     — break aggregates per user
  break_sessions   (user_id, break_end)       — active-break ("on break") lookup
  leave_requests   (from_date, to_date)       — "leave covering today" overlap
  leave_requests   (user_id, status)          — a user's requests in a state
  audit_logs       (entity, action, created_at) — dashboard "already notified
                                                   Absent today" check

The audit_logs three-column index supersedes the old (entity, action) index
(ix_audit_entity_action), which becomes a redundant prefix, so we drop it.

Statements use IF [NOT] EXISTS (supported by both PostgreSQL and SQLite) so the
migration is idempotent and cannot collide with indexes that bootstrap's
metadata create_all may already have materialised.

Revision ID: c8e3f1a92b50
Revises: b7d2e5f10a44
Create Date: 2026-05-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c8e3f1a92b50"
down_revision: Union[str, Sequence[str], None] = "b7d2e5f10a44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index_name, table, columns) — created on upgrade, dropped on downgrade.
_NEW_INDEXES = [
    ("ix_clock_sessions_user_clock_in",  "clock_sessions", "user_id, clock_in_time"),
    ("ix_clock_sessions_user_clock_out", "clock_sessions", "user_id, clock_out_time"),
    ("ix_break_sessions_user_start",     "break_sessions", "user_id, break_start"),
    ("ix_break_sessions_user_end",       "break_sessions", "user_id, break_end"),
    ("ix_leave_requests_dates",          "leave_requests", "from_date, to_date"),
    ("ix_leave_requests_user_status",    "leave_requests", "user_id, status"),
    ("ix_audit_entity_action_created",   "audit_logs",     "entity, action, created_at"),
]


def upgrade() -> None:
    for name, table, cols in _NEW_INDEXES:
        op.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})')
    # Old two-column audit index is now a prefix of the three-column one.
    op.execute("DROP INDEX IF EXISTS ix_audit_entity_action")


def downgrade() -> None:
    # Recreate the original audit index before dropping its replacement.
    op.execute('CREATE INDEX IF NOT EXISTS ix_audit_entity_action ON audit_logs (entity, action)')
    for name, _table, _cols in _NEW_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS {name}')
