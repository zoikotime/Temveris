"""Dunning persistence: subscription anchor + sent-notice ledger

A failed payment had exactly one consequence: a red banner. No email, no grace
period, no suspension — a customer whose card failed kept full service indefinitely,
unpaid, and was never told.

Two pieces of state are needed to fix that, and neither existed:

``billing_subscriptions.dunning_started_at``
    When the current run of payment failures began. Everything else is derived from
    it: which warnings are due (day 1/3/7/14) and whether the 14-day grace period has
    expired. Deriving suspension from this timestamp rather than persisting a separate
    "suspended" flag means the two can never disagree, and clearing the anchor on
    recovery restores the organization atomically.

    Deliberately *not* stored in ``subscription_status``: that column mirrors Stripe
    and is rewritten by every webhook, so a local suspension decision parked there
    would be silently overwritten by the next event.

``billing_dunning_notices``
    One row per warning actually sent. The sweep runs hourly, so without this a
    customer would receive the day-1 email 24 times on day 1.
    ``UNIQUE (subscription_id, day_marker)`` makes that impossible at the database
    level rather than by a Python check. Rows are deleted when the anchor is cleared,
    so a *later* failure cycle can legitimately warn again.

``organization_id`` is the canonical INTEGER tenant id, carried on the notice rows so
dunning history is tenant-scoped and auditable without a join.

Adds one nullable column and one table. Backfills nothing: no organization is
retroactively placed in dunning, and no existing row changes meaning. Reversible.

Revision ID: c7f4a1b83d95
Revises: b6a3d9e52f14
Create Date: 2026-08-13 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7f4a1b83d95'
down_revision: Union[str, Sequence[str], None] = 'b6a3d9e52f14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "billing_dunning_notices"
_UQ = "uq_billing_dunning_notices_subscription_marker"


def upgrade() -> None:
    op.add_column(
        "billing_subscriptions",
        sa.Column("dunning_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Queried by the hourly sweep to find organizations currently in dunning.
    op.create_index("ix_billing_subscriptions_dunning_started_at",
                    "billing_subscriptions", ["dunning_started_at"])

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(),
                  sa.ForeignKey("billing_subscriptions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                  nullable=True),
        # day_1 | day_3 | day_7 | day_14
        sa.Column("day_marker", sa.String(20), nullable=False),
        # sent | failed — a send that errored is recorded so it is not retried forever
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'sent'")),
        sa.Column("recipient_email", sa.String(160), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Copy of the anchor this notice was sent for, for audit after a reset.
        sa.Column("dunning_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    # The duplicate-suppression guarantee, enforced by the database.
    op.create_unique_constraint(_UQ, _TABLE, ["subscription_id", "day_marker"])
    op.create_index(f"ix_{_TABLE}_organization_id", _TABLE, ["organization_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_organization_id", table_name=_TABLE)
    op.drop_constraint(_UQ, _TABLE, type_="unique")
    op.drop_table(_TABLE)
    op.drop_index("ix_billing_subscriptions_dunning_started_at",
                  table_name="billing_subscriptions")
    op.drop_column("billing_subscriptions", "dunning_started_at")
