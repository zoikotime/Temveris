"""add users.billing_state and backfill from legacy flags (ZT-COM-BILL-001 §17)

Introduces the authoritative worker billing classification. The legacy booleans
cannot express the policy:

* ``is_invited`` is never cleared on acceptance — in production all 21 org-1
  users carrying ``is_invited=True`` have login *and* clock-in history, so
  treating the flag as "pending" under-billed by 21 seats.
* ``is_active=False`` conflates suspended (billable, §A4) with
  deactivated/terminated/archived (not billable).

Backfill rules (evidence-based, deliberately conservative):

  is_active AND (has login OR has clock-in)        -> ACTIVE_BILLABLE
  is_active AND no activity AND (invited OR token) -> INVITED_PENDING
  everything else                                  -> MIGRATION_REVIEW

MIGRATION_REVIEW is NOT billable, so no unresolved row can inflate an invoice.

Four accounts could not be resolved from the data (three inactive with differing
activity histories, one active but never used). They were classified by the
business owner on 2026-08-09 and are applied here as an explicit override, keyed
by email so the decision transports across environments:

  user1@example.invalid  -> DEACTIVATED  (left; 90 clock-ins, ended 2026-07-24)
  user2@example.invalid  -> DEACTIVATED  (never used the product)
  user3@example.invalid  -> DEACTIVATED  (test account)
  user4@example.invalid  -> DEACTIVATED  (test account; is_active=true but unused)

All four are non-billable. Suspended vs deactivated vs terminated is never
inferred from the legacy booleans — it is recorded, or it stays in review.

Additive only — no data is deleted and no existing column is modified.

Revision ID: d1b8f3a67c25
Revises: c7e4b1a93f52
Create Date: 2026-08-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1b8f3a67c25'
down_revision: Union[str, Sequence[str], None] = 'c7e4b1a93f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Depends on user_sessions / clock_sessions, both created earlier in the chain.
_BACKFILL = sa.text(
    """
    UPDATE users SET billing_state = CASE
        WHEN is_active
             AND (EXISTS (SELECT 1 FROM user_sessions  s WHERE s.user_id = users.id)
               OR EXISTS (SELECT 1 FROM clock_sessions c WHERE c.user_id = users.id))
            THEN 'ACTIVE_BILLABLE'
        WHEN is_active
             AND (is_invited OR invite_token IS NOT NULL)
            THEN 'INVITED_PENDING'
        ELSE 'MIGRATION_REVIEW'
    END
    WHERE billing_state IS NULL
    """
)


# Business-owner classifications for accounts the data cannot resolve.
# Keyed by email (unique and stable across environments) rather than by id.
# A row that is absent in a given environment simply updates nothing.
_EXPLICIT_CLASSIFICATIONS = {
    "user1@example.invalid": "DEACTIVATED",
    "user2@example.invalid": "DEACTIVATED",
    "user3@example.invalid": "DEACTIVATED",
    "user4@example.invalid": "DEACTIVATED",
}

_OVERRIDE = sa.text("UPDATE users SET billing_state = :state WHERE email = :email")


def upgrade() -> None:
    op.add_column('users', sa.Column('billing_state', sa.String(length=24), nullable=True))
    op.create_index('ix_users_billing_state', 'users', ['billing_state'], unique=False)

    conn = op.get_bind()
    conn.execute(_BACKFILL)

    # Applied after the rule-based pass so a recorded decision always wins.
    for email, state in _EXPLICIT_CLASSIFICATIONS.items():
        conn.execute(_OVERRIDE, {"state": state, "email": email})


def downgrade() -> None:
    op.drop_index('ix_users_billing_state', table_name='users')
    op.drop_column('users', 'billing_state')
