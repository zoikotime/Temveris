"""add app_sessions table (desktop-tool usage for the activity timeline)

Records a point-event whenever the foreground desktop TOOL changes, written by
the tracking heartbeat. Display-only source for the activity timeline so desktop
tools (Eclipse, IDEs, Office…) appear alongside website visits. Never read by
any productivity / active-time / idle calculation.

Idempotent (CREATE … IF NOT EXISTS) so it cannot collide with a table that
bootstrap's metadata create_all may already have materialised.

Revision ID: d5e8a1c3f9b2
Revises: c1d4e7f2a9b6
Create Date: 2026-06-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e8a1c3f9b2"
down_revision: Union[str, Sequence[str], None] = "c1d4e7f2a9b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS app_sessions (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER REFERENCES users(id)          ON DELETE SET NULL,
            session_id      INTEGER REFERENCES clock_sessions(id) ON DELETE SET NULL,
            organization_id INTEGER REFERENCES organizations(id)  ON DELETE SET NULL,
            app_name        VARCHAR(255),
            window_title    TEXT,
            started_at      TIMESTAMPTZ NOT NULL,
            created_at      TIMESTAMPTZ
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_sessions_user_id ON app_sessions (user_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_sessions_organization_id ON app_sessions (organization_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_sessions_started_at ON app_sessions (started_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_sessions_user_started ON app_sessions (user_id, started_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_sessions_org_started ON app_sessions (organization_id, started_at)"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS app_sessions"))
