"""Break-classification foundation (P-1, P-2, P-3, C-1, D-1).

Revision ID: d7f3b8c41a92
Revises: c5a9f2d38e61
Create Date: 2026-09-21

ADDITIVE. Creates four new tables and adds two nullable columns to
``eco_day_record``. No existing column is altered, no index is dropped, no row
is rewritten, and **no backfill is performed**.

**NO BACKFILL, DELIBERATELY.**
------------------------------
``evaluation_deferral_reason`` stays NULL on every existing row, and NULL is a
real answer rather than a missing one: it means the ledger/departure deferral
this state has always carried. Writing ``CLASSIFICATION_UNRESOLVED`` onto a
historical row would assert a determination nobody made, and writing any other
value would invent a vocabulary member. ``classification_snapshot_key`` stays
NULL for the same reason — those evaluations used no classification input,
because none existed.

Both columns are NULLABLE rather than ``NOT NULL DEFAULT ...`` for that reason:
a NOT NULL column would force exactly the fabricated value this revision
refuses to write, and would keep forcing it for every future evaluation that
legitimately has neither.

THE UNIQUE SNAPSHOT INDEX IS REBUILT, NOT WIDENED IN PLACE
-----------------------------------------------------------
``uq_eco_day_record_snapshot`` gains the two new components under COALESCE,
exactly as ``policy_version`` and ``config_version`` are already carried. An
index cannot be redefined in place, so it is dropped and recreated. That is not
destructive: the index holds no data, and the rebuilt form is strictly wider —
every row admissible under the old rule remains admissible under the new one,
because the added components are empty strings for every existing row.

CHECK CONSTRAINTS ARE POSTGRESQL-ONLY, AS IN a9d3f6b21c58 AND c5a9f2d38e61
---------------------------------------------------------------------------
SQLite cannot ADD a CHECK to an existing table, and rebuilding an evidentiary
table to add one would be destructive. The models declare them, so a
``create_all`` database and a migrated PostgreSQL database both have them.
Tables created fresh here carry their CHECKs on every dialect.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7f3b8c41a92"
down_revision = "c5a9f2d38e61"
branch_labels = None
depends_on = None

DAY_RECORD = "eco_day_record"
REASON_COLUMN = "evaluation_deferral_reason"
SNAPSHOT_COLUMN = "classification_snapshot_key"

AUTHORITY_TABLE = "eco_classification_authority"
ASSERTION_TABLE = "break_classification_assertion"
SNAPSHOT_TABLE = "classification_snapshot"
PROFILE_TABLE = "break_treatment_profile"

#: Mirrors ``app/break_classification/vocabulary.py``. Written out rather than
#: imported so the DDL text is stable and reviewable in the revision.
_RESULTS_SQL = "'BRK_M', 'BRK_R', 'UNCLASSIFIABLE'"

_OLD_SNAPSHOT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
    "ON eco_day_record "
    "(organization_id, user_id, work_date, ledger_version, "
    " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
    " leave_input_snapshot_key)"
)

_NEW_SNAPSHOT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
    "ON eco_day_record "
    "(organization_id, user_id, work_date, ledger_version, "
    " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
    " leave_input_snapshot_key, "
    " COALESCE(classification_snapshot_key, ''), "
    " COALESCE(evaluation_deferral_reason, ''))"
)


def _columns(inspector, table) -> set:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"

    # ── 1. The break-treatment profile (P-3) ────────────────────────────────
    if PROFILE_TABLE not in tables:
        op.create_table(
            PROFILE_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("profile_ref", sa.String(length=64), nullable=False),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column("captured_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("captured_by", sa.String(length=255), nullable=False),
            sa.Column("meal_qualifies", sa.Boolean(), nullable=False),
            sa.Column("rest_qualifies", sa.Boolean(), nullable=False),
            sa.Column("rest_qualifying_cap_minutes", sa.Integer(),
                      nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.UniqueConstraint("organization_id", "profile_ref",
                                name="uq_break_treatment_profile_ref"),
            sa.UniqueConstraint("organization_id", "effective_from",
                                name="uq_break_treatment_profile_effective"),
            sa.CheckConstraint("length(trim(profile_ref)) > 0",
                               name="ck_break_treatment_profile_ref_nonempty"),
            sa.CheckConstraint("length(trim(captured_by)) > 0",
                               name="ck_break_treatment_profile_actor_nonempty"),
            sa.CheckConstraint("length(content_hash) > 0",
                               name="ck_break_treatment_profile_hash_nonempty"),
            sa.CheckConstraint(
                "rest_qualifying_cap_minutes IS NULL OR "
                "rest_qualifying_cap_minutes >= 0",
                name="ck_break_treatment_profile_cap_nonnegative"),
        )
        op.create_index("ix_break_treatment_profile_org", PROFILE_TABLE,
                        ["organization_id"])

    # ── 2. The classification authority (P-1) ───────────────────────────────
    if AUTHORITY_TABLE not in tables:
        op.create_table(
            AUTHORITY_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("authority_label", sa.String(length=128), nullable=False),
            sa.Column("granted_by", sa.String(length=128), nullable=False),
            sa.Column("granted_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("revoked_at_utc", sa.DateTime(timezone=True),
                      nullable=True),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="RESTRICT"),
            sa.UniqueConstraint("organization_id", "user_id",
                                name="uq_eco_classification_authority_identity"),
            sa.CheckConstraint(
                "length(trim(authority_label)) > 0",
                name="ck_eco_classification_authority_label_nonempty"),
            sa.CheckConstraint(
                "length(trim(granted_by)) > 0",
                name="ck_eco_classification_authority_actor_nonempty"),
        )
        op.create_index("ix_eco_classification_authority_org", AUTHORITY_TABLE,
                        ["organization_id"])

    # ── 3. The assertion (P-1) ──────────────────────────────────────────────
    if ASSERTION_TABLE not in tables:
        op.create_table(
            ASSERTION_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("work_date", sa.Date(), nullable=False),
            sa.Column("source_ref", sa.String(length=128), nullable=False),
            sa.Column("classification", sa.String(length=32), nullable=False),
            sa.Column("evidence_ref", sa.String(length=255), nullable=False),
            sa.Column("evidence_detail", sa.Text(), nullable=True),
            sa.Column("authority_id", sa.Integer(), nullable=False),
            sa.Column("authority_label", sa.String(length=128), nullable=False),
            sa.Column("asserted_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("assertion_version", sa.Integer(), nullable=False),
            sa.Column("supersedes", sa.Integer(), nullable=True),
            sa.Column("superseded_by", sa.Integer(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["authority_id"],
                                    [f"{AUTHORITY_TABLE}.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["supersedes"], [f"{ASSERTION_TABLE}.id"],
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["superseded_by"],
                                    [f"{ASSERTION_TABLE}.id"],
                                    ondelete="SET NULL"),
            sa.UniqueConstraint(
                "organization_id", "user_id", "work_date", "source_ref",
                "assertion_version",
                name="uq_break_classification_assertion_version"),
            sa.CheckConstraint(
                f"classification IN ({_RESULTS_SQL})",
                name="ck_break_classification_assertion_vocabulary"),
            sa.CheckConstraint(
                "length(trim(source_ref)) > 0",
                name="ck_break_classification_assertion_source_nonempty"),
            sa.CheckConstraint(
                "length(trim(evidence_ref)) > 0",
                name="ck_break_classification_assertion_evidence_nonempty"),
            sa.CheckConstraint(
                "assertion_version >= 1",
                name="ck_break_classification_assertion_version_positive"),
            sa.CheckConstraint(
                "length(content_hash) > 0",
                name="ck_break_classification_assertion_hash_nonempty"),
        )
        op.create_index("ix_break_classification_assertion_day",
                        ASSERTION_TABLE,
                        ["organization_id", "user_id", "work_date"])

    # ── 4. The classification snapshot (C-1) ────────────────────────────────
    if SNAPSHOT_TABLE not in tables:
        op.create_table(
            SNAPSHOT_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_key", sa.String(length=64), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("work_date", sa.Date(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("assertion_count", sa.Integer(), nullable=False),
            sa.Column("has_unclassifiable", sa.Integer(), nullable=False),
            sa.Column("captured_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("members_json", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="RESTRICT"),
            sa.UniqueConstraint("organization_id", "snapshot_key",
                                name="uq_classification_snapshot_key"),
            sa.CheckConstraint("length(snapshot_key) > 0",
                               name="ck_classification_snapshot_key_nonempty"),
            sa.CheckConstraint("length(content_hash) > 0",
                               name="ck_classification_snapshot_hash_nonempty"),
            sa.CheckConstraint("snapshot_key = content_hash",
                               name="ck_classification_snapshot_key_is_hash"),
            sa.CheckConstraint(
                "assertion_count >= 0",
                name="ck_classification_snapshot_count_nonnegative"),
        )
        op.create_index("ix_classification_snapshot_day", SNAPSHOT_TABLE,
                        ["organization_id", "user_id", "work_date"])
        op.create_index("ix_classification_snapshot_key", SNAPSHOT_TABLE,
                        ["snapshot_key"])

    # ── 5. The two EcoDayRecord columns (D-1, C-1) ──────────────────────────
    if DAY_RECORD not in tables:
        # EVAL-DAY has not run in this database. create_all builds the table
        # with both columns already present.
        return

    existing = _columns(inspector, DAY_RECORD)
    if REASON_COLUMN not in existing:
        op.add_column(DAY_RECORD,
                      sa.Column(REASON_COLUMN, sa.String(length=32),
                                nullable=True))
    if SNAPSHOT_COLUMN not in existing:
        op.add_column(DAY_RECORD,
                      sa.Column(SNAPSHOT_COLUMN, sa.String(length=64),
                                nullable=True))

    # The snapshot identity gains its eighth and ninth components. Dropped
    # first: an index cannot be redefined in place.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.execute(sa.text(_NEW_SNAPSHOT_INDEX))

    if is_pg:
        # D-1's vocabulary, at the schema. NULL stays legitimate — it is the
        # departure/ledger deferral.
        op.create_check_constraint(
            "ck_eco_day_record_deferral_reason_vocabulary", DAY_RECORD,
            f"{REASON_COLUMN} IS NULL OR "
            f"{REASON_COLUMN} IN ('CLASSIFICATION_UNRESOLVED')")
        # A reason may accompany ONLY the deferred carrier. The structural
        # refusal lives in ``DayEvaluation.__post_init__``; this is the same
        # rule where the database can also see it.
        op.create_check_constraint(
            "ck_eco_day_record_deferral_reason_state", DAY_RECORD,
            f"{REASON_COLUMN} IS NULL OR state = 'DEFERRED_PENDING_LEDGER'")


def downgrade() -> None:
    """Drop the columns and the four tables; restore the seven-part index.

    Safe: nothing outside this increment reads any of it. What is lost is the
    ability to distinguish a classification deferral from a departure one, and
    the chain of custody behind any classification — the honest consequence of
    removing the artifacts, not a reason to keep a copy elsewhere.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"

    if DAY_RECORD in tables:
        if is_pg:
            for name in ("ck_eco_day_record_deferral_reason_state",
                         "ck_eco_day_record_deferral_reason_vocabulary"):
                op.drop_constraint(name, DAY_RECORD, type_="check")

        op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
        op.execute(sa.text(_OLD_SNAPSHOT_INDEX))

        existing = _columns(inspector, DAY_RECORD)
        if SNAPSHOT_COLUMN in existing:
            op.drop_column(DAY_RECORD, SNAPSHOT_COLUMN)
        if REASON_COLUMN in existing:
            op.drop_column(DAY_RECORD, REASON_COLUMN)

    # Dropped in dependency order: assertions reference the authority, and the
    # snapshot references neither.
    for table in (SNAPSHOT_TABLE, ASSERTION_TABLE, AUTHORITY_TABLE,
                  PROFILE_TABLE):
        if table in tables:
            op.drop_table(table)
