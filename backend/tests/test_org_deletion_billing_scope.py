"""Organization deletion must scope billing tables by the INTEGER org id.

THE BUG THIS DEFENDS AGAINST
----------------------------
The eight ``billing_*`` tables were listed in ``COMMERCIAL_TABLES``, which
deletes with ``organization_id = :cid`` where ``cid`` is the commercial
organization id — a string like ``org_f42364f64b61cc91f006653fdccd94f8``.

Migration ``e2c9a4d81f63`` converted those columns from ``VARCHAR(80)`` to
``INTEGER FK organizations.id``, so they are keyed on the OPERATIONAL integer
id. The manifest was never updated. On PostgreSQL every organization deletion
therefore died on the first billing table::

    (psycopg2.errors.InvalidTextRepresentation) invalid input syntax for
    type integer: "org_f42364f64b61cc91f006653fdccd94f8"
    [SQL: DELETE FROM billing_audit_events WHERE organization_id = %(cid)s]

Because the purge is one transaction, that rolled the whole deletion back and
no organization could ever be deleted.

WHY THESE TESTS CATCH IT ON SQLITE TOO
--------------------------------------
SQLite has dynamic typing, so it will NOT raise the Postgres cast error — it
silently compares an integer column against a string and matches nothing. That
is exactly what makes ``test_purge_removes_every_billing_row`` a valid
regression test on either backend: under the old manifest the billing rows
SURVIVE the purge, so the assertion fails. ``test_billing_deletes_never_bind_a
_string_org_id`` closes the remaining gap by inspecting the parameters actually
handed to the driver, which is the precise condition Postgres rejected.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database import database as _db
from app.commercial import models as cm
from app.org import deletion_service as ds

#: The tables migration e2c9a4d81f63 moved to an INTEGER FK.
BILLING_TABLES = (
    "billing_audit_events",
    "billing_enterprise_quotes",
    "billing_downgrade_requests",
    "billing_tax_profiles",
    "billing_contacts",
    "billing_payment_methods",
    "billing_invoices",
    "billing_subscriptions",
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bill_engine():
    """In-memory schema that DOES include the billing tables.

    ``tests/test_organization_lifecycle.py`` builds a curated table list that
    omits every ``billing_*`` table, and the manifest skips tables absent from
    the connected database — which is why this defect survived a green suite.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = eng
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    for tbl in (
        models.Organization, models.User, models.OrgSettings, models.AppSettings,
        models.Team, models.AuditLog,
        # The subject of these tests.
        models.BillingPlan,
        models.BillingSubscription, models.BillingInvoice, models.BillingContact,
        models.BillingTaxProfile, models.BillingPaymentMethod,
        models.BillingAuditEvent, models.BillingDowngradeRequest,
        models.BillingEnterpriseQuote,
        cm.CommercialOrganization, cm.CommercialSubscription,
    ):
        tbl.__table__.create(eng, checkfirst=True)

    try:
        yield eng
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def db(bill_engine):
    s = _db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


_seq = iter(range(88000, 99000))


def make_tenant(db, name):
    """One operational org + its commercial row + a full set of billing rows."""
    n = next(_seq)
    org = models.Organization(id=n, name=name)
    db.add(org)
    db.flush()

    comm = cm.CommercialOrganization(
        id=f"org_{name.lower().replace(' ', '')}{n}",
        name=name,
        operational_org_id=org.id,
    )
    db.add(comm)
    db.flush()

    # One row in each of the eight tables, all keyed on the INTEGER org id.
    db.add_all([
        models.BillingAuditEvent(organization_id=org.id, action_key="plan_changed"),
        models.BillingEnterpriseQuote(organization_id=org.id,
                                      company_name=name, work_email=f"{n}@x.test"),
        models.BillingDowngradeRequest(organization_id=org.id, reason="too costly"),
        models.BillingTaxProfile(organization_id=org.id),
        models.BillingContact(organization_id=org.id, name=name, email=f"{n}@x.test"),
        models.BillingPaymentMethod(organization_id=org.id),
        models.BillingSubscription(organization_id=org.id),
        models.BillingInvoice(organization_id=org.id, invoice_number=f"INV-{n}"),
    ])
    # Committed, not just flushed: the purge must act on persisted data, and the
    # rollback tests need a baseline that a rollback can actually restore.
    # Every tenant gets a fresh id from `_seq`, so committed rows from one test
    # can never be matched by another test's org-scoped assertions.
    db.commit()
    return org, comm


def _billing_counts(db, op_id):
    return {
        t: db.execute(
            text(f"SELECT count(*) FROM {t} WHERE organization_id = :v"),
            {"v": op_id},
        ).scalar()
        for t in BILLING_TABLES
    }


# ─── The manifest binds billing to the integer scope ─────────────────────────

class TestManifestScoping:

    def test_no_billing_table_is_scoped_by_the_commercial_string_id(self):
        """The exact misconfiguration that broke every deletion."""
        leaked = [t for t in BILLING_TABLES if t in ds.COMMERCIAL_TABLES]
        assert leaked == [], (
            f"{leaked} are scoped by the commercial STRING id but their "
            "organization_id is an INTEGER FK — this is the InvalidText"
            "Representation bug"
        )

    def test_every_billing_table_is_in_the_operational_path(self):
        missing = [t for t in BILLING_TABLES if t not in ds.OPERATIONAL_TABLES]
        assert missing == [], f"{missing} would never be deleted"

    def test_invoices_are_deleted_before_subscriptions(self):
        """`billing_invoices.subscription_id` FKs to `billing_subscriptions`."""
        order = list(ds.OPERATIONAL_TABLES)
        assert order.index("billing_invoices") < order.index("billing_subscriptions")

    def test_billing_plans_is_never_deleted(self):
        """Shared platform price catalogue — deleting it would break every tenant."""
        assert "billing_plans" not in ds.OPERATIONAL_TABLES
        assert "billing_plans" not in ds.COMMERCIAL_TABLES

    def test_manifest_still_classifies_all_eight(self):
        """Moving lists must not drop a table out of the coverage contract."""
        classified = ds.classified_tables()
        assert all(t in classified for t in BILLING_TABLES)


# ─── Behaviour: the rows actually go, and only the right ones ────────────────

class TestPurgeRemovesBillingRows:

    def test_purge_removes_every_billing_row(self, db):
        """Fails under the old manifest: the string filter matched nothing."""
        org, comm = make_tenant(db, "Doomed Co")
        op_id = org.id

        before = _billing_counts(db, op_id)
        assert all(v == 1 for v in before.values()), before

        ds.purge_organization(db, comm)
        db.flush()

        after = _billing_counts(db, op_id)
        survivors = {t: n for t, n in after.items() if n}
        assert survivors == {}, f"billing rows survived the purge: {survivors}"

    def test_other_tenants_billing_rows_are_untouched(self, db):
        """Multi-tenancy: the purge must never reach across organizations."""
        doomed, doomed_comm = make_tenant(db, "Doomed Co")
        keeper, _ = make_tenant(db, "Keeper Co")
        # Read the ids up front: the purge deletes the `organizations` row with
        # raw SQL, so touching the ORM instance afterwards raises
        # ObjectDeletedError (the router expunges it for the same reason).
        doomed_id, keeper_id = doomed.id, keeper.id

        ds.purge_organization(db, doomed_comm)
        db.flush()

        assert _billing_counts(db, doomed_id) == dict.fromkeys(BILLING_TABLES, 0)
        assert _billing_counts(db, keeper_id) == dict.fromkeys(BILLING_TABLES, 1), (
            "another organization's billing rows were deleted"
        )

    def test_billing_rows_are_reported_in_the_result(self, db):
        """The deleted-row counts must be visible, not silently zero."""
        org, comm = make_tenant(db, "Doomed Co")
        result = ds.purge_organization(db, comm)
        db.flush()

        touched = {t: n for t, n in result.deleted_rows.items()
                   if t in BILLING_TABLES}
        assert touched, "no billing table appeared in the purge result"
        assert sum(touched.values()) == len(BILLING_TABLES), touched


# ─── The Postgres cast error specifically ────────────────────────────────────

class TestNoStringBoundToAnIntegerColumn:

    def test_billing_deletes_never_bind_a_string_org_id(self, db, bill_engine):
        """The precise condition Postgres rejected.

        SQLite would happily accept the string, so assert on the parameters
        handed to the driver rather than waiting for a backend to complain.
        """
        seen: list[tuple[str, object]] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if "billing_" in statement and "DELETE" in statement.upper():
                seen.append((statement, parameters))

        event.listen(bill_engine, "before_cursor_execute", record)
        try:
            org, comm = make_tenant(db, "Doomed Co")
            ds.purge_organization(db, comm)
            db.flush()
        finally:
            event.remove(bill_engine, "before_cursor_execute", record)

        assert seen, "no billing DELETE was issued at all"

        for statement, parameters in seen:
            values = (list(parameters.values())
                      if isinstance(parameters, dict) else list(parameters or ()))
            for v in values:
                assert not isinstance(v, str), (
                    "a string org id was bound to an INTEGER column — this is "
                    f"the InvalidTextRepresentation crash.\n  SQL: {statement}\n"
                    f"  params: {parameters}"
                )
                assert isinstance(v, int), (statement, parameters)

    def test_bound_value_is_the_operational_id_not_the_commercial_id(self, db, bill_engine):
        seen: list[object] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if "billing_audit_events" in statement and "DELETE" in statement.upper():
                seen.append(parameters)

        event.listen(bill_engine, "before_cursor_execute", record)
        try:
            org, comm = make_tenant(db, "Doomed Co")
            op_id, comm_id = org.id, comm.id
            ds.purge_organization(db, comm)
            db.flush()
        finally:
            event.remove(bill_engine, "before_cursor_execute", record)

        flat = [v for p in seen
                for v in (p.values() if isinstance(p, dict) else (p or ()))]
        assert op_id in flat, f"operational id {op_id} was never bound: {seen}"
        assert comm_id not in flat, f"commercial id {comm_id} was bound: {seen}"


# ─── Transactionality is preserved ───────────────────────────────────────────

class TestDeletionStaysAtomic:

    def test_a_failure_mid_purge_leaves_billing_rows_intact(self, db, monkeypatch):
        """purge_organization is flush-only; the caller owns the transaction.

        A failure after the billing deletes must still leave the tenant whole
        once the caller rolls back — the guarantee the UI reports as
        "every change was rolled back — the organization is intact".
        """
        org, comm = make_tenant(db, "Doomed Co")
        op_id = org.id

        real_existing = ds._existing_tables

        def boom(*a, **k):
            raise RuntimeError("simulated failure mid-purge")

        # Fail late: after the billing sweep has already run.
        monkeypatch.setattr(ds, "_collect_storage_paths", boom, raising=False)

        with pytest.raises(RuntimeError):
            ds.purge_organization(db, comm)
        db.rollback()

        assert _billing_counts(db, op_id) == dict.fromkeys(BILLING_TABLES, 1), (
            "rollback did not restore the tenant's billing rows"
        )
        assert db.get(models.Organization, op_id) is not None
        assert real_existing is ds._existing_tables

    def test_purge_does_not_commit(self, db):
        """Flush-only contract: a rollback right after must undo everything."""
        org, comm = make_tenant(db, "Doomed Co")
        op_id = org.id

        ds.purge_organization(db, comm)
        db.rollback()

        assert _billing_counts(db, op_id) == dict.fromkeys(BILLING_TABLES, 1)
        assert db.get(models.Organization, op_id) is not None
