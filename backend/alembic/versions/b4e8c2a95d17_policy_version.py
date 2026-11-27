"""The §11 Policy artifact (POLICY-VERSION).

Revision ID: b4e8c2a95d17
Revises: a9d3f6b21c58
Create Date: 2026-09-18

ADDITIVE. Creates one new table and nothing else — no existing table is
altered, no row is written, **no backfill is performed and none is required**.
Existing ConfigValue data and every stored ``content_hash`` are untouched.

APPEND-ONLY IN THE DATABASE, NOT ONLY IN THE ORM
-------------------------------------------------
The trigger SQL is imported from ``app/policy_version/append_only_ddl.py``, the
same module ``models.py`` attaches to the table's ``after_create`` event. One
definition, two emitters: ``op.create_table`` builds its own ``Table`` object so
the ORM listener does NOT fire here, and without the explicit emission a
migrated database would lack the triggers while a ``create_all`` test database
had them — every below-ORM test would then pass vacuously against a shape
production does not have.

The guard function is shared and therefore dropped SEPARATELY and LAST:
PostgreSQL refuses with ``DependentObjectsStillExist`` while a trigger still
references it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.policy_version.append_only_ddl import (
    POLICY_TABLE,
    append_only_statements,
    drop_statements,
    guard_drop_statements,
)

revision = "b4e8c2a95d17"
down_revision = "a9d3f6b21c58"
branch_labels = None
depends_on = None

#: Mirrors ``app/policy_version/vocabulary.py``. Written out rather than
#: imported as a set so the DDL text is stable and reviewable in the revision.
_NETTING_MODES = "'NONE', 'PERIOD_BANK', 'WEEKLY_BANK'"


def upgrade() -> None:
    bind = op.get_bind()
    if POLICY_TABLE in sa.inspect(bind).get_table_names():
        return

    op.create_table(
        POLICY_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        # String(64) to match EcoDayRecord.policy_version, which is where a
        # resolved policy will eventually be recorded.
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False),
        # A label, not a user FK: a foreign key would pull this table into the
        # Ruling M retention boundary, and whether capturing a policy should
        # make its author undeletable is unruled.
        sa.Column("captured_by", sa.String(length=255), nullable=False),
        # ── the five owned §11 attributes ───────────────────────────────────
        sa.Column("counts_toward_week", sa.Boolean(), nullable=False),
        # Opaque references to DEFERRED artifacts. Nullable because nothing can
        # supply a real one yet, and a NOT NULL column would force a
        # placeholder — which would be an invented profile.
        sa.Column("break_treatment_ref", sa.String(length=64), nullable=True),
        sa.Column("netting_mode", sa.String(length=32), nullable=False),
        sa.Column("rounding_profile_ref", sa.String(length=64), nullable=True),
        sa.Column("grace_minutes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        # Two versions cannot claim the same start date: "which policy governed
        # that day" must have exactly one answer.
        sa.UniqueConstraint("organization_id", "effective_from",
                            name="uq_policy_version_org_effective_from"),
        sa.UniqueConstraint("organization_id", "policy_version",
                            name="uq_policy_version_org_version_key"),
        sa.CheckConstraint("length(policy_version) > 0",
                           name="ck_policy_version_key_nonempty"),
        sa.CheckConstraint("length(content_hash) > 0",
                           name="ck_policy_version_hash_nonempty"),
        sa.CheckConstraint("length(captured_by) > 0",
                           name="ck_policy_version_captured_by_nonempty"),
        sa.CheckConstraint("grace_minutes >= 0",
                           name="ck_policy_version_grace_nonnegative"),
        sa.CheckConstraint(f"netting_mode IN ({_NETTING_MODES})",
                           name="ck_policy_version_netting_mode"),
    )
    op.create_index("ix_policy_version_organization_id", POLICY_TABLE,
                    ["organization_id"])
    # The hot read: "latest policy at or before this date, for this org".
    op.create_index("ix_policy_version_org_effective", POLICY_TABLE,
                    ["organization_id", "effective_from"])

    for statement in append_only_statements(bind.dialect.name):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    if POLICY_TABLE not in sa.inspect(bind).get_table_names():
        return

    for statement in drop_statements(bind.dialect.name):
        op.execute(sa.text(statement))
    op.drop_index("ix_policy_version_org_effective", table_name=POLICY_TABLE)
    op.drop_index("ix_policy_version_organization_id", table_name=POLICY_TABLE)
    op.drop_table(POLICY_TABLE)
    # Last, and separately: the function cannot be dropped while a trigger
    # references it.
    for statement in guard_drop_statements(bind.dialect.name):
        op.execute(sa.text(statement))
