"""Permanent deletion of a tenant organization and everything it owns.

WHY A MANIFEST AND NOT `db.delete(org)`
---------------------------------------
A forensic pass over the live schema found that the ORM's declared delete
policy and the database's actual constraints have diverged substantially:
60 tables carry an ``organization_id`` column, but only 20 have a foreign key
to ``organizations``. Of those 20, four are ``ON DELETE RESTRICT``
(``app_settings``, ``holidays``, ``shifts``, ``shift_assignments``) and every
provisioned tenant has rows in at least one, so ``DELETE FROM organizations``
fails outright. Force past that and the remaining 15 ``SET NULL`` rows land in
the shared ``organization_id IS NULL`` bucket — the exact cross-tenant exposure
repaired by migration ``c4a7e2b915d3`` — while the 40 unconstrained tables keep
a dangling integer nobody will ever notice.

So deletion cannot be delegated to the database. It is an explicit, ordered,
reviewed manifest, and the manifest is validated against the live schema at
call time (see ``coverage_report``) so a table added later cannot quietly go
unhandled. ``tests/test_organization_lifecycle.py`` asserts that every table
carrying an ``organization_id`` is in exactly one of DELETE or KEEP.

TRANSACTION OWNERSHIP
---------------------
``purge_organization`` is FLUSH-ONLY, matching ``app.org.provisioning_service``:
it never commits and never rolls back. The caller owns the transaction so the
whole purge is one atomic unit and a failure anywhere leaves the tenant fully
intact. This mirrors the provisioning path exactly, which is what makes
"provision then delete" symmetric.

SCOPING RULES — the part that must never be wrong
--------------------------------------------------
Every statement is filtered by exactly one of:

  * ``organization_id == <operational int id>``   — operational tables
  * ``organization_id == <commercial str id>``    — commercial tables
  * ``tenant_id == str(<operational int id>)``    — offboarding tables (verified
    against live data: ``offboarding_cases.tenant_id`` holds '1', '6' — the
    stringified operational organization id)
  * ``user_id IN (<this org's user ids>)``        — tables with no tenant column

There is no unfiltered DELETE anywhere in this module, and no statement takes
an id from a caller-supplied payload — the router resolves the organization
server-side and passes the row.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import models

_log = logging.getLogger("zoiko.org.deletion")


class OrganizationDeletionError(Exception):
    """Refusal to delete, with an HTTP status and a client-safe reason."""

    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


# ──────────────────────────────────────────────────────────────────────────
# Manifest
# ──────────────────────────────────────────────────────────────────────────
# Ordered children-first. Within a group order does not matter; between groups
# it does. Tables absent from the connected database are skipped, so the same
# manifest works against a partially-migrated dev database and against prod.

#: Keyed on the operational (integer) organization id.
OPERATIONAL_TABLES: Sequence[str] = (
    # ── Activity + tracking (highest volume, and children of clock_sessions) ──
    "activities",
    "browser_activities",
    "app_sessions",
    "live_statuses",
    "offline_intervals",
    "break_sessions",
    "clock_sessions",
    # ── Leave ────────────────────────────────────────────────────────────────
    "leave_balance_adjustments",
    "employee_leave_balances",
    "leave_requests",
    "leave_policies",
    # ── Payroll ──────────────────────────────────────────────────────────────
    # `payroll_items` is NOT here: it is user-scoped in the real schema. See
    # USER_SCOPED_TABLES.
    "payroll_runs",
    "payroll_configs",
    # ── Schedule ─────────────────────────────────────────────────────────────
    "shift_assignments",
    "shifts",
    "employee_availability",
    "holidays",
    # ── Proof of work / evidence / risk ──────────────────────────────────────
    "work_evidence_chain",
    "work_events",
    "work_records",
    # `integrity_scores` is NOT here — user-scoped in the real schema.
    "fraud_events",
    "risk_alerts",
    # ── Intelligence ─────────────────────────────────────────────────────────
    # `daily_intelligence_summaries` is NOT here — user-scoped in the real schema.
    # ── Categorisation ───────────────────────────────────────────────────────
    "app_categories",
    "domain_categories",
    # ── Security centre ──────────────────────────────────────────────────────
    "security_break_glass_accounts",
    "security_exceptions",
    "security_trusted_domains",
    # ── Audit governance centre ──────────────────────────────────────────────
    "audit_data_exports",
    "audit_evidence_packages",
    "audit_legal_holds",
    "audit_policy_versions",
    "audit_retention_rules",
    # ── Tenant audit trail ───────────────────────────────────────────────────
    # The platform-level ORGANIZATION_DELETED record is written with
    # organization_id = NULL precisely so it is NOT matched here and survives.
    "audit_logs",
    # ── Billing (organization_id is an INTEGER FK to organizations.id) ───────
    # Migration `e2c9a4d81f63` converted these from VARCHAR(80) to an INTEGER
    # FK, so they are scoped by the OPERATIONAL id and belong here. They used
    # to sit in COMMERCIAL_TABLES, which fed them the commercial string id and
    # made every deletion fail with InvalidTextRepresentation.
    #
    # The organization FK is ON DELETE SET NULL, so dropping `organizations`
    # would orphan these rows rather than remove them — the explicit DELETE is
    # what actually clears them. `billing_plans` is deliberately absent: it is
    # the shared platform price catalogue (see KEEP_TABLES).
    #
    # Order is children-first: `billing_invoices.subscription_id` references
    # `billing_subscriptions`, so invoices must go first.
    "billing_audit_events",
    "billing_enterprise_quotes",
    "billing_downgrade_requests",
    "billing_tax_profiles",
    "billing_contacts",
    "billing_payment_methods",
    "billing_invoices",
    "billing_subscriptions",
    # ── Configuration (RESTRICT tables — must precede `organizations`) ───────
    "app_settings",
    "org_settings",
    "teams",
    # ── Legacy tenancy stubs (CASCADE, cleared explicitly for determinism) ───
    "tenants",
    "workspaces",
)

#: Keyed on the commercial (string) organization id.
COMMERCIAL_TABLES: Sequence[str] = (
    "commercial_email_verification_tokens",
    "commercial_trial_emails_sent",
    "commercial_onboarding_checklist",
    "commercial_entitlements_cache",
    "commercial_manual_review",
    "commercial_fraud_signals",
    "commercial_price_quotes",
    "commercial_checkout_sessions",
    "commercial_billing_ledger",
    "commercial_invoices",
    "commercial_consents",
    "commercial_events",
    "commercial_signups",
    # These two also CASCADE from commercial_organizations; deleted explicitly
    # so the row counts in the result are complete rather than invisible.
    "commercial_subscriptions",
    "commercial_workspaces",
)

#: Keyed on ``tenant_id == str(operational_org_id)``.
OFFBOARDING_TABLES: Sequence[str] = (
    "offboarding_telemetry_quarantine",
    "offboarding_portal_access_log",
    "offboarding_portal_mfa",
    "offboarding_portal_sessions",
    "offboarding_workflow_waivers",
    "offboarding_workflow_blockers",
    "offboarding_domain_events",
    "offboarding_audit_events",
    "offboarding_security_incidents",
    "offboarding_support_tickets",
    "offboarding_data_rights_requests",
    "offboarding_deletion_reviews",
    "offboarding_retention_policies",
    "integration_reconciliation_queue",
    "completion_certificates",
    "evidence_packages",
    "legal_holds",
    "retention_assignments",
    "former_worker_access",
    "access_revocation_records",
    "offboarding_tasks",
    "offboarding_cases",
)

#: Tables with no usable ``organization_id`` in the real schema, reachable only
#: through the org's users.
#: Most of these also CASCADE from ``users``; they are deleted explicitly first
#: so the purge does not depend on constraint behaviour that the audit showed
#: cannot be trusted, and so the reported row counts are real.
#: ``employees`` and ``leave_requests`` are the two that genuinely MUST be here:
#: their ``user_id`` FK is ``NO ACTION`` and would otherwise block the delete.
#:
#: The last three were moved here from OPERATIONAL_TABLES. Their ORM models
#: declare an ``organization_id`` (database/models.py) but **no migration ever
#: added the column**, so ``DELETE ... WHERE organization_id = :oid`` raised
#: UndefinedColumn against the real database and rolled the whole purge back —
#: deletion could never succeed in production. They are genuinely user-scoped
#: there (``user_id`` present, ``ON DELETE CASCADE`` to ``users``), so reaching
#: them through the organization's users is the semantically correct route, not
#: a workaround: the same rows are removed, by the relationship the schema
#: actually has.
USER_SCOPED_TABLES: Sequence[str] = (
    "activity_minute_scores",
    "activity_daily_scores",
    "activity_windows",
    "focus_sessions",
    "workflow_transitions",
    "time_adjustments",
    "employees",
    "password_reset_otps",
    "user_preferences",
    "user_sessions",
    "refresh_tokens",
    "memberships",
    # ── Moved from OPERATIONAL_TABLES: no organization_id in the real schema ──
    "payroll_items",
    "integrity_scores",
    "daily_intelligence_summaries",
)

#: Column used to reach users in USER_SCOPED_TABLES when it is not ``user_id``.
USER_SCOPED_COLUMN_OVERRIDES: Dict[str, str] = {
    "password_reset_otps": "employee_id",
}

#: Deleted, but by an explicit named statement rather than one of the sweeps
#: above — because each needs its own ordering guarantee. Listed so the coverage
#: report does not report them as gaps.
DELETED_BY_NAME: Dict[str, str] = {
    "users": "Step 6 — after every table holding a NO ACTION FK to users.",
    "commercial_organizations": "Step 7 — after its children, before the operational row.",
    "organizations": "Step 7 — last, once every RESTRICT child is gone.",
}

#: Tables that carry a tenant key but are deliberately NOT deleted. The coverage
#: test requires every such table to be listed here with a reason, so "we forgot"
#: and "we decided" can never look the same.
KEEP_TABLES: Dict[str, str] = {}

#: Tables with no ``organization_id`` that are global by design. Listed so the
#: coverage test can assert they were considered rather than missed.
GLOBAL_TABLES: Dict[str, str] = {
    "billing_plans": "Platform price catalogue, shared by every tenant.",
    "commercial_webhook_receipts": (
        "Payment-provider idempotency ledger. Deleting rows would let a "
        "replayed webhook be processed twice."
    ),
    "commercial_idempotency_keys": "Request-replay guard, not tenant data.",
    "commercial_signup_rate_buckets": "Abuse throttling counters keyed by IP/domain.",
    "commercial_leads": "Pre-tenant CRM records; a lead outlives any org it created.",
    "email_suppressions": (
        "Global unsubscribe/bounce list. Removing an address here would resume "
        "mailing someone who opted out."
    ),
    "outbox_events": "Transactional outbox; drained by the dispatcher, not tenant-owned.",
    "chatbot_conversations": "KAIROS has no tenant column — see the note in purge_organization().",
    "chatbot_messages": "KAIROS has no tenant column.",
    "chatbot_unknown_prompts": "KAIROS has no tenant column.",
    "chatbot_email_rate_limits": "KAIROS has no tenant column.",
}

#: Legacy tables that exist in the live database but have NO tenant key of any
#: kind — no ``organization_id``, no ``tenant_id``, and no reliable relationship
#: back to one. They are classified here rather than deleted because there is no
#: correct filter to write: scoping them by email or company name would be a
#: guess, and a wrong guess deletes another tenant's rows.
#:
#: Listed explicitly so ``coverage_report`` reports them as *classified* instead
#: of leaving them invisible — the coverage check previously only inspected
#: tables that already had a tenant key, so an untenanted table could never be
#: flagged at all.
#:
#: These are candidates for a future tenant-key migration, not for deletion.
LEGACY_UNTENANTED_TABLES: Dict[str, str] = {
    "chats": (
        "Legacy KAIROS chat messages, keyed by session_id/user_email only. No "
        "tenant column; superseded by chatbot_messages."
    ),
    "conversations": (
        "Legacy KAIROS conversations. Carries free-text `company` and "
        "`user_email` but no organization key — matching on either would be a "
        "guess that could delete another tenant's history."
    ),
    "new_prompts": (
        "Legacy unmatched-prompt counter keyed by the prompt text itself. "
        "Platform-level curation data, not tenant-owned."
    ),
    "email_rate_limits": (
        "Legacy rate-limit counters keyed by an opaque throttle key. Deleting "
        "rows would reset throttling, not remove tenant data."
    ),
    "activity": (
        "Legacy singular activity table superseded by `activities`. User-keyed "
        "only, no organization column."
    ),
    "departments": (
        "Legacy department lookup with no organization column; the live product "
        "uses `teams`, which IS org-scoped and IS deleted."
    ),
}


# ──────────────────────────────────────────────────────────────────────────
# Schema introspection
# ──────────────────────────────────────────────────────────────────────────
# Introspection is dialect-aware on purpose. Production runs on Postgres over a
# pooled remote connection where a per-table `get_columns()` sweep is ~140 round
# trips (measured: well over two minutes); `information_schema` answers the same
# question in one. The test suite runs on in-memory SQLite (tests/conftest.py),
# which has no information_schema, so the inspector is the fallback there —
# local, and cheap enough that the round-trip count does not matter.
def _is_postgres(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


def _existing_tables(db: Session) -> Set[str]:
    try:
        if _is_postgres(db):
            rows = db.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )).fetchall()
            return {r[0] for r in rows}
        from sqlalchemy import inspect as _sa_inspect
        return set(_sa_inspect(db.get_bind()).get_table_names())
    except Exception:
        _log.exception("Could not introspect table list")
        return set()


def tables_with_column(db: Session, column: str) -> Set[str]:
    try:
        if _is_postgres(db):
            rows = db.execute(text(
                "SELECT c.table_name FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.table_name = c.table_name AND t.table_schema = c.table_schema "
                "WHERE c.table_schema = 'public' AND c.column_name = :col "
                "  AND t.table_type = 'BASE TABLE'"
            ), {"col": column}).fetchall()
            return {r[0] for r in rows}

        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(db.get_bind())
        out: Set[str] = set()
        for table in insp.get_table_names():
            try:
                if any(c["name"] == column for c in insp.get_columns(table)):
                    out.add(table)
            except Exception:
                continue
        return out
    except Exception:
        _log.exception("Could not introspect columns for %r", column)
        return set()


#: Infrastructure tables that are not application data at all.
_NON_APPLICATION_TABLES = frozenset({"alembic_version"})


def scope_manifest() -> List[tuple]:
    """``(table, scope_column)`` for every table a sweep deletes by predicate.

    Single source of truth for "what column does this table get filtered on",
    shared by the purge and by ``coverage_report`` so the check and the
    behaviour can never disagree.
    """
    pairs: List[tuple] = []
    pairs += [(t, "organization_id") for t in OPERATIONAL_TABLES]
    pairs += [(t, "organization_id") for t in COMMERCIAL_TABLES]
    pairs += [(t, "tenant_id") for t in OFFBOARDING_TABLES]
    pairs += [
        (t, USER_SCOPED_COLUMN_OVERRIDES.get(t, "user_id"))
        for t in USER_SCOPED_TABLES
    ]
    return pairs


def _missing_scope_columns(db: Session, present: Set[str]) -> List[tuple]:
    """Manifest entries whose scope column is absent from the live schema.

    This is the exact defect that made deletion impossible: three tables were
    listed as ``organization_id``-scoped because their ORM models declare that
    column, but no migration ever added it, so the DELETE raised UndefinedColumn
    and rolled the whole purge back.
    """
    out: List[tuple] = []
    by_column: Dict[str, Set[str]] = {}
    for _table, column in scope_manifest():
        if column not in by_column:
            by_column[column] = tables_with_column(db, column)
    for table, column in scope_manifest():
        if table not in present:
            continue
        if table not in by_column[column]:
            out.append((table, column))
    return out


def classified_tables() -> Set[str]:
    """Every table the manifest has an explicit opinion about — delete or keep."""
    return (
        set(OPERATIONAL_TABLES)
        | set(COMMERCIAL_TABLES)
        | set(OFFBOARDING_TABLES)
        | set(USER_SCOPED_TABLES)
        | set(DELETED_BY_NAME)
        | set(KEEP_TABLES)
        | set(GLOBAL_TABLES)
        | set(LEGACY_UNTENANTED_TABLES)
        | set(_NON_APPLICATION_TABLES)
    )


def coverage_report(db: Session) -> Dict[str, List[str]]:
    """Which tables the manifest does and does not account for.

    Used by the coverage test and by ``purge_organization``'s pre-flight log so
    a table added in a later migration surfaces as a named gap instead of an
    orphaned row nobody looks for.

    ``unclassified`` covers the blind spot the first version had: it only
    inspected tables that ALREADY carried a tenant key, so a table with no
    tenant column could never be flagged — which is exactly how six legacy
    tables (``chats``, ``conversations``, …) stayed invisible. Every table in
    the database must now be named somewhere, even if the answer is "leave it
    alone, and here is why".

    ``scope_column_missing`` catches the failure that made deletion impossible
    in production: a table listed in a scope manifest whose scope column does
    not exist in the connected database.
    """
    handled = classified_tables()
    present = _existing_tables(db)
    org_keyed = tables_with_column(db, "organization_id") & present
    tenant_keyed = tables_with_column(db, "tenant_id") & present
    return {
        "unclassified": sorted(present - handled),
        "scope_column_missing": sorted(
            f"{t}.{c}" for t, c in _missing_scope_columns(db, present)
        ),
        "unhandled_organization_id": sorted(org_keyed - handled),
        "unhandled_tenant_id": sorted(tenant_keyed - handled),
        "manifest_not_in_schema": sorted(handled - present - set(GLOBAL_TABLES)),
    }


# ──────────────────────────────────────────────────────────────────────────
# Pre-flight safety gates
# ──────────────────────────────────────────────────────────────────────────
#: Provider names that mean "no external billing object exists".
_LOCAL_PROVIDERS = {"stub", "manual", "", None}

#: Subscription states in which no money can still be moving.
_SETTLED_SUBSCRIPTION_STATES = {
    "cancelled", "closed", "trial_closed", "trial_expired", "suspended",
}


def assert_billing_safe(db: Session, commercial_org_id: Optional[str]) -> Dict[str, object]:
    """Refuse to delete a tenant that still has a live payment-provider object.

    Deliberately does NOT call Stripe, cancel anything, or touch billing state —
    the billing lifecycle is owned by ``app.commercial.subscription_ops`` and
    ``app.commercial.routers.webhooks`` and is out of scope here. This only
    *reads*, and refuses, because the alternative is worse than refusing:
    ``commercial_subscriptions`` CASCADEs off ``commercial_organizations``, so
    deleting the tenant destroys ``provider_subscription_id`` — the only handle
    back to the remote object — while the subscription keeps renewing and
    charging the customer.

    Returns a summary dict for the audit payload when deletion is allowed.
    """
    summary: Dict[str, object] = {"provider": None, "subscription_state": None,
                                  "provider_subscription_id": None}
    if not commercial_org_id:
        return summary

    try:
        from app.commercial.models import CommercialSubscription
    except Exception:  # pragma: no cover - commercial layer not migrated
        return summary

    sub = (
        db.query(CommercialSubscription)
          .filter(CommercialSubscription.organization_id == commercial_org_id)
          .one_or_none()
    )
    if sub is None:
        return summary

    summary = {
        "provider": sub.provider,
        "subscription_state": sub.state,
        "provider_subscription_id": sub.provider_subscription_id,
    }

    external = (sub.provider not in _LOCAL_PROVIDERS) or bool(sub.provider_subscription_id)
    settled = (sub.state or "").strip().lower() in _SETTLED_SUBSCRIPTION_STATES
    if external and not settled:
        raise OrganizationDeletionError(
            status_code=409,
            code="billing_subscription_active",
            detail=(
                "This organization still has an active billing subscription "
                f"({sub.provider}, state '{sub.state}'). Cancel the subscription "
                "with the billing provider first — deleting the organization now "
                "would remove the only reference to it while it keeps renewing."
            ),
        )
    return summary


def assert_operational_link(commercial_org) -> int:
    """Refuse to delete a tenant whose operational link is missing.

    ``commercial_organizations.operational_org_id`` is ``ON DELETE SET NULL``,
    and self-serve organizations provisioned before that link existed still have
    it NULL (see scripts/inventory_orphan_self_service_tenants.py). Without the
    link the purge can only reach the commercial rows: the operational tables
    and — critically — the tenant's ``users`` would survive, while the endpoint
    reported success. A half-deleted tenant that looks deleted is worse than a
    refusal, so this refuses.

    Deliberately does NOT try to infer the operational organization. Guessing
    which tenant a row belongs to is how cross-tenant deletions happen; if the
    link is broken it is a data-repair job for an operator, not something to
    resolve inside a destructive endpoint.
    """
    op_id = getattr(commercial_org, "operational_org_id", None)
    if op_id is None:
        raise OrganizationDeletionError(
            status_code=409,
            code="organization_operational_link_missing",
            detail=(
                "This organization has no operational_org_id, so its users and "
                "operational data cannot be located. Deleting it now would "
                "remove the commercial record and leave the tenant's data "
                "behind. Repair the link before deleting."
            ),
        )
    return int(op_id)


def assert_no_legal_hold(db: Session, operational_org_id: int) -> None:
    """Refuse to delete a tenant with an unreleased legal hold.

    ``audit_legal_holds`` exists to freeze a tenant's records for a matter. A
    purge that ignored it would destroy the very evidence the hold was placed
    to preserve.
    """
    if "audit_legal_holds" not in _existing_tables(db):
        return
    active = db.execute(text(
        "SELECT count(*) FROM audit_legal_holds "
        "WHERE organization_id = :oid AND released_at IS NULL"
    ), {"oid": operational_org_id}).scalar() or 0
    if active:
        raise OrganizationDeletionError(
            status_code=409,
            code="legal_hold_active",
            detail=(
                f"This organization has {active} active legal hold(s). Release "
                "them before deleting, or the held records will be destroyed."
            ),
        )


# ──────────────────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class PurgeResult:
    organization_id: Optional[str]
    operational_org_id: Optional[int]
    name: str
    deleted_rows: Dict[str, int] = field(default_factory=dict)
    user_ids: List[int] = field(default_factory=list)
    storage_paths: List[str] = field(default_factory=list)
    billing: Dict[str, object] = field(default_factory=dict)
    coverage_gaps: Dict[str, List[str]] = field(default_factory=dict)
    #: Manifest tables skipped because their scope column is absent from this
    #: database. Always empty on a correct manifest; surfaced in the API
    #: response so a mis-classified table is visible to the operator.
    skipped_tables: List[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.deleted_rows.values())


# ──────────────────────────────────────────────────────────────────────────
# The purge
# ──────────────────────────────────────────────────────────────────────────
def purge_organization(db: Session, commercial_org) -> PurgeResult:
    """Delete a tenant and everything it owns. FLUSH-ONLY — caller commits.

    ``commercial_org`` is a ``CommercialOrganization`` row the caller has
    already resolved and authorised. Nothing here accepts a raw id from a
    request payload.
    """
    op_id: Optional[int] = getattr(commercial_org, "operational_org_id", None)
    comm_id: Optional[str] = getattr(commercial_org, "id", None)
    name = getattr(commercial_org, "name", "") or ""

    result = PurgeResult(organization_id=comm_id, operational_org_id=op_id, name=name)

    present = _existing_tables(db)
    result.coverage_gaps = coverage_report(db)
    if result.coverage_gaps["unhandled_organization_id"] or \
       result.coverage_gaps["unhandled_tenant_id"] or \
       result.coverage_gaps["unclassified"]:
        # Not fatal — a new table with no rows for this tenant is harmless, and
        # refusing every delete because an unrelated migration landed would be
        # worse. Logged loudly so it is visible, and asserted on by the
        # coverage test so it gets fixed rather than accumulating.
        _log.warning(
            "Tenant purge manifest has gaps: %s", result.coverage_gaps,
        )

    # Scope columns that the connected database does not actually have. A
    # manifest entry listed against a column that was never migrated used to
    # raise UndefinedColumn mid-purge and roll the entire transaction back, so
    # deletion could never complete. Resolved ONCE up front (introspection is a
    # query, not a per-table cost) and skipped below with a loud log.
    #
    # This is a backstop, not a substitute for classifying tables correctly:
    # anything appearing here is a manifest bug and is asserted against by
    # tests/test_organization_lifecycle.py.
    _missing = {t for t, _c in _missing_scope_columns(db, present)}
    if _missing:
        _log.error(
            "Tenant purge SKIPPING tables whose scope column is missing from "
            "this database — the manifest is wrong for them and their rows will "
            "NOT be removed by predicate: %s", sorted(_missing),
        )
        result.skipped_tables = sorted(_missing)

    def _delete(table: str, where: str, params: dict) -> None:
        # `table` and `where` are module constants, never request input — the
        # only values that reach SQL from outside are bound parameters.
        if table not in present or table in _missing:
            return
        n = db.execute(text(f"DELETE FROM {table} WHERE {where}"), params).rowcount or 0
        if n:
            result.deleted_rows[table] = result.deleted_rows.get(table, 0) + n

    def _delete_in(table: str, column: str, ids: Sequence[int]) -> None:
        """`DELETE ... WHERE col IN (...)` in chunks.

        An expanding bindparam keeps this portable across SQLite and Postgres
        (``= ANY(:ids)`` is Postgres-only), and the chunking keeps the statement
        under SQLite's variable limit for a tenant with thousands of users.
        """
        if table not in present or table in _missing or not ids:
            return
        from sqlalchemy import bindparam
        stmt = (text(f"DELETE FROM {table} WHERE {column} IN :ids")
                .bindparams(bindparam("ids", expanding=True)))
        total = 0
        for i in range(0, len(ids), 500):
            total += db.execute(stmt, {"ids": list(ids[i:i + 500])}).rowcount or 0
        if total:
            result.deleted_rows[table] = result.deleted_rows.get(table, 0) + total

    # ── Pre-flight: collect what we need BEFORE the rows are gone ────────────
    if op_id is not None:
        result.user_ids = [
            r[0] for r in db.query(models.User.id)
                            .filter(models.User.organization_id == op_id)
                            .all()
        ]
        result.storage_paths = _collect_storage_paths(db, op_id, present)

    # ── 1. User-scoped tables with no tenant column ──────────────────────────
    # First, because `employees` and `leave_requests` hold NO ACTION FKs to
    # users and would otherwise block step 6.
    if result.user_ids:
        for table in USER_SCOPED_TABLES:
            col = USER_SCOPED_COLUMN_OVERRIDES.get(table, "user_id")
            _delete_in(table, col, result.user_ids)

    # ── 2. Operational tenant data ───────────────────────────────────────────
    if op_id is not None:
        for table in OPERATIONAL_TABLES:
            _delete(table, "organization_id = :oid", {"oid": op_id})

    # ── 3. Offboarding (tenant_id is the stringified operational id) ─────────
    if op_id is not None:
        tenant_key = str(op_id)
        for table in OFFBOARDING_TABLES:
            _delete(table, "tenant_id = :tid", {"tid": tenant_key})

    # ── 4. Commercial + billing rows keyed on the string org id ──────────────
    if comm_id:
        for table in COMMERCIAL_TABLES:
            _delete(table, "organization_id = :cid", {"cid": comm_id})

    # ── 5. Sever inbound references from rows that deliberately survive ──────
    # `commercial_organizations.owner_user_id` and `users.invited_by_id` are
    # SET NULL, but the commercial row is deleted in step 7 and other tenants'
    # users can never point at ours, so no extra work is needed. This step
    # exists for the one case that is NOT covered by a constraint: another
    # tenant's row referencing ours is impossible, but OUR row referencing a
    # user we are about to delete is not — clear it explicitly so step 6 cannot
    # fail on a constraint the audit could not see.
    if comm_id and result.user_ids:
        db.execute(text(
            "UPDATE commercial_organizations SET owner_user_id = NULL "
            "WHERE id = :cid"
        ), {"cid": comm_id})

    # ── 6. Users ─────────────────────────────────────────────────────────────
    # Scoped to this organization. Super-admin accounts carry
    # organization_id = NULL and can therefore never be matched.
    if op_id is not None:
        _delete("users", "organization_id = :oid", {"oid": op_id})

    # ── 7. The tenant rows themselves ────────────────────────────────────────
    if comm_id:
        _delete("commercial_organizations", "id = :cid", {"cid": comm_id})
    if op_id is not None:
        _delete("organizations", "id = :oid", {"oid": op_id})

    db.flush()

    # KAIROS (chatbot_*) is deliberately untouched: those four tables carry no
    # tenant column of any kind, so there is no correct filter to write. Deleting
    # by user email would be a guess, and deleting nothing is the safe failure.
    # Recorded here rather than silently skipped.
    _log.info(
        "Purged tenant %s (%s, op#%s): %d rows across %d tables",
        name, comm_id, op_id, result.total_rows, len(result.deleted_rows),
    )
    return result


# ──────────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────────
def _collect_storage_paths(db: Session, op_id: int, present: Set[str]) -> List[str]:
    """Public URLs of this tenant's stored objects, gathered before deletion.

    Screenshots are written to ``/uploads/org_<id>/…`` so the whole prefix is
    tenant-owned and can be removed wholesale. Leave attachments and the org
    logo share a flat namespace with every other tenant, so those are collected
    as individual paths from the rows that reference them.
    """
    paths: List[str] = []
    if "org_settings" in present:
        for (p,) in db.execute(text(
            "SELECT logo_path FROM org_settings "
            "WHERE organization_id = :oid AND logo_path IS NOT NULL"
        ), {"oid": op_id}).fetchall():
            if p:
                paths.append(str(p))
    if "leave_requests" in present:
        for (p,) in db.execute(text(
            "SELECT attachment_path FROM leave_requests "
            "WHERE organization_id = :oid AND attachment_path IS NOT NULL"
        ), {"oid": op_id}).fetchall():
            if p:
                paths.append(str(p))
    return paths


def cleanup_storage(operational_org_id: Optional[int], paths: Sequence[str]) -> Dict[str, int]:
    """Best-effort removal of a deleted tenant's files. Call AFTER the commit.

    Deliberately post-commit and non-fatal. Running it inside the transaction
    would mean a later rollback leaves the database intact but the customer's
    screenshots already destroyed — the one failure mode worse than an orphaned
    file. Failures are logged and counted, never raised.
    """
    removed = {"screenshot_dirs": 0, "objects": 0, "failed": 0}
    try:
        from app.storage import service as storage
    except Exception:  # pragma: no cover
        return removed

    # Screenshots: one directory per tenant.
    if operational_org_id is not None:
        prefix = f"org_{operational_org_id}"
        try:
            if (storage.backend_name() or "local") == "local":
                d = os.path.join(
                    storage.TRACKER_DATA_DIR, storage.SCREENSHOTS.local_subdir, prefix,
                )
                if os.path.isdir(d):
                    shutil.rmtree(d)
                    removed["screenshot_dirs"] += 1
            else:
                # Object storage: no recursive delete in the shared helper, so
                # the prefix is left for the bucket's lifecycle policy. Logged
                # so it is a known, stated behaviour and not a silent leak.
                _log.warning(
                    "Storage backend %r has no recursive delete — screenshot "
                    "prefix %s/ for deleted org %s was NOT removed and must be "
                    "reclaimed by the bucket lifecycle policy.",
                    storage.backend_name(), prefix, operational_org_id,
                )
        except Exception:
            removed["failed"] += 1
            _log.exception("Screenshot cleanup failed for org %s", operational_org_id)

    # Individually referenced objects (org logo, leave attachments).
    for public_path in paths:
        try:
            bucket = storage.bucket_for_prefix(public_path)
            if bucket is None:
                continue
            filename = public_path[len(bucket.url_prefix):].lstrip("/")
            if not filename:
                continue
            storage.delete_object(bucket, filename)
            removed["objects"] += 1
        except Exception:
            removed["failed"] += 1
            _log.warning("Could not delete stored object %s", public_path, exc_info=True)

    return removed
