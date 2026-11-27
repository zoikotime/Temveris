"""add commercial_price_quotes table + checkout_sessions.quote_id

Server-authoritative price quote (Handoff §D-05). The frontend renders a
quote by id and never recomputes money; checkout consumes the quote so the
charged amount equals the reviewed amount.

Idempotent: inspects the live DB and skips objects that already exist, so
`alembic upgrade head` is safe on both fresh and existing databases (matches
the commercial-tables migration pattern).

Revision ID: b3c6d9e2f4a8
Revises: a1b2c3d4e5f7
Create Date: 2026-07-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c6d9e2f4a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "commercial_price_quotes"


def _has_table(insp, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp, table: str, col: str) -> bool:
    return _has_table(insp, table) and col in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if not _has_table(insp, TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column("organization_id", sa.String(length=40), nullable=True),
            sa.Column("workspace_id", sa.String(length=40), nullable=True),
            sa.Column("plan_code", sa.String(length=40), nullable=False),
            sa.Column("billing_cycle", sa.String(length=20), nullable=False, server_default="monthly"),
            sa.Column("seat_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
            sa.Column("country", sa.String(length=2), nullable=True),
            sa.Column("promotion_code", sa.String(length=60), nullable=True),
            sa.Column("subtotal_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("discount_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tax_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tax_state", sa.String(length=20), nullable=False, server_default="estimated"),
            sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("renewal_amount_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("renewal_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("line_items", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False, server_default="active"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("correlation_id", sa.String(length=40), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(f"ix_{TABLE}_organization_id", TABLE, ["organization_id"])
        op.create_index(f"ix_{TABLE}_state", TABLE, ["state"])
        op.create_index(f"ix_{TABLE}_expires_at", TABLE, ["expires_at"])
        op.create_index(f"ix_{TABLE}_correlation_id", TABLE, ["correlation_id"])
        op.create_index(f"ix_{TABLE}_created_at", TABLE, ["created_at"])

    if not _has_column(insp, "commercial_checkout_sessions", "quote_id"):
        op.add_column(
            "commercial_checkout_sessions",
            sa.Column("quote_id", sa.String(length=40), nullable=True),
        )
        op.create_index(
            "ix_commercial_checkout_sessions_quote_id",
            "commercial_checkout_sessions", ["quote_id"],
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if _has_column(insp, "commercial_checkout_sessions", "quote_id"):
        op.drop_index("ix_commercial_checkout_sessions_quote_id", table_name="commercial_checkout_sessions")
        op.drop_column("commercial_checkout_sessions", "quote_id")
    if _has_table(insp, TABLE):
        op.drop_table(TABLE)
