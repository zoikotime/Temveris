"""create_commercial_tables

Revision ID: q1r4s7t0u3v6
Revises: p0r3s6t9u2v5
Create Date: 2026-05-20 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision: str = 'q1r4s7t0u3v6'
down_revision: Union[str, Sequence[str], None] = 'p0r3s6t9u2v5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # commercial_organizations
    op.create_table(
        'commercial_organizations',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('legal_name', sa.String(255), nullable=True),
        sa.Column('country', sa.String(2), nullable=True),
        sa.Column('jurisdiction', sa.String(40), nullable=True),
        sa.Column('industry', sa.String(80), nullable=True),
        sa.Column('expected_users', sa.Integer(), nullable=True),
        sa.Column('domain', sa.String(255), nullable=True),
        sa.Column('owner_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('commercial_state', sa.String(40), nullable=False, server_default='trial_active'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_organizations_domain', 'commercial_organizations', ['domain'])
    op.create_index('ix_commercial_organizations_commercial_state', 'commercial_organizations', ['commercial_state'])
    op.create_index('ix_commercial_organizations_created_at', 'commercial_organizations', ['created_at'])
    op.create_index('ix_commercial_org_state_created', 'commercial_organizations', ['commercial_state', 'created_at'])

    # commercial_workspaces
    op.create_table(
        'commercial_workspaces',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('organization_id', sa.String(40), sa.ForeignKey('commercial_organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('slug', sa.String(120), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('region', sa.String(40), nullable=True),
        sa.Column('timezone', sa.String(64), nullable=True),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('locale', sa.String(10), nullable=True, server_default='en-US'),
        sa.Column('provisioning_state', sa.String(40), nullable=False, server_default='not_started'),
        sa.Column('first_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_email_verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_policy_configured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_invite_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_teammate_accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_work_session_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_report_viewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_workspaces_organization_id', 'commercial_workspaces', ['organization_id'])
    op.create_index('ix_commercial_workspaces_slug', 'commercial_workspaces', ['slug'])
    op.create_index('ix_commercial_workspaces_provisioning_state', 'commercial_workspaces', ['provisioning_state'])

    # commercial_signups
    op.create_table(
        'commercial_signups',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('state', sa.String(40), nullable=False, server_default='signup_started'),
        sa.Column('route', sa.String(40), nullable=True),
        sa.Column('intent', sa.String(20), nullable=False, server_default='trial'),
        sa.Column('plan', sa.String(40), nullable=True),
        sa.Column('expected_users', sa.Integer(), nullable=True),
        sa.Column('country', sa.String(2), nullable=True),
        sa.Column('locale', sa.String(10), nullable=True),
        sa.Column('timezone', sa.String(64), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('organization_id', sa.String(40), nullable=True),
        sa.Column('workspace_id', sa.String(40), nullable=True),
        sa.Column('utm_source', sa.String(120), nullable=True),
        sa.Column('utm_medium', sa.String(120), nullable=True),
        sa.Column('utm_campaign', sa.String(120), nullable=True),
        sa.Column('referrer', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent_hash', sa.String(64), nullable=True),
        sa.Column('device_fingerprint', sa.String(64), nullable=True),
        sa.Column('failure_code', sa.String(60), nullable=True),
        sa.Column('failure_message', sa.Text(), nullable=True),
        sa.Column('correlation_id', sa.String(40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_signups_email', 'commercial_signups', ['email'])
    op.create_index('ix_commercial_signups_state', 'commercial_signups', ['state'])
    op.create_index('ix_commercial_signups_organization_id', 'commercial_signups', ['organization_id'])
    op.create_index('ix_commercial_signups_correlation_id', 'commercial_signups', ['correlation_id'])
    op.create_index('ix_commercial_signups_ip_address', 'commercial_signups', ['ip_address'])
    op.create_index('ix_commercial_signups_user_agent_hash', 'commercial_signups', ['user_agent_hash'])
    op.create_index('ix_commercial_signups_device_fingerprint', 'commercial_signups', ['device_fingerprint'])
    op.create_index('ix_commercial_signups_created_at', 'commercial_signups', ['created_at'])

    # commercial_events
    op.create_table(
        'commercial_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.String(40), nullable=False, unique=True),
        sa.Column('event_type', sa.String(80), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_service', sa.String(60), nullable=False),
        sa.Column('tenant_id', sa.String(40), nullable=True),
        sa.Column('organization_id', sa.String(40), nullable=True),
        sa.Column('workspace_id', sa.String(40), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('correlation_id', sa.String(40), nullable=True),
        sa.Column('payload', JSON, nullable=False, server_default='{}'),
        sa.Column('state_from', sa.String(60), nullable=True),
        sa.Column('state_to', sa.String(60), nullable=True),
        sa.Column('error_code', sa.String(60), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_events_event_id', 'commercial_events', ['event_id'])
    op.create_index('ix_commercial_events_event_type', 'commercial_events', ['event_type'])
    op.create_index('ix_commercial_events_occurred_at', 'commercial_events', ['occurred_at'])
    op.create_index('ix_commercial_events_tenant_id', 'commercial_events', ['tenant_id'])
    op.create_index('ix_commercial_events_organization_id', 'commercial_events', ['organization_id'])
    op.create_index('ix_commercial_events_workspace_id', 'commercial_events', ['workspace_id'])
    op.create_index('ix_commercial_events_user_id', 'commercial_events', ['user_id'])
    op.create_index('ix_commercial_events_correlation_id', 'commercial_events', ['correlation_id'])
    op.create_index('ix_commercial_events_error_code', 'commercial_events', ['error_code'])
    op.create_index('ix_commercial_events_created_at', 'commercial_events', ['created_at'])
    op.create_index('ix_commercial_events_corr_type', 'commercial_events', ['correlation_id', 'event_type'])
    op.create_index('ix_commercial_events_org_type_at', 'commercial_events', ['organization_id', 'event_type', 'occurred_at'])

    # commercial_consents
    op.create_table(
        'commercial_consents',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('organization_id', sa.String(40), nullable=True),
        sa.Column('signup_id', sa.String(40), nullable=True),
        sa.Column('consent_type', sa.String(60), nullable=False),
        sa.Column('version', sa.String(40), nullable=True),
        sa.Column('granted', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent_hash', sa.String(64), nullable=True),
        sa.Column('country', sa.String(2), nullable=True),
        sa.Column('locale', sa.String(10), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_consents_user_id', 'commercial_consents', ['user_id'])
    op.create_index('ix_commercial_consents_organization_id', 'commercial_consents', ['organization_id'])
    op.create_index('ix_commercial_consents_signup_id', 'commercial_consents', ['signup_id'])
    op.create_index('ix_commercial_consents_consent_type', 'commercial_consents', ['consent_type'])
    op.create_index('ix_commercial_consents_occurred_at', 'commercial_consents', ['occurred_at'])

    # commercial_subscriptions
    op.create_table(
        'commercial_subscriptions',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('organization_id', sa.String(40), sa.ForeignKey('commercial_organizations.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('plan_code', sa.String(40), nullable=False, server_default='verified'),
        sa.Column('billing_cycle', sa.String(20), nullable=False, server_default='monthly'),
        sa.Column('seat_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('currency', sa.String(3), nullable=False, server_default='USD'),
        sa.Column('state', sa.String(40), nullable=False, server_default='trial_active'),
        sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('active_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_commercial_subscriptions_organization_id', 'commercial_subscriptions', ['organization_id'])
    op.create_index('ix_commercial_subscriptions_state', 'commercial_subscriptions', ['state'])


def downgrade() -> None:
    op.drop_table('commercial_subscriptions')
    op.drop_table('commercial_consents')
    op.drop_table('commercial_events')
    op.drop_table('commercial_signups')
    op.drop_table('commercial_workspaces')
    op.drop_table('commercial_organizations')
