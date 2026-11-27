"""add commercial_idempotency_keys

Revision ID: a1b2c3d4e5f7
Revises: f0e1d2c3b4a5
Create Date: 2026-07-14 00:00:00.000000

Idempotency store for mutating commercial endpoints (Build Contracts §7.1).
Brand-new table — no existing-DB guard needed. bootstrap.ensure_tables() also
creates it via create_all(checkfirst=True) as a net; this migration is the
authoritative definition.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, Sequence[str], None] = 'f0e1d2c3b4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'commercial_idempotency_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=40), nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('response_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope', 'idempotency_key', name='uq_idempotency_scope_key'),
    )
    op.create_index(op.f('ix_commercial_idempotency_keys_id'), 'commercial_idempotency_keys', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_idempotency_keys_created_at'), 'commercial_idempotency_keys', ['created_at'], unique=False)
    op.create_index(op.f('ix_commercial_idempotency_keys_expires_at'), 'commercial_idempotency_keys', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_commercial_idempotency_keys_expires_at'), table_name='commercial_idempotency_keys')
    op.drop_index(op.f('ix_commercial_idempotency_keys_created_at'), table_name='commercial_idempotency_keys')
    op.drop_index(op.f('ix_commercial_idempotency_keys_id'), table_name='commercial_idempotency_keys')
    op.drop_table('commercial_idempotency_keys')
