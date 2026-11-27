"""align billing_plans with the canonical price catalog (ZT-COM-BILL-001 §13)

Corrects commercial drift in the plan catalog. Production held Verified
$9.00 / $86.40 and Governed annual $144.00, while the confirmed commercial
standard is:

    Verified   $8/user/month    $79/user/year    cap 25
    Governed   $15/user/month   $149/user/year   cap 100
    Sovereign  $25/user/month   $240/user/year   unlimited

Enterprise is intentionally untouched — it carries no fixed self-service price.

Data-only: no schema is altered, nothing is dropped, no billing records are
deleted. Idempotent, so re-running is a no-op once values already match.

Revision ID: c7e4b1a93f52
Revises: f1a9d4c72e86
Create Date: 2026-08-07 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7e4b1a93f52'
down_revision: Union[str, Sequence[str], None] = 'f1a9d4c72e86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# code, monthly_price_per_user, annual_price_per_user, user_limit
_CANONICAL_PLANS = (
    ("verified", 8.0, 79.0, 25),
    ("governed", 15.0, 149.0, 100),
    ("sovereign", 25.0, 240.0, None),
)

_UPDATE = sa.text(
    """
    UPDATE billing_plans
       SET monthly_price_per_user = :monthly,
           annual_price_per_user  = :annual,
           user_limit             = :user_limit,
           updated_at             = CURRENT_TIMESTAMP
     WHERE code = :code
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    for code, monthly, annual, user_limit in _CANONICAL_PLANS:
        conn.execute(
            _UPDATE,
            {"code": code, "monthly": monthly, "annual": annual, "user_limit": user_limit},
        )


def downgrade() -> None:
    """Intentionally a no-op.

    The previous values ($9.00 / $86.40 Verified, $144.00 Governed annual) were
    the drift this migration exists to remove. Re-introducing them on downgrade
    would put incorrect prices back in front of customers and out of step with
    Stripe. If a price genuinely needs to change, add a new forward migration
    with the new canonical catalog instead.
    """
    pass
