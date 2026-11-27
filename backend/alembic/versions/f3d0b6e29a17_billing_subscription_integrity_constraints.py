"""billing_subscriptions integrity: one per org, one per Stripe subscription

Two constraints the multi-tenant model relies on but the schema never enforced:

1. ``UNIQUE (organization_id)`` — exactly one legacy billing subscription per
   organization. Without it, ``ensure_subscription`` racing with itself (two
   concurrent requests from the same tenant) can create a second row, and the
   tenant then sees a different plan depending on which row ``.first()`` returns.

2. ``UNIQUE (stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL``
   — the webhook handler resolves subscriptions by this id
   (``customer.subscription.updated``/``.deleted``, ``invoice.paid``,
   ``invoice.payment_failed``). If two rows ever carried the same id, a webhook
   would silently update whichever row came back first. A partial index is used
   so the many rows with NULL remain allowed.

Guards: both constraints are checked against live data first and the migration
raises rather than creating them if duplicates exist. Duplicates are never
merged or deleted automatically — resolving them is a business decision about
which subscription is authoritative.

Runs after e2c9a4d81f63, so organization_id is already INTEGER by this point.
Touches no Stripe resource and modifies no row.

Revision ID: f3d0b6e29a17
Revises: e2c9a4d81f63
Create Date: 2026-08-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f3d0b6e29a17'
down_revision: Union[str, Sequence[str], None] = 'e2c9a4d81f63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ_ORG = "uq_billing_subscriptions_organization_id"
_UQ_STRIPE_SUB = "uq_billing_subscriptions_stripe_subscription_id"


def upgrade() -> None:
    conn = op.get_bind()

    # ── Guard 1: one subscription per organization ────────────────────────────
    dupes = conn.execute(
        sa.text(
            """
            SELECT organization_id, COUNT(*) AS n
              FROM billing_subscriptions
             WHERE organization_id IS NOT NULL
             GROUP BY organization_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if dupes:
        raise RuntimeError(
            "billing_subscriptions has multiple subscriptions for the same "
            f"organization: {[(r[0], r[1]) for r in dupes]!r}. Decide which row "
            "is authoritative before adding UNIQUE(organization_id) — this "
            "migration will not merge or delete billing records."
        )

    # ── Guard 2: one row per Stripe subscription ─────────────────────────────
    stripe_dupes = conn.execute(
        sa.text(
            """
            SELECT stripe_subscription_id, COUNT(*) AS n
              FROM billing_subscriptions
             WHERE stripe_subscription_id IS NOT NULL
             GROUP BY stripe_subscription_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if stripe_dupes:
        raise RuntimeError(
            "billing_subscriptions maps one Stripe subscription to multiple "
            f"rows: {[(r[0], r[1]) for r in stripe_dupes]!r}. Webhook routing is "
            "ambiguous until this is resolved; no rows will be changed here."
        )

    op.create_unique_constraint(_UQ_ORG, "billing_subscriptions", ["organization_id"])

    # Partial unique index — NULL stripe_subscription_id must stay repeatable.
    op.create_index(
        _UQ_STRIPE_SUB,
        "billing_subscriptions",
        ["stripe_subscription_id"],
        unique=True,
        postgresql_where=sa.text("stripe_subscription_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_UQ_STRIPE_SUB, table_name="billing_subscriptions")
    op.drop_constraint(_UQ_ORG, "billing_subscriptions", type_="unique")
