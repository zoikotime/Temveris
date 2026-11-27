"""add_missing_subscription_columns

Adds columns to commercial_subscriptions that were present in the model
but absent from the initial create_commercial_tables migration.

Revision ID: r2s5t8u1v4w7
Revises: a3f4c8d91e02
Create Date: 2026-05-20 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision: str = 'r2s5t8u1v4w7'
down_revision: Union[str, Sequence[str], None] = 'a3f4c8d91e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use IF NOT EXISTS so this migration is safe to run even if columns were
    # already added to production outside of Alembic.
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS trial_ending_notified_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS trial_expired_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS past_due_since TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS provider VARCHAR(40) NULL DEFAULT 'stub'"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS provider_subscription_id VARCHAR(120) NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS enterprise_override JSON NULL"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_commercial_subscriptions_provider_sub_id ON commercial_subscriptions (provider_subscription_id)"))


def downgrade() -> None:
    op.drop_index('ix_commercial_subscriptions_provider_sub_id', table_name='commercial_subscriptions')
    op.drop_column('commercial_subscriptions', 'enterprise_override')
    op.drop_column('commercial_subscriptions', 'provider_subscription_id')
    op.drop_column('commercial_subscriptions', 'provider')
    op.drop_column('commercial_subscriptions', 'past_due_since')
    op.drop_column('commercial_subscriptions', 'suspended_at')
    op.drop_column('commercial_subscriptions', 'closed_at')
    op.drop_column('commercial_subscriptions', 'cancel_requested_at')
    op.drop_column('commercial_subscriptions', 'trial_expired_at')
    op.drop_column('commercial_subscriptions', 'trial_ending_notified_at')
    op.drop_column('commercial_subscriptions', 'trial_started_at')
