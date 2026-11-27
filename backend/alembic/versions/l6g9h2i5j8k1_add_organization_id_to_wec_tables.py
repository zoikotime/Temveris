"""add organization_id to work_evidence_chain, work_events, integrity_scores"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'l6g9h2i5j8k1'
down_revision: Union[str, None] = 'k5f8g1h4i7j0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── work_evidence_chain ───────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE work_evidence_chain ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE work_evidence_chain ADD CONSTRAINT fk_wec_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE work_evidence_chain SET organization_id = users.organization_id
        FROM users WHERE work_evidence_chain.user_id = users.id AND work_evidence_chain.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_wec_organization_id ON work_evidence_chain (organization_id)"))

    # ── work_events ───────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE work_events ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE work_events ADD CONSTRAINT fk_work_events_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE work_events SET organization_id = users.organization_id
        FROM users WHERE work_events.user_id = users.id AND work_events.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_work_events_organization_id ON work_events (organization_id)"))

    # ── integrity_scores ──────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE integrity_scores ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE integrity_scores ADD CONSTRAINT fk_integrity_scores_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE integrity_scores SET organization_id = users.organization_id
        FROM users WHERE integrity_scores.user_id = users.id AND integrity_scores.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_integrity_scores_organization_id ON integrity_scores (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_integrity_scores_organization_id', table_name='integrity_scores')
    op.drop_constraint('fk_integrity_scores_organization_id', 'integrity_scores', type_='foreignkey')
    op.drop_column('integrity_scores', 'organization_id')

    op.drop_index('ix_work_events_organization_id', table_name='work_events')
    op.drop_constraint('fk_work_events_organization_id', 'work_events', type_='foreignkey')
    op.drop_column('work_events', 'organization_id')

    op.drop_index('ix_wec_organization_id', table_name='work_evidence_chain')
    op.drop_constraint('fk_wec_organization_id', 'work_evidence_chain', type_='foreignkey')
    op.drop_column('work_evidence_chain', 'organization_id')
