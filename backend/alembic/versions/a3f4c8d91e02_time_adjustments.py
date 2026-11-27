"""time_adjustments: admin-recorded credits for lost work time

Adds the `time_adjustments` table that backs the Workspace Settings
"Time Adjustments" tab. Each row is an admin-recorded credit added to
an employee's day to compensate for genuinely-worked time the tracker
lost (power cut, system crash, network outage, etc.). The original
ClockSession rows stay immutable; daily/weekly reports SUM in the
adjustments via the API layer.

Revision ID: a3f4c8d91e02
Revises: 7e91a4c0b321
Create Date: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f4c8d91e02"
down_revision: Union[str, Sequence[str], None] = "c37cc72db12d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "time_adjustments",
        sa.Column("id",              sa.Integer(), primary_key=True),
        sa.Column("user_id",         sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adjustment_date", sa.Date(),    nullable=False),
        sa.Column("seconds_added",   sa.Integer(), nullable=False),
        sa.Column("reason",          sa.Text(),    nullable=False),
        sa.Column("created_by",      sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_active",       sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("reverted_by",     sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reverted_at",     sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_time_adjustments_user_id",       "time_adjustments", ["user_id"])
    op.create_index("ix_time_adjustments_adjustment_date","time_adjustments", ["adjustment_date"])
    op.create_index("ix_time_adjustments_created_by",    "time_adjustments", ["created_by"])
    op.create_index("ix_time_adjustments_created_at",    "time_adjustments", ["created_at"])
    op.create_index("ix_time_adjustments_is_active",     "time_adjustments", ["is_active"])
    op.create_index("ix_time_adjustments_user_date",     "time_adjustments", ["user_id", "adjustment_date"])


def downgrade() -> None:
    op.drop_index("ix_time_adjustments_user_date",      table_name="time_adjustments")
    op.drop_index("ix_time_adjustments_is_active",      table_name="time_adjustments")
    op.drop_index("ix_time_adjustments_created_at",     table_name="time_adjustments")
    op.drop_index("ix_time_adjustments_created_by",     table_name="time_adjustments")
    op.drop_index("ix_time_adjustments_adjustment_date",table_name="time_adjustments")
    op.drop_index("ix_time_adjustments_user_id",        table_name="time_adjustments")
    op.drop_table("time_adjustments")
