"""leave balance management: employee_leave_balances + leave_balance_adjustments

Adds the two tables behind the HR "Leave Balance Management" module:

  • employee_leave_balances   — per-(employee, leave_type) allocation OVERRIDE.
                                 Presence of a row means "use this allocated_days
                                 instead of the policy default". `used` is never
                                 stored; it stays derived from leave_requests, so
                                 remaining = allocated_days - derived_used.
  • leave_balance_adjustments — immutable audit trail of every Grant / Deduct /
                                 Reset / Custom action (who, when, why, before/after).

Both are keyed by the leave_type STRING (not a numeric id) so any current or
future leave type — including custom types HR creates at runtime — is supported
with no further migration.

Additive and reversible. Idempotent (CREATE TABLE / INDEX IF NOT EXISTS) so it
cannot collide with a table a bootstrap create_all may already have materialised.

Revision ID: b7d2e1a9c3f4
Revises: f2a9c4e7b1d3
Create Date: 2026-06-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d2e1a9c3f4"
down_revision: Union[str, Sequence[str], None] = "f2a9c4e7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        """
        CREATE TABLE IF NOT EXISTS employee_leave_balances (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
            leave_type      VARCHAR(100) NOT NULL,
            allocated_days  DOUBLE PRECISION NOT NULL DEFAULT 0,
            updated_at      TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_employee_leave_balances_user_type UNIQUE (user_id, leave_type)
        )
        """
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_employee_leave_balances_user_id "
        "ON employee_leave_balances (user_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_employee_leave_balances_org "
        "ON employee_leave_balances (organization_id)"
    ))

    op.execute(sa.text(
        """
        CREATE TABLE IF NOT EXISTS leave_balance_adjustments (
            id                  SERIAL PRIMARY KEY,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            organization_id     INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
            leave_type          VARCHAR(100) NOT NULL,
            adjustment_type     VARCHAR(20) NOT NULL,
            adjustment_value    DOUBLE PRECISION,
            previous_allocation DOUBLE PRECISION,
            new_allocation      DOUBLE PRECISION,
            reason              TEXT,
            effective_date      TIMESTAMP,
            created_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at          TIMESTAMPTZ DEFAULT now()
        )
        """
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_leave_balance_adjustments_user_id "
        "ON leave_balance_adjustments (user_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_leave_balance_adjustments_user_type "
        "ON leave_balance_adjustments (user_id, leave_type)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_leave_balance_adjustments_created_at "
        "ON leave_balance_adjustments (created_at)"
    ))


def downgrade() -> None:
    # Postgres drops a table's indexes with the table, so explicit DROP INDEX
    # calls aren't needed here.
    op.execute(sa.text("DROP TABLE IF EXISTS leave_balance_adjustments"))
    op.execute(sa.text("DROP TABLE IF EXISTS employee_leave_balances"))
