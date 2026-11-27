"""WEC hash-chain tail index (user_id, id DESC)

The Work Evidence Chain is append-only and every governed break/clock action
appends an entry. Before appending, `app.wec.service._get_previous_hash` reads
the user's latest row to chain off its hash:

    SELECT ... FROM work_evidence_chain
    WHERE user_id = :uid ORDER BY id DESC LIMIT 1

The existing index `ix_wec_user_ts` is on (user_id, timestamp), which Postgres
cannot fully use for an `ORDER BY id DESC` — it filters by user_id then sorts to
find the max id, a cost that grows as the chain grows. This adds an index that
matches the access pattern exactly so the lookup is a single index-tail seek
regardless of chain length.

Statement uses IF [NOT] EXISTS so it is idempotent across PostgreSQL and SQLite.

Revision ID: f7a3c9d21e64
Revises: b2f9c7a41d58
Create Date: 2026-06-10
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f7a3c9d21e64"
down_revision: Union[str, Sequence[str], None] = "b2f9c7a41d58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wec_user_id_desc "
        "ON work_evidence_chain (user_id, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_wec_user_id_desc")
