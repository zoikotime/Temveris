"""harden password_reset_otps: store hashed OTP + owning employee_id

The reset-OTP table previously stored the 4-digit code in plaintext (otp_code)
and was keyed only by email. This migration:
  • adds otp_hash      — HMAC-SHA256 of the code; the code itself is never stored
  • adds employee_id   — FK to users(id), so resets join back to the account
  • relaxes otp_code NOT NULL so new rows can be hash-only (column kept for
    back-compat / clean downgrade; no longer written by the app)

Additive and reversible. OTP rows are ephemeral (5-minute TTL), so any row
written before this runs simply fails to match (hash compare) and the user
re-requests a code — no data migration is needed.

Idempotent (ADD COLUMN IF NOT EXISTS) so it cannot collide with a table that
bootstrap's metadata create_all may already have materialised.

Revision ID: f2a9c4e7b1d3
Revises: d5e8a1c3f9b2
Create Date: 2026-06-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a9c4e7b1d3"
down_revision: Union[str, Sequence[str], None] = "d5e8a1c3f9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE password_reset_otps ADD COLUMN IF NOT EXISTS otp_hash VARCHAR(255)"
    ))
    op.execute(sa.text(
        "ALTER TABLE password_reset_otps ADD COLUMN IF NOT EXISTS employee_id INTEGER "
        "REFERENCES users(id) ON DELETE CASCADE"
    ))
    # New rows are hash-only — drop the legacy NOT NULL so they can omit otp_code.
    op.execute(sa.text(
        "ALTER TABLE password_reset_otps ALTER COLUMN otp_code DROP NOT NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_password_reset_otps_employee_id "
        "ON password_reset_otps (employee_id)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_password_reset_otps_employee_id"))
    op.execute(sa.text("ALTER TABLE password_reset_otps DROP COLUMN IF EXISTS employee_id"))
    op.execute(sa.text("ALTER TABLE password_reset_otps DROP COLUMN IF EXISTS otp_hash"))
    # otp_code is intentionally left nullable on downgrade: restoring NOT NULL
    # could fail if hash-only rows (otp_code IS NULL) still exist.
