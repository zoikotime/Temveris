"""ECO compliance evidence, governed jurisdiction mapping, netting provenance

The accepted owner decision record: D1-D7, A-I, B8, B9.

WHAT THIS REVISION ADDS
-----------------------
Six new tables and one new column. Nothing existing is dropped, rewritten or
back-filled.

    jurisdiction_classification                 B: platform-governed, effective-dated
    tenant_jurisdiction_assignment              B8: platform assigns, {admin, hr} correct
    tenant_jurisdiction_assignment_country      B9: zero rows IS the declared empty set
    eco_dpia_evidence                           D2 (+ C: two applicability flags)
    eco_consultation_attestation                D3
    eco_worker_notice_confirmation              D4 (Tenant-level only)
    policy_version.netting_decision_provenance  D5 / OD-02: A

WHY THE NEW COLUMN IS NULLABLE
------------------------------
Owner ruling F: D. ``policy_version`` is append-only with database triggers, so
rows captured before this column existed can never be back-filled -- and this
revision does not try. NULL is the grandfathered historical state; the
requirement binds on the NEXT capture, which ``capture_policy_version`` now
refuses without a value.

WHY IT IS NOT IN THE CONTENT HASH
---------------------------------
``compute_policy_content_hash`` excludes ``captured_at_utc`` and
``captured_by`` because, in its own words, "they are provenance about the
capture, not the policy... two Tenants with the same rules would be unable to
recognise that they match". How a netting mode was SELECTED is the same class
of fact. Hashing it would make two identical policies hash differently, so the
digest is untouched and every existing hash stays valid.

A DIALECT LIMITATION, STATED RATHER THAN HIDDEN
-----------------------------------------------
The provenance CHECK constraint is added on PostgreSQL via ALTER TABLE ... ADD
CONSTRAINT. SQLite cannot add a CHECK to an existing table without rebuilding
it, and rebuilding ``policy_version`` would drop its append-only triggers and
rewrite immutable rows -- exactly what this increment is forbidden to do. So on
SQLite the column is added WITHOUT the CHECK, and the constraint is enforced
there by the model (which every ``create_all`` schema carries) and by
``capture_policy_version``, which refuses an ungoverned value before the
session is touched. PostgreSQL is production; the gap is SQLite-only and is
asserted by test rather than left to be discovered.

APPEND-ONLY (rulings B, B8, D2, D3, D4)
---------------------------------------
All six new tables carry dialect triggers, emitted here explicitly because
``op.create_table`` builds its own ``Table`` object and the ORM-table listeners
do not fire for it.

SEED DATA: EXACTLY FOUR GOVERNED CLASSIFICATION ROWS, AND NOTHING ELSE
----------------------------------------------------------------------
Ruling B requires DE, NL, AT and FR to be marked as requiring consultation
from the start. That is platform-governed reference data, and a migration is
the only mechanism here that is platform-controlled by construction: no Tenant
can run it, and no application path writes it. The rows come from
``SPEC_CLASSIFICATIONS`` in the jurisdiction package, so the schema and the
resolver's vocabulary depend on ONE definition rather than two that drift.

Idempotent: each row is inserted only where no row for that country and
effective date already exists, so re-running the seed adds nothing and the
append-only triggers are never asked to accept a duplicate.

NOT seeded: any other country — §40.4's "and elsewhere" is an instruction that
the mapping be EXTENSIBLE, never a licence to infer a list, and GB is absent
on purpose. Not any Tenant assignment, which is a decision about a real
Tenant. Not any compliance evidence, which would fabricate the very thing
§44.3 exists to require.

Revision ID: f1c6b28d5a73
Revises: e4b7d9c2a516
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

from app.compliance_evidence.append_only_ddl import (
    CONSULTATION_TABLE, DPIA_TABLE, WORKER_NOTICE_TABLE,
    all_append_only_statements as compliance_append_only,
    all_drop_statements as compliance_drop,
)
from app.jurisdiction.append_only_ddl import (
    ASSIGNMENT_COUNTRY_TABLE, ASSIGNMENT_TABLE, CLASSIFICATION_TABLE,
    all_append_only_statements as jurisdiction_append_only,
    all_drop_statements as jurisdiction_drop,
)
from app.jurisdiction.outcomes import (
    SPEC_CLASSIFICATION_EFFECTIVE_FROM, SPEC_CLASSIFICATIONS,
)

revision = "f1c6b28d5a73"
down_revision = "e4b7d9c2a516"
branch_labels = None
depends_on = None

_PROVENANCE_CHECK = "ck_policy_version_netting_provenance"
_PROVENANCE_SQL = ("netting_decision_provenance IS NULL OR "
                   "netting_decision_provenance = 'EXPLICIT_TENANT_CHOICE'")

_ACTOR_ROLE_SQL = "actor_role IN ('admin', 'hr')"
_STATUS_SQL = "status IN ('COMPLETED', 'NOT_REQUIRED')"


def _evidence_columns():
    """The shape D2, D3 and D4 share. Spelled once, per table."""
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("actor_label", sa.String(length=64), nullable=False,
                  server_default="SYSTEM"),
        sa.Column("external_reference", sa.Text(), nullable=True),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
    ]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    # ── B: the governed, effective-dated country classification ────────────
    op.create_table(
        CLASSIFICATION_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("is_eu", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_uk", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("requires_consultation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("recorded_by", sa.String(length=64), nullable=False,
                  server_default="PLATFORM"),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["supersedes_id"],
                                [f"{CLASSIFICATION_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("country_code", "effective_from",
                            name="uq_jurisdiction_classification_effective"),
        sa.CheckConstraint("length(country_code) = 2",
                           name="ck_jurisdiction_classification_alpha2_length"),
        sa.CheckConstraint("country_code = upper(country_code)",
                           name="ck_jurisdiction_classification_alpha2_upper"),
    )
    op.create_index(f"ix_{CLASSIFICATION_TABLE}_country_code",
                    CLASSIFICATION_TABLE, ["country_code"])
    op.create_index(f"ix_{CLASSIFICATION_TABLE}_effective_from",
                    CLASSIFICATION_TABLE, ["effective_from"])
    op.create_index("ix_jurisdiction_classification_lookup",
                    CLASSIFICATION_TABLE, ["country_code", "effective_from"])

    # ── B8: the Tenant assignment, with its provenance ─────────────────────
    op.create_table(
        ASSIGNMENT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("actor_label", sa.String(length=64), nullable=False,
                  server_default="PLATFORM"),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"], [f"{ASSIGNMENT_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "effective_from",
                            name="uq_tenant_jurisdiction_assignment_effective"),
        sa.CheckConstraint("source IN ('PLATFORM', 'TENANT')",
                           name="ck_tenant_jurisdiction_assignment_source"),
        sa.CheckConstraint(
            "(source = 'TENANT' AND actor_user_id IS NOT NULL "
            " AND actor_role IS NOT NULL)"
            " OR (source = 'PLATFORM' AND actor_user_id IS NULL)",
            name="ck_tenant_jurisdiction_assignment_actor_pair"),
    )
    op.create_index(f"ix_{ASSIGNMENT_TABLE}_organization_id",
                    ASSIGNMENT_TABLE, ["organization_id"])
    op.create_index(f"ix_{ASSIGNMENT_TABLE}_effective_from",
                    ASSIGNMENT_TABLE, ["effective_from"])
    op.create_index("ix_tenant_jurisdiction_assignment_lookup",
                    ASSIGNMENT_TABLE, ["organization_id", "effective_from"])

    # ── B9: the country rows. ZERO of them is the declared empty set ───────
    op.create_table(
        ASSIGNMENT_COUNTRY_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], [f"{ASSIGNMENT_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("assignment_id", "country_code",
                            name="uq_tenant_jurisdiction_assignment_country"),
        sa.CheckConstraint("length(country_code) = 2",
                           name="ck_tenant_jurisdiction_country_alpha2_length"),
        sa.CheckConstraint("country_code = upper(country_code)",
                           name="ck_tenant_jurisdiction_country_alpha2_upper"),
    )
    op.create_index(f"ix_{ASSIGNMENT_COUNTRY_TABLE}_assignment_id",
                    ASSIGNMENT_COUNTRY_TABLE, ["assignment_id"])
    op.create_index(f"ix_{ASSIGNMENT_COUNTRY_TABLE}_country_code",
                    ASSIGNMENT_COUNTRY_TABLE, ["country_code"])

    # ── D2: DPIA evidence, with C's two applicability flags ────────────────
    op.create_table(
        DPIA_TABLE,
        *_evidence_columns(),
        sa.Column("jurisdiction_scope", sa.String(length=255), nullable=False,
                  server_default=""),
        sa.Column("eu_substantive_applies", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("eu_uk_gate_applies", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"], [f"{DPIA_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.CheckConstraint(_STATUS_SQL, name="ck_eco_dpia_evidence_status"),
        sa.CheckConstraint(_ACTOR_ROLE_SQL,
                           name="ck_eco_dpia_evidence_actor_role"),
        sa.CheckConstraint("valid_until IS NULL OR valid_until >= effective_from",
                           name="ck_eco_dpia_evidence_validity_window"),
    )
    op.create_index(f"ix_{DPIA_TABLE}_organization_id", DPIA_TABLE,
                    ["organization_id"])
    op.create_index(f"ix_{DPIA_TABLE}_effective_from", DPIA_TABLE,
                    ["effective_from"])
    op.create_index("ix_eco_dpia_evidence_lookup", DPIA_TABLE,
                    ["organization_id", "effective_from"])

    # ── D3: the consultation attestation ───────────────────────────────────
    op.create_table(
        CONSULTATION_TABLE,
        *_evidence_columns(),
        sa.Column("jurisdictions", sa.String(length=255), nullable=False,
                  server_default=""),
        sa.Column("applicability_scope", sa.String(length=32), nullable=False,
                  server_default="ORGANIZATION"),
        sa.Column("attested_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"],
                                [f"{CONSULTATION_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.CheckConstraint(_STATUS_SQL,
                           name="ck_eco_consultation_attestation_status"),
        sa.CheckConstraint(_ACTOR_ROLE_SQL,
                           name="ck_eco_consultation_attestation_actor_role"),
        sa.CheckConstraint("expires_on IS NULL OR expires_on >= effective_from",
                           name="ck_eco_consultation_attestation_window"),
    )
    op.create_index(f"ix_{CONSULTATION_TABLE}_organization_id",
                    CONSULTATION_TABLE, ["organization_id"])
    op.create_index(f"ix_{CONSULTATION_TABLE}_effective_from",
                    CONSULTATION_TABLE, ["effective_from"])
    op.create_index("ix_eco_consultation_attestation_lookup",
                    CONSULTATION_TABLE, ["organization_id", "effective_from"])

    # ── D4: the Tenant-level worker-notice confirmation ────────────────────
    op.create_table(
        WORKER_NOTICE_TABLE,
        *_evidence_columns(),
        sa.Column("confirmed_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("population_scope", sa.String(length=64), nullable=False,
                  server_default="ALL_WORKERS"),
        sa.Column("new_joiners_covered", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("repeat_on_policy_change", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"],
                                [f"{WORKER_NOTICE_TABLE}.id"],
                                ondelete="RESTRICT"),
        sa.CheckConstraint(_STATUS_SQL, name="ck_eco_worker_notice_status"),
        sa.CheckConstraint(_ACTOR_ROLE_SQL,
                           name="ck_eco_worker_notice_actor_role"),
    )
    op.create_index(f"ix_{WORKER_NOTICE_TABLE}_organization_id",
                    WORKER_NOTICE_TABLE, ["organization_id"])
    op.create_index(f"ix_{WORKER_NOTICE_TABLE}_effective_from",
                    WORKER_NOTICE_TABLE, ["effective_from"])
    op.create_index("ix_eco_worker_notice_lookup", WORKER_NOTICE_TABLE,
                    ["organization_id", "effective_from"])

    # ── D5 / OD-02: A. Additive, nullable, no back-fill ────────────────────
    op.add_column("policy_version",
                  sa.Column("netting_decision_provenance",
                            sa.String(length=32), nullable=True))
    if dialect == "postgresql":
        # SQLite cannot ADD a CHECK without rebuilding the table, and
        # rebuilding `policy_version` would drop its append-only triggers and
        # rewrite immutable rows. See the module docstring.
        op.create_check_constraint(_PROVENANCE_CHECK, "policy_version",
                                   _PROVENANCE_SQL)

    # ── Append-only, emitted explicitly (rulings B, B8, D2, D3, D4) ────────
    for statement in jurisdiction_append_only(dialect):
        op.execute(statement)
    for statement in compliance_append_only(dialect):
        op.execute(statement)

    # ── Ruling B: the four named jurisdictions ─────────────────────────────
    seed_spec_classifications(op.get_bind())


def seed_spec_classifications(bind) -> int:
    """Insert the governed rows that are absent. Adds nothing on a re-run.

    Written as INSERT ... SELECT WHERE NOT EXISTS rather than a read followed
    by a write, so two operators running this at once cannot both decide the
    row is missing. UNIQUE(country_code, effective_from) is the backstop if
    they somehow do.

    Runs AFTER the append-only triggers are installed, so these rows are
    subject to the same immutability as every row written later — the seed
    gets no privilege the operational path lacks.
    """
    inserted = 0
    for code, is_eu, is_uk, requires_consultation in SPEC_CLASSIFICATIONS:
        result = bind.execute(
            sa.text(
                f"INSERT INTO {CLASSIFICATION_TABLE} "  # noqa: S608
                f"(country_code, is_eu, is_uk, requires_consultation,"
                f" effective_from, recorded_by)"
                f" SELECT :code, :is_eu, :is_uk, :consult, :eff, 'PLATFORM'"
                f" WHERE NOT EXISTS (SELECT 1 FROM {CLASSIFICATION_TABLE}"
                f"  WHERE country_code = :code AND effective_from = :eff)"),
            {"code": code, "is_eu": is_eu, "is_uk": is_uk,
             "consult": requires_consultation,
             "eff": SPEC_CLASSIFICATION_EFFECTIVE_FROM})
        inserted += result.rowcount or 0
    return inserted


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for table in (DPIA_TABLE, CONSULTATION_TABLE, WORKER_NOTICE_TABLE,
                  ASSIGNMENT_COUNTRY_TABLE, ASSIGNMENT_TABLE,
                  CLASSIFICATION_TABLE):
        held = bind.execute(
            sa.text(f"SELECT count(*) FROM {table}")).scalar()  # noqa: S608
        if held:
            raise RuntimeError(
                f"cannot downgrade: {held} row(s) in {table}. Dropping it "
                f"would discard governed jurisdiction or compliance evidence, "
                f"which is a decision rather than a schema step. Note that "
                f"upgrade() seeds four classification rows, so this revision "
                f"is not reversible once applied without an explicit decision "
                f"to discard the governed mapping.")

    for statement in compliance_drop(dialect):
        op.execute(statement)
    for statement in jurisdiction_drop(dialect):
        op.execute(statement)

    if dialect == "postgresql":
        op.drop_constraint(_PROVENANCE_CHECK, "policy_version",
                           type_="check")
    op.drop_column("policy_version", "netting_decision_provenance")

    for table in (WORKER_NOTICE_TABLE, CONSULTATION_TABLE, DPIA_TABLE,
                  ASSIGNMENT_COUNTRY_TABLE, ASSIGNMENT_TABLE,
                  CLASSIFICATION_TABLE):
        op.drop_table(table)
