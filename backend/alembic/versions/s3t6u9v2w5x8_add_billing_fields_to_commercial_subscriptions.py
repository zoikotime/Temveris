"""add_billing_fields_to_commercial_subscriptions

Adds payment_risk, renewal_date, active_users to commercial_subscriptions.
Seeds billing_plans with Verified / Governed / Sovereign tier pricing if
those plan codes do not already exist.

Revision ID: s3t6u9v2w5x8
Revises: r2s5t8u1v4w7
Create Date: 2026-05-26 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 's3t6u9v2w5x8'
down_revision: Union[str, Sequence[str], None] = 'r2s5t8u1v4w7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PLAN_SEEDS = [
    {
        "code": "verified",
        "name": "Verified",
        "description": "Core workforce intelligence for growing teams.",
        "monthly_price_per_user": 8.0,
        "annual_price_per_user": 6.4,
        "annual_discount_percentage": 20.0,
        "tier_order": 1,
        "is_most_popular": False,
        "is_enterprise": False,
        "trial_enabled": True,
        "active_status": True,
        "currency": "USD",
    },
    {
        "code": "governed",
        "name": "Governed",
        "description": "Advanced compliance and governance for regulated industries.",
        "monthly_price_per_user": 16.0,
        "annual_price_per_user": 12.8,
        "annual_discount_percentage": 20.0,
        "tier_order": 2,
        "is_most_popular": True,
        "is_enterprise": False,
        "trial_enabled": True,
        "active_status": True,
        "currency": "USD",
    },
    {
        "code": "sovereign",
        "name": "Sovereign",
        "description": "Private architecture for sovereign data requirements.",
        "monthly_price_per_user": 28.0,
        "annual_price_per_user": 22.4,
        "annual_discount_percentage": 20.0,
        "tier_order": 3,
        "is_most_popular": False,
        "is_enterprise": False,
        "trial_enabled": False,
        "active_status": True,
        "currency": "USD",
    },
]


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS payment_risk VARCHAR(20) NOT NULL DEFAULT 'low'"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS renewal_date TIMESTAMPTZ NULL"))
    op.execute(sa.text("ALTER TABLE commercial_subscriptions ADD COLUMN IF NOT EXISTS active_users INTEGER NOT NULL DEFAULT 0"))

    conn = op.get_bind()
    for plan in _PLAN_SEEDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM billing_plans WHERE code = :code LIMIT 1"),
            {"code": plan["code"]},
        ).fetchone()
        if not exists:
            conn.execute(
                sa.text("""
                    INSERT INTO billing_plans
                        (code, name, description,
                         monthly_price_per_user, annual_price_per_user,
                         annual_discount_percentage, tier_order,
                         is_most_popular, is_enterprise, trial_enabled,
                         active_status, currency, created_at, updated_at)
                    VALUES
                        (:code, :name, :description,
                         :monthly_price_per_user, :annual_price_per_user,
                         :annual_discount_percentage, :tier_order,
                         :is_most_popular, :is_enterprise, :trial_enabled,
                         :active_status, :currency, NOW(), NOW())
                """),
                plan,
            )


def downgrade() -> None:
    op.drop_column("commercial_subscriptions", "active_users")
    op.drop_column("commercial_subscriptions", "renewal_date")
    op.drop_column("commercial_subscriptions", "payment_risk")
