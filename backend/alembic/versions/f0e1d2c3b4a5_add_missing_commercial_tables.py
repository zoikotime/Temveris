"""add missing commercial tables (bring create_all tables under Alembic)

Revision ID: f0e1d2c3b4a5
Revises: e7ebc3f1fdb5
Create Date: 2026-07-14 00:00:00.000000

Twelve commercial models were previously created only by
``app.commercial.bootstrap.ensure_tables()`` (SQLAlchemy ``create_all``),
never by a migration. That masked schema drift: a new column on any of
these models would silently never ALTER onto an already-provisioned DB.

This migration brings them under version control with explicit DDL. The
other six commercial tables (organizations, workspaces, signups, events,
consents, subscriptions) already ship in
``q1r4s7t0u3v6_create_commercial_tables``.

The op.create_table calls below were produced by ``alembic autogenerate``
against the current models and frozen here so the DDL is explicit and
reviewable and will not drift if the models change later.

NOTE for existing databases: every already-provisioned deploy created these
tables via bootstrap's create_all, so this migration will fail with
"table already exists" if applied naively. Stamp those DBs past this
revision (``alembic stamp f0e1d2c3b4a5``) OR drop the 12 tables first. New /
fresh databases apply it normally. See the deploy runbook.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f0e1d2c3b4a5'
down_revision: Union[str, Sequence[str], None] = 'e7ebc3f1fdb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing deployments already created these 12 tables out-of-band via
    # bootstrap.ensure_tables() (create_all runs them together in one
    # transaction, so the presence of one implies all). Detect that and no-op
    # so `alembic upgrade` is safe everywhere WITHOUT a manual `alembic stamp`.
    # Fresh databases fall through and create everything below.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "commercial_webhook_receipts" in existing:
        return

    op.create_table('commercial_billing_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entry_id', sa.String(length=40), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('subscription_id', sa.String(length=40), nullable=True),
    sa.Column('invoice_id', sa.String(length=40), nullable=True),
    sa.Column('payment_id', sa.String(length=40), nullable=True),
    sa.Column('parent_entry_id', sa.String(length=40), nullable=True),
    sa.Column('entry_type', sa.String(length=40), nullable=False),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('metadata_json', sa.JSON(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_billing_ledger_entry_id'), 'commercial_billing_ledger', ['entry_id'], unique=True)
    op.create_index(op.f('ix_commercial_billing_ledger_entry_type'), 'commercial_billing_ledger', ['entry_type'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_id'), 'commercial_billing_ledger', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_invoice_id'), 'commercial_billing_ledger', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_occurred_at'), 'commercial_billing_ledger', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_organization_id'), 'commercial_billing_ledger', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_parent_entry_id'), 'commercial_billing_ledger', ['parent_entry_id'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_payment_id'), 'commercial_billing_ledger', ['payment_id'], unique=False)
    op.create_index(op.f('ix_commercial_billing_ledger_subscription_id'), 'commercial_billing_ledger', ['subscription_id'], unique=False)
    op.create_index('ix_ledger_org_type_at', 'commercial_billing_ledger', ['organization_id', 'entry_type', 'occurred_at'], unique=False)

    op.create_table('commercial_checkout_sessions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('workspace_id', sa.String(length=40), nullable=True),
    sa.Column('plan_code', sa.String(length=40), nullable=False),
    sa.Column('billing_cycle', sa.String(length=20), nullable=False),
    sa.Column('seat_count', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('success_url', sa.Text(), nullable=True),
    sa.Column('cancel_url', sa.Text(), nullable=True),
    sa.Column('state', sa.String(length=30), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('provider_session_id', sa.String(length=120), nullable=True),
    sa.Column('provider_redirect_url', sa.Text(), nullable=True),
    sa.Column('correlation_id', sa.String(length=40), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recovery_required', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('recovery_reason', sa.Text(), nullable=True),
    sa.Column('recovery_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_checkout_sessions_correlation_id'), 'commercial_checkout_sessions', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_commercial_checkout_sessions_organization_id'), 'commercial_checkout_sessions', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_checkout_sessions_recovery_required'), 'commercial_checkout_sessions', ['recovery_required'], unique=False)
    op.create_index(op.f('ix_commercial_checkout_sessions_state'), 'commercial_checkout_sessions', ['state'], unique=False)

    op.create_table('commercial_entitlements_cache',
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('profile_json', sa.JSON(), nullable=False),
    sa.Column('plan_code', sa.String(length=40), nullable=False),
    sa.Column('commercial_state', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('organization_id')
    )

    op.create_table('commercial_fraud_signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('signup_id', sa.String(length=40), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=True),
    sa.Column('signal_type', sa.String(length=60), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('score_delta', sa.Float(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_fraud_signals_created_at'), 'commercial_fraud_signals', ['created_at'], unique=False)
    op.create_index(op.f('ix_commercial_fraud_signals_id'), 'commercial_fraud_signals', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_fraud_signals_organization_id'), 'commercial_fraud_signals', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_fraud_signals_signal_type'), 'commercial_fraud_signals', ['signal_type'], unique=False)
    op.create_index(op.f('ix_commercial_fraud_signals_signup_id'), 'commercial_fraud_signals', ['signup_id'], unique=False)

    op.create_table('commercial_invoices',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('subscription_id', sa.String(length=40), nullable=True),
    sa.Column('number', sa.String(length=40), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('subtotal_cents', sa.Integer(), nullable=False),
    sa.Column('tax_cents', sa.Integer(), nullable=False),
    sa.Column('discount_cents', sa.Integer(), nullable=False),
    sa.Column('total_cents', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provider', sa.String(length=40), nullable=True),
    sa.Column('provider_invoice_id', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_invoices_number'), 'commercial_invoices', ['number'], unique=True)
    op.create_index(op.f('ix_commercial_invoices_organization_id'), 'commercial_invoices', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_invoices_provider_invoice_id'), 'commercial_invoices', ['provider_invoice_id'], unique=False)
    op.create_index(op.f('ix_commercial_invoices_status'), 'commercial_invoices', ['status'], unique=False)
    op.create_index(op.f('ix_commercial_invoices_subscription_id'), 'commercial_invoices', ['subscription_id'], unique=False)

    op.create_table('commercial_onboarding_checklist',
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('workspace_id', sa.String(length=40), nullable=False),
    sa.Column('email_confirmed', sa.Boolean(), nullable=False),
    sa.Column('first_policy_configured', sa.Boolean(), nullable=False),
    sa.Column('first_invite_sent', sa.Boolean(), nullable=False),
    sa.Column('dashboard_reviewed', sa.Boolean(), nullable=False),
    sa.Column('tools_connected', sa.Boolean(), nullable=False),
    sa.Column('billing_reviewed', sa.Boolean(), nullable=False),
    sa.Column('email_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_policy_configured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_invite_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dashboard_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tools_connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('billing_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('organization_id')
    )
    op.create_index(op.f('ix_commercial_onboarding_checklist_workspace_id'), 'commercial_onboarding_checklist', ['workspace_id'], unique=False)

    op.create_table('commercial_signup_rate_buckets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bucket_key', sa.String(length=160), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bucket_key', 'window_start', name='uq_signup_rate_bucket')
    )
    op.create_index(op.f('ix_commercial_signup_rate_buckets_bucket_key'), 'commercial_signup_rate_buckets', ['bucket_key'], unique=False)
    op.create_index(op.f('ix_commercial_signup_rate_buckets_id'), 'commercial_signup_rate_buckets', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_signup_rate_buckets_window_start'), 'commercial_signup_rate_buckets', ['window_start'], unique=False)

    op.create_table('commercial_trial_emails_sent',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('subscription_id', sa.String(length=40), nullable=True),
    sa.Column('day_marker', sa.String(length=20), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'day_marker', name='uq_trial_email_org_day')
    )
    op.create_index(op.f('ix_commercial_trial_emails_sent_id'), 'commercial_trial_emails_sent', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_trial_emails_sent_organization_id'), 'commercial_trial_emails_sent', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_trial_emails_sent_sent_at'), 'commercial_trial_emails_sent', ['sent_at'], unique=False)
    op.create_index(op.f('ix_commercial_trial_emails_sent_subscription_id'), 'commercial_trial_emails_sent', ['subscription_id'], unique=False)

    op.create_table('commercial_webhook_receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('provider_event_id', sa.String(length=160), nullable=False),
    sa.Column('event_type', sa.String(length=80), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'provider_event_id', name='uq_webhook_provider_event')
    )
    op.create_index(op.f('ix_commercial_webhook_receipts_provider'), 'commercial_webhook_receipts', ['provider'], unique=False)
    op.create_index(op.f('ix_commercial_webhook_receipts_provider_event_id'), 'commercial_webhook_receipts', ['provider_event_id'], unique=False)
    op.create_index(op.f('ix_commercial_webhook_receipts_received_at'), 'commercial_webhook_receipts', ['received_at'], unique=False)
    op.create_index(op.f('ix_commercial_webhook_receipts_status'), 'commercial_webhook_receipts', ['status'], unique=False)

    op.create_table('commercial_email_verification_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resend_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_email_verification_tokens_id'), 'commercial_email_verification_tokens', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_email_verification_tokens_organization_id'), 'commercial_email_verification_tokens', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_email_verification_tokens_token_hash'), 'commercial_email_verification_tokens', ['token_hash'], unique=True)
    op.create_index(op.f('ix_commercial_email_verification_tokens_user_id'), 'commercial_email_verification_tokens', ['user_id'], unique=False)

    op.create_table('commercial_leads',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('intent', sa.String(length=40), nullable=False),
    sa.Column('first_name', sa.String(length=120), nullable=True),
    sa.Column('last_name', sa.String(length=120), nullable=True),
    sa.Column('work_email', sa.String(length=255), nullable=False),
    sa.Column('company_name', sa.String(length=255), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('expected_users', sa.Integer(), nullable=True),
    sa.Column('industry', sa.String(length=80), nullable=True),
    sa.Column('deployment_pref', sa.String(length=40), nullable=True),
    sa.Column('integrations', sa.Text(), nullable=True),
    sa.Column('compliance_needs', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('routing_score', sa.Float(), nullable=False),
    sa.Column('assigned_to_user_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('utm_source', sa.String(length=120), nullable=True),
    sa.Column('utm_medium', sa.String(length=120), nullable=True),
    sa.Column('utm_campaign', sa.String(length=120), nullable=True),
    sa.Column('referrer', sa.Text(), nullable=True),
    sa.Column('crm_external_id', sa.String(length=160), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['assigned_to_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_leads_created_at'), 'commercial_leads', ['created_at'], unique=False)
    op.create_index(op.f('ix_commercial_leads_crm_external_id'), 'commercial_leads', ['crm_external_id'], unique=False)
    op.create_index(op.f('ix_commercial_leads_status'), 'commercial_leads', ['status'], unique=False)
    op.create_index(op.f('ix_commercial_leads_work_email'), 'commercial_leads', ['work_email'], unique=False)

    op.create_table('commercial_manual_review',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.String(length=40), nullable=False),
    sa.Column('signup_id', sa.String(length=40), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=True),
    sa.Column('reason', sa.String(length=120), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('fraud_score', sa.Float(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('outcome', sa.String(length=40), nullable=True),
    sa.Column('reviewer_user_id', sa.Integer(), nullable=True),
    sa.Column('review_notes', sa.Text(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['reviewer_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commercial_manual_review_case_id'), 'commercial_manual_review', ['case_id'], unique=True)
    op.create_index(op.f('ix_commercial_manual_review_created_at'), 'commercial_manual_review', ['created_at'], unique=False)
    op.create_index(op.f('ix_commercial_manual_review_id'), 'commercial_manual_review', ['id'], unique=False)
    op.create_index(op.f('ix_commercial_manual_review_organization_id'), 'commercial_manual_review', ['organization_id'], unique=False)
    op.create_index(op.f('ix_commercial_manual_review_signup_id'), 'commercial_manual_review', ['signup_id'], unique=False)
    op.create_index(op.f('ix_commercial_manual_review_status'), 'commercial_manual_review', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_commercial_manual_review_status'), table_name='commercial_manual_review')
    op.drop_index(op.f('ix_commercial_manual_review_signup_id'), table_name='commercial_manual_review')
    op.drop_index(op.f('ix_commercial_manual_review_organization_id'), table_name='commercial_manual_review')
    op.drop_index(op.f('ix_commercial_manual_review_id'), table_name='commercial_manual_review')
    op.drop_index(op.f('ix_commercial_manual_review_created_at'), table_name='commercial_manual_review')
    op.drop_index(op.f('ix_commercial_manual_review_case_id'), table_name='commercial_manual_review')
    op.drop_table('commercial_manual_review')
    op.drop_index(op.f('ix_commercial_leads_work_email'), table_name='commercial_leads')
    op.drop_index(op.f('ix_commercial_leads_status'), table_name='commercial_leads')
    op.drop_index(op.f('ix_commercial_leads_crm_external_id'), table_name='commercial_leads')
    op.drop_index(op.f('ix_commercial_leads_created_at'), table_name='commercial_leads')
    op.drop_table('commercial_leads')
    op.drop_index(op.f('ix_commercial_email_verification_tokens_user_id'), table_name='commercial_email_verification_tokens')
    op.drop_index(op.f('ix_commercial_email_verification_tokens_token_hash'), table_name='commercial_email_verification_tokens')
    op.drop_index(op.f('ix_commercial_email_verification_tokens_organization_id'), table_name='commercial_email_verification_tokens')
    op.drop_index(op.f('ix_commercial_email_verification_tokens_id'), table_name='commercial_email_verification_tokens')
    op.drop_table('commercial_email_verification_tokens')
    op.drop_index(op.f('ix_commercial_webhook_receipts_status'), table_name='commercial_webhook_receipts')
    op.drop_index(op.f('ix_commercial_webhook_receipts_received_at'), table_name='commercial_webhook_receipts')
    op.drop_index(op.f('ix_commercial_webhook_receipts_provider_event_id'), table_name='commercial_webhook_receipts')
    op.drop_index(op.f('ix_commercial_webhook_receipts_provider'), table_name='commercial_webhook_receipts')
    op.drop_table('commercial_webhook_receipts')
    op.drop_index(op.f('ix_commercial_trial_emails_sent_subscription_id'), table_name='commercial_trial_emails_sent')
    op.drop_index(op.f('ix_commercial_trial_emails_sent_sent_at'), table_name='commercial_trial_emails_sent')
    op.drop_index(op.f('ix_commercial_trial_emails_sent_organization_id'), table_name='commercial_trial_emails_sent')
    op.drop_index(op.f('ix_commercial_trial_emails_sent_id'), table_name='commercial_trial_emails_sent')
    op.drop_table('commercial_trial_emails_sent')
    op.drop_index(op.f('ix_commercial_signup_rate_buckets_window_start'), table_name='commercial_signup_rate_buckets')
    op.drop_index(op.f('ix_commercial_signup_rate_buckets_id'), table_name='commercial_signup_rate_buckets')
    op.drop_index(op.f('ix_commercial_signup_rate_buckets_bucket_key'), table_name='commercial_signup_rate_buckets')
    op.drop_table('commercial_signup_rate_buckets')
    op.drop_index(op.f('ix_commercial_onboarding_checklist_workspace_id'), table_name='commercial_onboarding_checklist')
    op.drop_table('commercial_onboarding_checklist')
    op.drop_index(op.f('ix_commercial_invoices_subscription_id'), table_name='commercial_invoices')
    op.drop_index(op.f('ix_commercial_invoices_status'), table_name='commercial_invoices')
    op.drop_index(op.f('ix_commercial_invoices_provider_invoice_id'), table_name='commercial_invoices')
    op.drop_index(op.f('ix_commercial_invoices_organization_id'), table_name='commercial_invoices')
    op.drop_index(op.f('ix_commercial_invoices_number'), table_name='commercial_invoices')
    op.drop_table('commercial_invoices')
    op.drop_index(op.f('ix_commercial_fraud_signals_signup_id'), table_name='commercial_fraud_signals')
    op.drop_index(op.f('ix_commercial_fraud_signals_signal_type'), table_name='commercial_fraud_signals')
    op.drop_index(op.f('ix_commercial_fraud_signals_organization_id'), table_name='commercial_fraud_signals')
    op.drop_index(op.f('ix_commercial_fraud_signals_id'), table_name='commercial_fraud_signals')
    op.drop_index(op.f('ix_commercial_fraud_signals_created_at'), table_name='commercial_fraud_signals')
    op.drop_table('commercial_fraud_signals')
    op.drop_table('commercial_entitlements_cache')
    op.drop_index(op.f('ix_commercial_checkout_sessions_state'), table_name='commercial_checkout_sessions')
    op.drop_index(op.f('ix_commercial_checkout_sessions_recovery_required'), table_name='commercial_checkout_sessions')
    op.drop_index(op.f('ix_commercial_checkout_sessions_organization_id'), table_name='commercial_checkout_sessions')
    op.drop_index(op.f('ix_commercial_checkout_sessions_correlation_id'), table_name='commercial_checkout_sessions')
    op.drop_table('commercial_checkout_sessions')
    op.drop_index('ix_ledger_org_type_at', table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_subscription_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_payment_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_parent_entry_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_organization_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_occurred_at'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_invoice_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_id'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_entry_type'), table_name='commercial_billing_ledger')
    op.drop_index(op.f('ix_commercial_billing_ledger_entry_id'), table_name='commercial_billing_ledger')
    op.drop_table('commercial_billing_ledger')
