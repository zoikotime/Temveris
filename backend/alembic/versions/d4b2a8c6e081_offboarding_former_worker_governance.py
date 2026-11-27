"""offboarding former-worker governance: portal + retention + deletion review

Adds the seven tables that back the former-worker lifecycle governance layer
(§1-§9 of the governance spec) on top of the security migration
(c3a1f7e92b15):

  offboarding_portal_sessions          — isolated former-worker portal sessions
  offboarding_portal_mfa               — alternate verified email + enrollment
  offboarding_portal_access_log        — append-only portal action audit
  offboarding_retention_policies       — versioned, jurisdiction-aware policies
  offboarding_deletion_reviews         — dual-approval deletion eligibility
  offboarding_support_tickets          — former-worker support requests
  offboarding_data_rights_requests     — DSAR / erasure / portability filings

Tables are created from SQLAlchemy metadata (GOVERNANCE_MODELS) so this
migration stays authoritative against models.py.

Revision ID: d4b2a8c6e081
Revises: c3a1f7e92b15
Create Date: 2026-05-27
"""
from typing import Sequence, Union

from alembic import op

from app.offboarding import models as offb_models


revision: str = "d4b2a8c6e081"
down_revision: Union[str, Sequence[str], None] = "c3a1f7e92b15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for model in offb_models.GOVERNANCE_MODELS:
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(offb_models.GOVERNANCE_MODELS):
        model.__table__.drop(bind=bind, checkfirst=True)
