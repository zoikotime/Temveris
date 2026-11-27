"""add organization_id to shifts, shift_assignments, employee_availability"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'k5f8g1h4i7j0'
down_revision: Union[str, None] = 'j4e7f0g3h6i9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── shifts ────────────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE shifts ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE shifts ADD CONSTRAINT fk_shifts_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE shifts SET organization_id = users.organization_id
        FROM users WHERE shifts.created_by = users.id AND shifts.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_shifts_organization_id ON shifts (organization_id)"))

    # ── shift_assignments ─────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE shift_assignments ADD CONSTRAINT fk_shift_assignments_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE shift_assignments SET organization_id = users.organization_id
        FROM users WHERE shift_assignments.user_id = users.id AND shift_assignments.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_shift_assignments_organization_id ON shift_assignments (organization_id)"))

    # ── employee_availability ─────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE employee_availability ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE employee_availability ADD CONSTRAINT fk_employee_availability_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE employee_availability SET organization_id = users.organization_id
        FROM users WHERE employee_availability.user_id = users.id AND employee_availability.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_employee_availability_organization_id ON employee_availability (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_employee_availability_organization_id', table_name='employee_availability')
    op.drop_constraint('fk_employee_availability_organization_id', 'employee_availability', type_='foreignkey')
    op.drop_column('employee_availability', 'organization_id')

    op.drop_index('ix_shift_assignments_organization_id', table_name='shift_assignments')
    op.drop_constraint('fk_shift_assignments_organization_id', 'shift_assignments', type_='foreignkey')
    op.drop_column('shift_assignments', 'organization_id')

    op.drop_index('ix_shifts_organization_id', table_name='shifts')
    op.drop_constraint('fk_shifts_organization_id', 'shifts', type_='foreignkey')
    op.drop_column('shifts', 'organization_id')
