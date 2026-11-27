"""offboarding lifecycle: governed exit closure system

Creates the 13 tables that back the Offboarding & Former-Worker Lifecycle
module (§1):

  offboarding_cases, offboarding_tasks, access_revocation_records,
  former_worker_access, retention_assignments, legal_holds,
  completion_certificates, evidence_packages, offboarding_audit_events,
  offboarding_domain_events, offboarding_workflow_blockers,
  offboarding_workflow_waivers, integration_reconciliation_queue.

Every table carries tenant_id (indexed), UUID/opaque string PKs, created_at /
updated_at (audit + event tables are append-only and omit updated_at), and the
indexed fields required by the spec. The audit table is hash-chained at the
application layer (events.append_audit); the domain-event table is idempotent
via a UNIQUE idempotency_key.

The tables are created from the SQLAlchemy model metadata so this migration
stays authoritative and cannot drift from models.py — the same definitions used
by bootstrap.ensure_tables().

Revision ID: b7d2e5f10a44
Revises: a3f4c8d91e02
Create Date: 2026-05-27
"""
from typing import Sequence, Union

from alembic import op

from app.offboarding import models as offb_models


revision: str = "b7d2e5f10a44"
down_revision: Union[str, Sequence[str], None] = "a3f4c8d91e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Create in dependency order (cases first — children FK to it).
    for model in offb_models.ALL_MODELS:
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    # Drop in reverse so FK children go before the parent case table.
    for model in reversed(offb_models.ALL_MODELS):
        model.__table__.drop(bind=bind, checkfirst=True)
