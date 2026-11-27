"""durable actor attribution on config_version

Revision ID: e5f8c2a91d64
Revises: c9d4a7e2f581
Create Date: 2026-09-07 00:00:00.000000

Adds ONE column:

    config_version.captured_by_label   String(64), NOT NULL, default 'SYSTEM'

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
Owner ruling F: threshold configuration history MUST preserve durable
attribution even after the original user's row is deleted.

``config_version.captured_by_user_id`` cannot satisfy that, for two independent
reasons:

* it is ``ondelete="SET NULL"`` — deliberately, so that removing a person never
  removes configuration history. The consequence is that it stops answering
  "who set this" the moment that person is deleted;
* a database-level SET NULL bypasses the ORM immutability listeners entirely,
  so the column's immutability guarantee does not extend to that path.

Owner ruling E additionally requires an explicit ``SYSTEM`` actor for an
authorized system capture. ``captured_by_user_id`` is an INTEGER foreign key to
``users.id``; there is no value in its domain that means SYSTEM. So the sentinel
needs somewhere to live regardless.

PRECEDENT
---------
``rollout.RolloutControl.last_transition_by`` — ``String(64)``, NOT NULL,
default ``"SYSTEM"``, documented as "an id where one exists, SYSTEM otherwise".
The same shape is used here rather than a second pattern being invented, and
``billing`` sets the same precedent for denormalising attribution onto the row
it describes.

WHAT IT IS NOT
--------------
Not a personal-data column. It holds an identifying label, not a profile: no
name, no email, no role history. Ruling F asks for attribution sufficient to
identify the original actor, and nothing further.

Not part of the configuration content hash. ``compute_content_hash`` covers
identity plus values and deliberately excludes ``captured_at_utc`` and
``captured_by_user_id`` because provenance about a capture is not configuration
— including it would make an identical configuration hash differently depending
on who recorded it. This column sits on the same side of that line, so the
hashing function is untouched by this revision.

PURELY ADDITIVE
---------------
One ``add_column``. No table created, altered in any other respect, renamed or
dropped. No index, no constraint, no default row. ``work_day_ledger``,
``work_state_interval``, ``config_value``, ``eco_day_record``, ``users``,
``app_settings`` and every rollout, leave-interval and worker-context table are
untouched.

NO ROWS CREATED AND NO ROWS REWRITTEN. The server default supplies ``SYSTEM``
for any row that already exists, which is the honest value for a capture that
predates attribution being recorded — it does not attribute those rows to
anybody.

NOT NULL IS SAFE HERE
---------------------
A ``server_default`` is used, not merely a Python-side default, so adding the
column NOT NULL to a table holding rows cannot fail. In practice
``config_version`` has never been created in any database — revision
``d4b7e2f9a615`` is unapplied everywhere — so there are no rows either way.

MIGRATION PARENT
----------------
``down_revision = 'c9d4a7e2f581'`` — the current single head. An ordinary
single-parent child: no new head, no merge revision, no history modified.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so no
command executing ``env.py`` was run. Verified offline against a private
in-memory SQLite database only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5f8c2a91d64'
down_revision: Union[str, Sequence[str], None] = 'c9d4a7e2f581'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Guarded the same way every revision in this programme is guarded: a database
#: built by ``create_all`` already has the column from the ORM model, and a
#: database that has never run ``d4b7e2f9a615`` has no table to alter. Both are
#: real states in this repository, so neither may raise.
TABLE = "config_version"
COLUMN = "captured_by_label"


def _existing_columns(inspector) -> set:
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        # The table is created — with this column, from the ORM model — by
        # d4b7e2f9a615 plus this revision, or by create_all. Nothing to alter.
        return
    if COLUMN in _existing_columns(inspector):
        return

    op.add_column(
        TABLE,
        sa.Column(
            COLUMN, sa.String(length=64),
            nullable=False, server_default="SYSTEM",
        ),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return
    if COLUMN not in _existing_columns(inspector):
        return
    op.drop_column(TABLE, COLUMN)
