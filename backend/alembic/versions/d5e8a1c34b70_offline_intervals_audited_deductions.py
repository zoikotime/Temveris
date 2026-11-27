"""offline intervals — bound + audit the network-pause deduction

Background
----------
``POST /employees/network-pause`` accepted an arbitrary ``offline_start`` /
``offline_end`` from the client and did::

    session.break_seconds = (session.break_seconds or 0) + seconds

Three defects compounded:

  * **no upper bound** — clamped only to the session, so a single call could
    erase a whole day (observed: 73 sessions in 90 days losing up to 8h20m,
    one credited 3 seconds of work for a 7-hour session);
  * **raw ``+=``** — no idempotency and no interval union, so a retrying
    client inflated the deduction without limit (the same failure mode already
    fixed for breaks in ``_actual_break_seconds_for_sessions``);
  * **no audit trail** — nothing recorded when, why or how much was deducted,
    so it could not be explained or reversed.

Additionally the deduction was written into ``break_seconds``, conflating
"time the worker chose to take" with "time the tracker could not observe" —
which polluted break analytics/governance and made the loss invisible in the
dashboards (they render raw elapsed time labelled "Worked for").

This migration
--------------
  * ``clock_sessions.offline_seconds`` — connectivity gaps, kept separate from
    ``break_seconds``. Both are subtracted from ``total_seconds``.
  * ``offline_intervals`` — one auditable row per accepted deduction, storing
    the claimed vs applied seconds and whether a cap was hit.

Additive and idempotent (``IF NOT EXISTS`` throughout). Nothing is rewritten:
legacy rows keep their merged value in ``break_seconds`` and start at
``offline_seconds = 0``, so no historical total changes when this is applied.
Repairing the already-damaged sessions is a separate, deliberate backfill.

Revision ID: d5e8a1c34b70
Revises: b3c6d9e2f4a8
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d5e8a1c34b70"
down_revision: Union[str, Sequence[str], None] = "b3c6d9e2f4a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get 0 via server_default, so no total changes on apply.
    op.execute(
        "ALTER TABLE clock_sessions "
        "ADD COLUMN IF NOT EXISTS offline_seconds INTEGER NOT NULL DEFAULT 0"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_intervals (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id)          ON DELETE CASCADE,
            session_id      INTEGER NOT NULL REFERENCES clock_sessions(id) ON DELETE CASCADE,
            organization_id INTEGER          REFERENCES organizations(id)  ON DELETE SET NULL,
            offline_start   TIMESTAMPTZ NOT NULL,
            offline_end     TIMESTAMPTZ NOT NULL,
            claimed_seconds INTEGER NOT NULL DEFAULT 0,
            applied_seconds INTEGER NOT NULL DEFAULT 0,
            capped          BOOLEAN NOT NULL DEFAULT false,
            cap_reason      VARCHAR(32),
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_offline_intervals_session_start "
        "ON offline_intervals (session_id, offline_start)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_offline_intervals_user_start "
        "ON offline_intervals (user_id, offline_start)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_offline_intervals_org "
        "ON offline_intervals (organization_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS offline_intervals")
    op.execute("ALTER TABLE clock_sessions DROP COLUMN IF EXISTS offline_seconds")
