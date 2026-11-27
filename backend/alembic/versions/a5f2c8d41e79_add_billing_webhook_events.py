"""billing_webhook_events — Stripe webhook idempotency ledger

The webhook handler had no idempotency mechanism: ``event.id`` was never read or
stored, so every Stripe re-delivery re-executed business logic. Stripe retries a
non-2xx for up to three days, and ``checkout.session.completed`` recomputed
``renewal_date`` as ``now() + 30/365 days`` on each delivery — so duplicates
silently extended paid service.

This table is the atomic claim. ``UNIQUE (stripe_event_id)`` is the *database*
guarantee, not an application check: two concurrent deliveries of the same event
both attempt the INSERT, the second one violates the constraint, and only one
claim survives. A check-then-insert in Python cannot provide that under
concurrency.

Billing-owned by design (K1): no dependency on ``app/commercial`` and no reuse of
``commercial_webhook_receipts``, so the billing webhook path never couples to the
commercial tenant model. ``organization_id`` is a plain INTEGER FK to
``organizations.id`` — the canonical tenant identity — and stays NULL for events
whose tenant cannot be safely resolved (the handler must never guess a tenant).

``payload`` stores a redacted subset for diagnostics only. Reprocessing re-fetches
authoritative state from Stripe (K2) rather than replaying a stale payload, so the
payload is never a source of truth and sensitive fields are stripped before it is
written.

Creates one table. Alters nothing, backfills nothing, touches no Stripe resource.
Fully reversible.

Revision ID: a5f2c8d41e79
Revises: f3d0b6e29a17
Create Date: 2026-08-11 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a5f2c8d41e79'
down_revision: Union[str, Sequence[str], None] = 'f3d0b6e29a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "billing_webhook_events"
_UQ_EVENT_ID = "uq_billing_webhook_events_stripe_event_id"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),

        # The idempotency key. NOT NULL + UNIQUE: an event with no id must never
        # be claimable, and an id must never be claimable twice.
        sa.Column("stripe_event_id", sa.String(160), nullable=False),

        sa.Column("event_type", sa.String(80), nullable=False),
        # Stripe's API version for this event — the same event type can carry a
        # different shape across versions, so a stored diagnosis needs it.
        sa.Column("api_version", sa.String(40), nullable=True),
        # event.created at Stripe. Recorded for diagnosing out-of-order delivery;
        # it is deliberately NOT used as a watermark (K2 — Stripe re-fetch).
        sa.Column("stripe_created_at", sa.DateTime(timezone=True), nullable=True),

        # NULL when the tenant could not be resolved safely. SET NULL on delete so
        # removing an organization never destroys the processing ledger.
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),

        # received | processed | failed | dead_letter  (K5)
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'received'")),
        sa.Column("attempts", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),

        # Redacted diagnostic copy only — never replayed as truth.
        sa.Column("payload", sa.JSON(), nullable=True),

        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The atomic claim. A named UNIQUE constraint rather than a unique index so
    # ON CONFLICT / IntegrityError handling has a stable, assertable name.
    op.create_unique_constraint(_UQ_EVENT_ID, _TABLE, ["stripe_event_id"])

    # Operational lookups: dead-letter sweeps, per-tenant history, retry queues.
    op.create_index(f"ix_{_TABLE}_status", _TABLE, ["status"])
    op.create_index(f"ix_{_TABLE}_event_type", _TABLE, ["event_type"])
    op.create_index(f"ix_{_TABLE}_organization_id", _TABLE, ["organization_id"])
    op.create_index(f"ix_{_TABLE}_received_at", _TABLE, ["received_at"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_received_at", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_organization_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_event_type", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_status", table_name=_TABLE)
    op.drop_constraint(_UQ_EVENT_ID, _TABLE, type_="unique")
    op.drop_table(_TABLE)
