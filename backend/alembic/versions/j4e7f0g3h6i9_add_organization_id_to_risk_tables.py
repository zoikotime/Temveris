"""add organization_id to risk_alerts, work_records, fraud_events"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'j4e7f0g3h6i9'
down_revision: Union[str, None] = 'i3d6e9f2g5h8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── risk_alerts ──────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE risk_alerts ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE risk_alerts ADD CONSTRAINT fk_risk_alerts_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE risk_alerts SET organization_id = users.organization_id
        FROM users WHERE risk_alerts.user_id = users.id AND risk_alerts.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_risk_alerts_organization_id ON risk_alerts (organization_id)"))

    # ── work_records ─────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE work_records ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE work_records ADD CONSTRAINT fk_work_records_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE work_records SET organization_id = users.organization_id
        FROM users WHERE work_records.user_id = users.id AND work_records.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_work_records_organization_id ON work_records (organization_id)"))

    # ── fraud_events ─────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE fraud_events ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE fraud_events ADD CONSTRAINT fk_fraud_events_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE fraud_events SET organization_id = work_records.organization_id
        FROM work_records
        WHERE fraud_events.wec_id = work_records.record_id AND fraud_events.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_fraud_events_organization_id ON fraud_events (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_fraud_events_organization_id', table_name='fraud_events')
    op.drop_constraint('fk_fraud_events_organization_id', 'fraud_events', type_='foreignkey')
    op.drop_column('fraud_events', 'organization_id')

    op.drop_index('ix_work_records_organization_id', table_name='work_records')
    op.drop_constraint('fk_work_records_organization_id', 'work_records', type_='foreignkey')
    op.drop_column('work_records', 'organization_id')

    op.drop_index('ix_risk_alerts_organization_id', table_name='risk_alerts')
    op.drop_constraint('fk_risk_alerts_organization_id', 'risk_alerts', type_='foreignkey')
    op.drop_column('risk_alerts', 'organization_id')
