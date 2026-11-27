"""
COMPLIANCE EVIDENCE — DPIA, consultation, worker notice (rulings D2, D3, D4).
=============================================================================

Three dedicated stores for three §44.3 gates. What is under test:

1. **Only ``{admin, hr}`` may record any of it**, attributably, and only for
   their own Tenant. One authorization vocabulary, imported rather than
   restated.
2. **Nothing is ever edited.** A correction is a NEW record superseding the
   prior one. Enforced by the database, because a Core UPDATE never reaches the
   ORM (Ruling M / D18-F1). Evidence that could be edited after the fact is not
   evidence; it is a current opinion wearing a timestamp.
3. **"In force" is answered as of a DATE**, so an assessment of a past date
   gets the past answer and a future-dated record satisfies nothing today.
4. **The worker-notice record is Tenant-level and carries no Worker.** Ruling
   D4 forbade Worker-level notice and acknowledgement records, and D7 forbade
   reusing ``commercial_consents``, ``audit_logs`` or ``ConfigVersion``.
5. **Temveris records; it does not assess adequacy.** There is no reviewed,
   approved or verified column anywhere here, because a field for a judgement
   is an invitation to make one.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.compliance_evidence import (
    EcoConsultationAttestation,
    EcoDpiaEvidence,
    EcoWorkerNoticeConfirmation,
    EvidenceStatus,
    UnauthorizedEvidenceActor,
    consultation_in_force,
    dpia_in_force,
    record_consultation_attestation,
    record_dpia_evidence,
    record_worker_notice,
    worker_notice_in_force,
)
from app.compliance_evidence.append_only_ddl import TABLES
from app.compliance_evidence.outcomes import RETENTION_POLICY

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "compliance_evidence"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "f1c6b28d5a73_eco_compliance_evidence_and_jurisdiction.py")

ORG, OTHER_ORG = 8910, 8920
ADMIN, HR, WORKER, OTHER_ADMIN = 8911, 8912, 8913, 8921

JAN = date(2026, 1, 1)
MAR = date(2026, 3, 1)
JUN = date(2026, 6, 1)

#: Each recorder, with the model and the in-force reader that goes with it.
RECORDERS = [
    pytest.param(record_dpia_evidence, EcoDpiaEvidence, dpia_in_force,
                 id="dpia"),
    pytest.param(record_consultation_attestation, EcoConsultationAttestation,
                 consultation_in_force, id="consultation"),
    pytest.param(record_worker_notice, EcoWorkerNoticeConfirmation,
                 worker_notice_in_force, id="worker_notice"),
]

MODELS = (EcoDpiaEvidence, EcoConsultationAttestation,
          EcoWorkerNoticeConfirmation)


class Actor:
    def __init__(self, *, role="admin", user_id=ADMIN, organization_id=ORG):
        self.role = role
        self.user_id = user_id
        self.organization_id = organization_id
        self.label = role


@pytest.fixture()
def db():
    import app.compliance_evidence.models  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng, tables=[
        dbm.Organization.__table__, dbm.User.__table__,
        *(m.__table__ for m in MODELS)])
    with SaSession(eng, expire_on_commit=False) as s:
        for org in (ORG, OTHER_ORG):
            s.add(dbm.Organization(id=org, name=f"Org {org}"))
        s.commit()
        for uid, org in ((ADMIN, ORG), (HR, ORG), (WORKER, ORG),
                         (OTHER_ADMIN, OTHER_ORG)):
            s.add(dbm.User(id=uid, name="U", email=f"{uid}@c.test",
                           organization_id=org, role="employee"))
        s.commit()
        yield s


# ═════════════════════════════════════════════════════════════════════════════
# 1. Authorization — one vocabulary, composed not restated
# ═════════════════════════════════════════════════════════════════════════════
class TestAuthorization:
    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    @pytest.mark.parametrize("role", ["admin", "hr"])
    def test_admin_and_hr_may_record(self, db, recorder, model, reader, role):
        row = recorder(db, organization_id=ORG,
                       actor=Actor(role=role,
                                   user_id=ADMIN if role == "admin" else HR),
                       effective_from=JAN)
        db.commit()
        assert row.actor_role == role
        assert row.status == EvidenceStatus.COMPLETED.value

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    @pytest.mark.parametrize("role", ["employee", "manager", "owner", None])
    def test_every_other_role_is_refused(self, db, recorder, model, reader,
                                         role):
        with pytest.raises(UnauthorizedEvidenceActor):
            recorder(db, organization_id=ORG,
                     actor=Actor(role=role, user_id=WORKER),
                     effective_from=JAN)

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_unattributable_evidence_is_refused(self, db, recorder, model,
                                                reader):
        """Evidence nobody is named on is not evidence of anybody having done
        anything."""
        with pytest.raises(UnauthorizedEvidenceActor):
            recorder(db, organization_id=ORG,
                     actor=Actor(user_id=None), effective_from=JAN)

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_an_actor_may_not_record_for_another_tenant(self, db, recorder,
                                                        model, reader):
        with pytest.raises(UnauthorizedEvidenceActor):
            recorder(db, organization_id=ORG,
                     actor=Actor(user_id=OTHER_ADMIN,
                                 organization_id=OTHER_ORG),
                     effective_from=JAN)

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_a_refused_recording_writes_nothing(self, db, recorder, model,
                                                reader):
        with pytest.raises(UnauthorizedEvidenceActor):
            recorder(db, organization_id=ORG, actor=Actor(role="employee"),
                     effective_from=JAN)
        db.rollback()
        assert db.query(model).count() == 0

    def test_the_package_composes_the_one_authorization_vocabulary(self):
        """``AUTHORIZED_ROLES`` imported, never restated: a second copy is a
        second answer to "who may act", and they drift."""
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        assert any(n.module == "app.eco_threshold_capture.outcomes"
                   and any(a.name == "AUTHORIZED_ROLES" for a in n.names)
                   for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        assert "AUTHORIZED_ROLES" not in assigned

    @pytest.mark.parametrize("model", MODELS)
    def test_the_database_also_constrains_the_role(self, db, model):
        """Defence in depth. The service refuses first; the CHECK refuses a
        row that reached the table another way."""
        checks = [c.sqltext.text for c in model.__table__.constraints
                  if isinstance(c, sa.CheckConstraint)]
        assert any("actor_role IN" in c for c in checks), model.__tablename__


# ═════════════════════════════════════════════════════════════════════════════
# 2. Append-only, enforced by the database
# ═════════════════════════════════════════════════════════════════════════════
class TestAppendOnly:
    @pytest.mark.parametrize("table", TABLES)
    def test_a_core_update_is_refused(self, db, table):
        for recorder, _model, _reader in [p.values for p in RECORDERS]:
            recorder(db, organization_id=ORG, actor=Actor(),
                     effective_from=JAN)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                f"UPDATE {table} SET status = 'NOT_REQUIRED'"))  # noqa: S608
            db.commit()

    @pytest.mark.parametrize("table", TABLES)
    def test_a_core_delete_is_refused(self, db, table):
        for recorder, _model, _reader in [p.values for p in RECORDERS]:
            recorder(db, organization_id=ORG, actor=Actor(),
                     effective_from=JAN)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608
            db.commit()

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_a_correction_supersedes_rather_than_edits(self, db, recorder,
                                                       model, reader):
        first = recorder(db, organization_id=ORG, actor=Actor(),
                         effective_from=JAN)
        db.commit()
        second = recorder(db, organization_id=ORG, actor=Actor(),
                          effective_from=JUN)
        db.commit()
        assert second.supersedes_id == first.id
        assert db.get(model, first.id) is not None
        assert db.query(model).count() == 2

    def test_the_refusal_text_carries_no_apostrophe(self):
        """These strings are embedded in a single-quoted SQLite
        RAISE(ABORT, ...) literal. An apostrophe terminates it and CREATE
        TRIGGER fails at schema-creation time — a broken schema rather than a
        wrong answer."""
        from app.compliance_evidence.append_only_ddl import (
            APPEND_ONLY_DDL_BY_DIALECT,
        )
        for statements in APPEND_ONLY_DDL_BY_DIALECT["sqlite"].values():
            for sql in statements:
                body = sql.split("RAISE(ABORT, '", 1)[1]
                assert body[body.index("')")] == "'"

    def test_postgresql_protection_is_emitted_for_every_table(self):
        """Offline check. No PostgreSQL database is reachable here, so this
        asserts the statements EXIST — not that they were executed."""
        from app.compliance_evidence.append_only_ddl import (
            all_append_only_statements,
        )
        pg = all_append_only_statements("postgresql")
        for table in TABLES:
            assert any(f"CREATE TRIGGER {table}_no_update" in s for s in pg)
            assert any(f"CREATE TRIGGER {table}_no_delete" in s for s in pg)

    def test_an_unknown_dialect_gets_no_protection_rather_than_partial(self):
        from app.compliance_evidence.append_only_ddl import (
            all_append_only_statements,
        )
        assert all_append_only_statements("mysql") == ()


# ═════════════════════════════════════════════════════════════════════════════
# 3. "In force" is a question about a date
# ═════════════════════════════════════════════════════════════════════════════
class TestInForce:
    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_nothing_recorded_is_nothing_in_force(self, db, recorder, model,
                                                  reader):
        assert reader(db, organization_id=ORG, as_of=JUN) is None

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_a_future_dated_record_is_not_in_force_today(self, db, recorder,
                                                         model, reader):
        """A confirmation dated next year does not satisfy a gate today."""
        recorder(db, organization_id=ORG, actor=Actor(), effective_from=JUN)
        db.commit()
        assert reader(db, organization_id=ORG, as_of=MAR) is None
        assert reader(db, organization_id=ORG, as_of=JUN) is not None

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_a_past_date_gets_the_past_answer(self, db, recorder, model,
                                              reader):
        first = recorder(db, organization_id=ORG, actor=Actor(),
                         effective_from=JAN)
        db.commit()
        second = recorder(db, organization_id=ORG, actor=Actor(),
                          effective_from=JUN)
        db.commit()
        assert reader(db, organization_id=ORG, as_of=MAR).id == first.id
        assert reader(db, organization_id=ORG, as_of=JUN).id == second.id

    @pytest.mark.parametrize("recorder,model,reader", RECORDERS)
    def test_one_tenants_evidence_is_not_anothers(self, db, recorder, model,
                                                  reader):
        recorder(db, organization_id=ORG, actor=Actor(), effective_from=JAN)
        db.commit()
        assert reader(db, organization_id=OTHER_ORG, as_of=JUN) is None


# ═════════════════════════════════════════════════════════════════════════════
# 4. The shape each record has, and the shape it must NOT have
# ═════════════════════════════════════════════════════════════════════════════
class TestShape:
    def test_the_dpia_carries_both_applicability_answers(self, db):
        """Owner ruling C: B. D1 ruled both scopes apply and they are not the
        same set: §40.2's substantive requirement is EU, §44.3's gate is
        EU/UK. One enum would make "EU-substantive, UK-gate-only" unsayable."""
        row = record_dpia_evidence(db, organization_id=ORG, actor=Actor(),
                                   effective_from=JAN,
                                   eu_substantive_applies=False,
                                   eu_uk_gate_applies=True,
                                   jurisdiction_scope=["gb"])
        db.commit()
        assert row.eu_substantive_applies is False
        assert row.eu_uk_gate_applies is True
        assert row.jurisdiction_scope == "GB"

    def test_the_worker_notice_carries_no_worker(self, db):
        """Ruling D4: a TENANT-level confirmation. Worker-level notice and
        acknowledgement records are forbidden, and the only person named here
        is the CONFIRMING administrator."""
        columns = {c.name for c in EcoWorkerNoticeConfirmation.__table__.columns}
        assert columns & {"user_id", "worker_id", "employee_id",
                          "acknowledged_at", "acknowledged_by",
                          "acknowledgement_id"} == set()
        assert "actor_user_id" in columns

    @pytest.mark.parametrize("model", MODELS)
    def test_nothing_records_an_adequacy_judgement(self, model):
        """§44.3: Temveris records the attestation and does not assess its
        adequacy. A field for a judgement is an invitation to make one."""
        columns = {c.name for c in model.__table__.columns}
        assert columns & {"reviewed", "reviewed_by", "approved", "approved_by",
                          "verified", "is_adequate", "adequacy",
                          "assessment_outcome"} == set(), model.__tablename__

    @pytest.mark.parametrize("model", MODELS)
    def test_the_tenant_cannot_be_orphaned(self, model):
        """Ruling D7 rejected ``audit_logs`` partly because its organization FK
        is ``SET NULL``: evidence that can lose its Tenant is evidence of
        nothing. Every FK here is RESTRICT."""
        for fk in model.__table__.foreign_keys:
            assert fk.ondelete == "RESTRICT", (model.__tablename__, fk.column)

    def test_the_retention_policy_is_named_rather_than_assumed(self):
        """§37's four retention classes cover none of this. Ruling A: C —
        Tenant lifetime unless superseded, recorded as a constant so the answer
        is stated rather than inferred from the absence of a deleter."""
        assert RETENTION_POLICY == "TENANT_LIFETIME_UNLESS_SUPERSEDED"

    def test_no_deleter_exists(self):
        """The retention policy above is implemented by there being nothing
        that deletes. Asserted by AST, not by reading the docstring."""
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        functions = {n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not any("delete" in n or "purge" in n or "expire" in n
                       for n in functions), functions


# ═════════════════════════════════════════════════════════════════════════════
# 5. Ruling D7: these are DEDICATED stores
# ═════════════════════════════════════════════════════════════════════════════
class TestDedicatedStores:
    def test_it_reuses_none_of_the_three_rejected_tables(self):
        """``commercial_consents`` (wrong subject and lifecycle),
        ``audit_logs`` (SET NULL on both organization and actor) and
        ``ConfigVersion`` (configuration history, not compliance)."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute)}
            imported = {n.module for n in ast.walk(tree)
                        if isinstance(n, ast.ImportFrom) and n.module}
            assert not (names & {"CommercialConsent", "AuditLog",
                                 "ConfigVersion"}), path.name
            assert not any("commercial" in m or "audit" in m
                           for m in imported), path.name

    def test_each_gate_has_its_own_table(self):
        assert len({m.__tablename__ for m in MODELS}) == 3
        assert set(TABLES) == {m.__tablename__ for m in MODELS}


# ═════════════════════════════════════════════════════════════════════════════
# 6. Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_no_route_scheduler_or_notification(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            assert not any(m.startswith(("fastapi", "apscheduler", "celery"))
                           or "email_service" in m or "notification" in m
                           for m in imported), path.name


# ═════════════════════════════════════════════════════════════════════════════
# 7. The migration
# ═════════════════════════════════════════════════════════════════════════════
def _migrated_engine(suffix):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    eng = sa.create_engine("sqlite:///:memory:", future=True)
    chain = ("b4e8c2a95d17_policy_version.py",
             "a3c8e5f10b47_policy_surplus_caps_and_horizon.py",
             MIGRATION.name)
    for name in chain:
        path = BACKEND / "alembic" / "versions" / name
        spec = importlib.util.spec_from_file_location(name[:12] + suffix, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
    return eng


class TestMigration:
    def test_the_executed_schema_matches_the_orm_model(self):
        """Run it rather than read it: the trigger SQL and the CHECK
        constraints are strings a static check would not catch if malformed."""
        actual = sa.inspect(_migrated_engine("_a"))
        for model in MODELS:
            name = model.__tablename__
            assert name in actual.get_table_names(), name
            assert sorted(c["name"] for c in actual.get_columns(name)) == \
                sorted(c.name for c in model.__table__.columns), name
            assert {c["name"]: c["nullable"]
                    for c in actual.get_columns(name)} == \
                {c.name: c.nullable for c in model.__table__.columns}, name

    def test_the_migrated_schema_is_append_only(self):
        """The triggers must exist in a MIGRATED database, not only in a
        create_all one — otherwise every immutability test above passes
        vacuously against a shape production does not have."""
        eng = _migrated_engine("_b")
        with eng.begin() as connection:
            triggers = {r[0] for r in connection.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"))}
        for table in TABLES:
            assert f"{table}_no_update" in triggers, table
            assert f"{table}_no_delete" in triggers, table

    def test_the_migrated_triggers_actually_refuse(self, db):
        """Existing is not refusing."""
        eng = _migrated_engine("_c")
        with eng.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO eco_dpia_evidence (organization_id, status,"
                " effective_from, actor_user_id, actor_role, actor_label,"
                " jurisdiction_scope, recorded_at_utc)"
                " VALUES (1, 'COMPLETED', '2026-01-01', 7, 'admin', 'x', 'DE',"
                " '2026-01-01 00:00:00')"))
        for statement in ("UPDATE eco_dpia_evidence SET status='NOT_REQUIRED'",
                          "DELETE FROM eco_dpia_evidence"):
            with pytest.raises(IntegrityError):
                with eng.begin() as connection:
                    connection.execute(sa.text(statement))

    def test_it_records_no_evidence_of_its_own(self):
        """Seeding evidence would be fabricating the very thing §44.3 exists
        to require.

        The revision DOES seed the governed country mapping (ruling B), so
        this is asserted against the three evidence tables by name rather than
        against the absence of any INSERT at all.
        """
        source = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {getattr(n.func, "attr", None) for n in ast.walk(tree)
                  if isinstance(n, ast.Call)}
        assert "bulk_insert" not in called

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.upper()
                if "INSERT INTO" not in text:
                    continue
                for table in ("ECO_DPIA", "ECO_CONSULTATION",
                              "ECO_WORKER_NOTICE", "DPIA_TABLE",
                              "CONSULTATION_TABLE", "WORKER_NOTICE_TABLE"):
                    assert table not in text, table

    def test_no_evidence_exists_in_a_freshly_migrated_database(self):
        """The strongest form of the same claim: run it and look."""
        eng = _migrated_engine("_e")
        with eng.begin() as connection:
            for model in MODELS:
                held = connection.execute(sa.text(
                    f"SELECT count(*) FROM {model.__tablename__}")).scalar()
                assert held == 0, model.__tablename__

    def test_the_downgrade_refuses_to_discard_recorded_evidence(self):
        """Dropping a table holding compliance evidence is a decision, not a
        schema step."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = _migrated_engine("_d")
        with eng.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO eco_worker_notice_confirmation (organization_id,"
                " status, effective_from, actor_user_id, actor_role,"
                " actor_label, recorded_at_utc, confirmed_at_utc)"
                " VALUES (1, 'COMPLETED', '2026-01-01', 7, 'admin', 'x',"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))

        spec = importlib.util.spec_from_file_location("mig_down", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(RuntimeError, match="discard"):
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.downgrade()
