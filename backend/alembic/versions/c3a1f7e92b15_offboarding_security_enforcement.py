"""offboarding security enforcement: incidents + telemetry quarantine

Adds the two tables that back the runtime-enforcement layer (§1-§8) on top of
the original 13-table lifecycle migration (b7d2e5f10a44):

  offboarding_security_incidents     — graded, escalatable failure records (§6)
  offboarding_telemetry_quarantine   — rejected post-exit telemetry metadata (§2)

Both are created from the SQLAlchemy model metadata (SECURITY_MODELS) so this
migration cannot drift from models.py — the same definitions used by
bootstrap.ensure_tables(). The reconciliation queue, audit and domain-event
streams that the enforcement layer also relies on already exist from the
lifecycle migration; nothing here alters them.

Revision ID: c3a1f7e92b15
Revises: c8e3f1a92b50
Create Date: 2026-05-27

NB: chained AFTER c8e3f1a92b50 (perf indexes) rather than directly off
b7d2e5f10a44. Both this migration and the perf-indexes migration were authored
against the lifecycle head; pointing this one at the perf migration keeps the
revision graph LINEAR (single head) so `alembic upgrade head` never hits a
"multiple heads" error in CI/CD. Order is irrelevant for correctness — perf
indexes touch legacy tables, this creates new offboarding tables.
"""
from typing import Sequence, Union

from alembic import op

from app.offboarding import models as offb_models


revision: str = "c3a1f7e92b15"
down_revision: Union[str, Sequence[str], None] = "c8e3f1a92b50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for model in offb_models.SECURITY_MODELS:
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(offb_models.SECURITY_MODELS):
        model.__table__.drop(bind=bind, checkfirst=True)
