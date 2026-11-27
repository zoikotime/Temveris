"""Class-A append-only triggers — Owner Decision 1A

Database-level append-only enforcement for the FOUR tables that survived
verification of the append-only readiness audit's **Class A**: no authorized
UPDATE, no authorized DELETE, already whole-row protected at ORM level.

    work_state_interval
    leave_input_snapshot
    config_value
    clock_session_boundary_event

``config_version`` was authorized as a fifth and is DELIBERATELY absent.
Verification found two legitimate mutations the audit had missed: ``note`` is
writable by design (administrator prose, outside the content hash), and
``captured_by_user_id`` is ``ondelete="SET NULL"``, so deleting a user issues a
database-level UPDATE against the row -- Ruling M's own mechanism for "removing
a person must never remove configuration history". An unconditional trigger
refuses it and makes such a user undeletable. Enforcement for that table needs
an owner ruling.

WHY THIS REVISION EXISTS
------------------------
Ruling M and D18-F1: ORM listeners are bypassed by SQLAlchemy Core UPDATE, bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees. The readiness audit demonstrated this
empirically — a Core UPDATE rewrote a SEALED ledger row's state and a Core
DELETE removed it — and the owner authorized closing the gap for the tables
whose lifecycle admits no mutation at all.

WHAT THIS REVISION DELIBERATELY DOES NOT TOUCH
-----------------------------------------------
``work_day_ledger``, ``eco_day_record``, ``break_classification_assertion`` and
``eco_classification_authority`` each have ONE authorized UPDATE — a NULL ->
value supersession or revocation link — so an unconditional trigger would
refuse the very mechanism §23 / INV-3 requires. The owner declined database
triggers for them (Decision 2C).

``eco_recipient_designation``, ``eco_view_grant`` and ``rollout_control`` are
intentionally mutable: DELETE is how a designation or a grant is revoked, and
UPDATE is how the durable rollout control transitions OFF -> SHADOW -> ENABLED
under its ``state_version`` optimistic-concurrency token. A trigger on
``rollout_control`` would make the rollout this programme is working toward
impossible to start.

ADDITIVE AND INERT
------------------
No table is created, altered or dropped. No column is added or removed. No row
is read, rewritten or backfilled. The revision emits DDL only, and the DDL only
REFUSES statements that no production code path issues — verified by AST scan
across the whole application before this revision was written.

Upgrading changes no behaviour for any correct caller. It changes the outcome
of exactly one class of statement: an UPDATE or DELETE that the ORM guards
already forbid and that previously slipped past them below the ORM.

DIALECTS
--------
PostgreSQL (production) and SQLite (the in-process test database) — the two
this repository supports. An unsupported dialect gets no statements at all
rather than partial protection, which the modules assert by test.

DOWNGRADE
---------
Drops the triggers it created, and nothing else. Reversible because removing an
enforcement mechanism destroys no evidence: the rows and the ORM guards are
untouched either way. This does NOT weaken production enforcement — the
upgrade path is unconditional.

Revision ID: e4b7d9c2a516
Revises: d9a4c7e35f18
Create Date: 2026-09-23
"""
from alembic import op

from app.clock_session_boundary.append_only_ddl import (
    boundary_append_only_statements,
    boundary_drop_statements,
)
from app.config_versioning.append_only_ddl import (
    value_append_only_statements,
    value_drop_statements,
)
from app.eval_day.append_only_ddl import (
    snapshot_append_only_statements,
    snapshot_drop_statements,
)
from app.work_ledger.append_only_ddl import (
    append_only_statements as interval_append_only_statements,
    drop_statements as interval_drop_statements,
)

revision = "e4b7d9c2a516"
down_revision = "d9a4c7e35f18"
branch_labels = None
depends_on = None

#: The four enforced tables, each paired with the ONE definition of its DDL.
#: Named here rather than inlined so the revision cannot drift from the modules
#: the ORM listeners use — the whole point of the shared-constant convention.
_UPGRADE = (
    interval_append_only_statements,
    snapshot_append_only_statements,
    value_append_only_statements,
    boundary_append_only_statements,
)

_DOWNGRADE = (
    boundary_drop_statements,
    value_drop_statements,
    snapshot_drop_statements,
    interval_drop_statements,
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    for statements_for in _UPGRADE:
        for statement in statements_for(dialect):
            op.execute(statement)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for statements_for in _DOWNGRADE:
        for statement in statements_for(dialect):
            op.execute(statement)
