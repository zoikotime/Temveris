"""break governance — entitlement enforcement + overrun provenance

Adds the columns the Break Governance & Workday Provenance spec needs on
``break_sessions`` so the platform can:

  * snapshot the worker's employment type + the allowance that applied when
    a break started (``worker_type``, ``allowed_minutes``) — frozen so reports
    and audit records stay stable if policy/type changes later;
  * track the overrun lifecycle (``status`` active → overdue → ended,
    ``allowance_reached_at``, ``overrun_flag``); and
  * throttle the HR escalation emails (``overrun_notification_sent``,
    ``overrun_notified_at``, ``last_alert_at``, ``alert_count``) — first alert
    at the threshold, then a reminder every 30 minutes while the break stays
    active.

The immutable provenance events themselves (BREAK_START_REQUESTED,
BREAK_STARTED, BREAK_DENIED, BREAK_ALLOWANCE_REACHED, BREAK_OVERRUN,
BREAK_OVERRUN_NOTIFICATION_SENT, BREAK_ENDED) reuse the existing append-only
``work_events`` + ``work_evidence_chain`` tables — no new table is needed.

Uses ADD COLUMN IF NOT EXISTS so the migration is idempotent and cannot
collide with a column bootstrap's metadata create_all may already have
materialised from the model.

Revision ID: a9c4e7b21f30
Revises: f3b8c2a91d07
Create Date: 2026-06-03
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a9c4e7b21f30"
down_revision: Union[str, Sequence[str], None] = "f3b8c2a91d07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS worker_type VARCHAR(16)")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS allowed_minutes INTEGER")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS allowance_reached_at TIMESTAMPTZ")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS overrun_flag BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS overrun_notification_sent BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS overrun_notified_at TIMESTAMPTZ")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS last_alert_at TIMESTAMPTZ")
    op.execute("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS alert_count INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS alert_count")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS last_alert_at")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS overrun_notified_at")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS overrun_notification_sent")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS overrun_flag")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS allowance_reached_at")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS allowed_minutes")
    op.execute("ALTER TABLE break_sessions DROP COLUMN IF EXISTS worker_type")
