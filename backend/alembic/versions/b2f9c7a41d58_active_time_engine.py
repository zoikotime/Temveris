"""active time engine — materialised window / minute / daily activity scores

Adds the three tables the redesigned Active Time Engine writes to so the
"Active Time %" is computed once, server-side, and stored — instead of being
re-derived on every request from the old single-OR ``is_active`` boolean that
saturated to 100%.

  * ``activity_windows``        — one scored row per screenshot interval
  * ``activity_minute_scores``  — one classified row per minute
  * ``activity_daily_scores``   — one cached authoritative row per (user, day)

All scoring lives in ``app/active_time/engine.py``; these tables only persist
results. See ``docs/active-time-engine-analysis.md``.

Uses CREATE TABLE / INDEX IF NOT EXISTS so the migration is idempotent and
cannot collide with tables bootstrap's metadata create_all may already have
materialised from the models.

Revision ID: b2f9c7a41d58
Revises: a9c4e7b21f30
Create Date: 2026-06-05
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b2f9c7a41d58"
down_revision: Union[str, Sequence[str], None] = "a9c4e7b21f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_windows (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
            session_id       INTEGER REFERENCES clock_sessions(id) ON DELETE SET NULL,
            activity_id      INTEGER REFERENCES activities(id) ON DELETE SET NULL,
            window_start     TIMESTAMPTZ NOT NULL,
            window_end       TIMESTAMPTZ NOT NULL,
            interval_seconds INTEGER NOT NULL DEFAULT 0,
            key_presses      INTEGER DEFAULT 0,
            mouse_clicks     INTEGER DEFAULT 0,
            mouse_moves      INTEGER DEFAULT 0,
            mouse_scrolls    INTEGER DEFAULT 0,
            mouse_events     INTEGER DEFAULT 0,
            active_seconds   INTEGER DEFAULT 0,
            idle_seconds     INTEGER DEFAULT 0,
            app_name         VARCHAR(255),
            has_screenshot   BOOLEAN DEFAULT false,
            hook_active      BOOLEAN DEFAULT true,
            on_break         BOOLEAN DEFAULT false,
            keyboard_score       DOUBLE PRECISION DEFAULT 0,
            mouse_score          DOUBLE PRECISION DEFAULT 0,
            focus_score          DOUBLE PRECISION DEFAULT 0,
            continuity_score     DOUBLE PRECISION DEFAULT 0,
            screenshot_score     DOUBLE PRECISION DEFAULT 0,
            app_engagement_score DOUBLE PRECISION DEFAULT 0,
            score                DOUBLE PRECISION DEFAULT 0,
            label            VARCHAR(20) DEFAULT 'idle',
            fully_active     BOOLEAN DEFAULT false,
            flags            JSONB,
            active_credit_seconds INTEGER DEFAULT 0,
            created_at       TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_windows_user_start ON activity_windows (user_id, window_start)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_windows_start ON activity_windows (window_start)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_minute_scores (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
            window_id      INTEGER REFERENCES activity_windows(id) ON DELETE SET NULL,
            minute_start   TIMESTAMPTZ NOT NULL,
            classification VARCHAR(20) DEFAULT 'IDLE',
            score          DOUBLE PRECISION DEFAULT 0,
            active_seconds INTEGER DEFAULT 0,
            on_break       BOOLEAN DEFAULT false,
            created_at     TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_activity_minute_user_minute "
        "ON activity_minute_scores (user_id, minute_start)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_daily_scores (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            day             TIMESTAMPTZ NOT NULL,
            worked_seconds  INTEGER DEFAULT 0,
            active_seconds  INTEGER DEFAULT 0,
            idle_seconds    INTEGER DEFAULT 0,
            break_seconds   INTEGER DEFAULT 0,
            active_pct      DOUBLE PRECISION DEFAULT 0,
            raw_active_pct  DOUBLE PRECISION DEFAULT 0,
            coverage_ratio  DOUBLE PRECISION DEFAULT 0,
            minutes_active  INTEGER DEFAULT 0,
            minutes_partial INTEGER DEFAULT 0,
            minutes_low     INTEGER DEFAULT 0,
            minutes_idle    INTEGER DEFAULT 0,
            window_count    INTEGER DEFAULT 0,
            flags           JSONB,
            finalized_through TIMESTAMPTZ,
            computed_at     TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_activity_daily_user_day "
        "ON activity_daily_scores (user_id, day)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_daily_day ON activity_daily_scores (day)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS activity_minute_scores")
    op.execute("DROP TABLE IF EXISTS activity_daily_scores")
    op.execute("DROP TABLE IF EXISTS activity_windows")
