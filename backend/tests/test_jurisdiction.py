"""
JURISDICTION — the governed effective-dated mapping (owner ruling B).
=====================================================================

Four properties, and the fourth is the one the whole design exists for:

1. **What a country legally IS is platform-governed.** A Tenant may correct
   WHICH jurisdictions apply to it; it may never say what a jurisdiction is.
2. **Everything is effective-dated.** An assessment of a past date resolves the
   mapping and the assignment that were in force THEN, never today's.
3. **Nothing is ever edited.** Corrections supersede. Enforced by the database,
   because the ORM never sees a Core UPDATE (Ruling M / D18-F1).
4. **An explicitly empty jurisdiction set is not the same as never having been
   assigned one** (ruling B9: B). One is a statement; the other is silence, and
   a compliance gate that treated them alike would waive itself on silence.
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

from app.jurisdiction import (
    ClassificationNotGoverned,
    InvalidCountryCode,
    JurisdictionClassification,
    SPEC_NAMED_CONSULTATION_COUNTRIES,
    TenantJurisdictionAssignment,
    TenantJurisdictionAssignmentCountry,
    UnauthorizedAssignment,
    assign_platform,
    classification_as_of,
    correct_assignment,
    record_classification,
    resolve,
)
from app.jurisdiction.append_only_ddl import TABLES

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "jurisdiction"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "f1c6b28d5a73_eco_compliance_evidence_and_jurisdiction.py")

ORG, OTHER_ORG = 8810, 8820
ADMIN, HR, WORKER, OTHER_ADMIN = 8811, 8812, 8813, 8821

JAN = date(2026, 1, 1)
MAR = date(2026, 3, 1)
JUN = date(2026, 6, 1)


class Actor:
    def __init__(self, *, role, user_id=ADMIN, organization_id=ORG):
        self.role = role
        self.user_id = user_id
        self.organization_id = organization_id
        self.label = role


@pytest.fixture()
def db():
    import app.jurisdiction.models  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng, tables=[
        dbm.Organization.__table__, dbm.User.__table__,
        JurisdictionClassification.__table__,
        TenantJurisdictionAssignment.__table__,
        TenantJurisdictionAssignmentCountry.__table__,
    ])
    with SaSession(eng, expire_on_commit=False) as s:
        for org in (ORG, OTHER_ORG):
            s.add(dbm.Organization(id=org, name=f"Org {org}"))
        s.commit()
        for uid, org in ((ADMIN, ORG), (HR, ORG), (WORKER, ORG),
                         (OTHER_ADMIN, OTHER_ORG)):
            s.add(dbm.User(id=uid, name="U", email=f"{uid}@j.test",
                           organization_id=org, role="employee"))
        s.commit()
        s.info["engine"] = eng
        yield s


def _germany(db, effective_from=JAN):
    return record_classification(db, country_code="DE",
                                 effective_from=effective_from, is_eu=True,
                                 requires_consultation=True)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The classification is platform-governed
# ═════════════════════════════════════════════════════════════════════════════
class TestClassificationIsPlatformGoverned:
    def test_a_tenant_actor_is_refused(self, db):
        """Ruling B6/B8. A Tenant correcting WHICH jurisdictions apply to it is
        authorized; a Tenant declaring what a jurisdiction IS is not."""
        with pytest.raises(ClassificationNotGoverned):
            record_classification(db, country_code="DE", effective_from=JAN,
                                  is_eu=True, actor=Actor(role="admin"))

    def test_the_refusal_happens_before_anything_is_written(self, db):
        with pytest.raises(ClassificationNotGoverned):
            record_classification(db, country_code="DE", effective_from=JAN,
                                  actor=Actor(role="admin"))
        db.rollback()
        assert db.query(JurisdictionClassification).count() == 0

    @pytest.mark.parametrize("code", ["DEU", "D", "", "  ", "D1", "12"])
    def test_a_non_iso_alpha2_code_is_refused(self, db, code):
        with pytest.raises(InvalidCountryCode):
            record_classification(db, country_code=code, effective_from=JAN)

    def test_case_is_normalised_rather_than_refused(self, db):
        """A caller typing "de" means Germany. What must not vary is the
        STORED value: the CHECK constraint requires canonical uppercase, so
        normalising on the way in is what keeps one country one row."""
        row = record_classification(db, country_code="de", effective_from=JAN,
                                    is_eu=True)
        db.commit()
        assert row.country_code == "DE"
        assert classification_as_of(db, country_code="DE",
                                    as_of=JUN).id == row.id

    def test_the_spec_named_consultation_countries_are_the_four(self):
        """§40.4 names Germany, the Netherlands, Austria and France. The
        constant is the spec's list, not an invented one, and nothing here
        expands it."""
        assert set(SPEC_NAMED_CONSULTATION_COUNTRIES) == {"AT", "DE", "FR",
                                                          "NL"}


# ═════════════════════════════════════════════════════════════════════════════
# 2. Effective dating and supersession
# ═════════════════════════════════════════════════════════════════════════════
class TestEffectiveDating:
    def test_a_classification_before_its_effective_date_is_not_in_force(self, db):
        _germany(db, effective_from=JUN)
        db.commit()
        assert classification_as_of(db, country_code="DE", as_of=MAR) is None
        assert classification_as_of(db, country_code="DE", as_of=JUN) is not None

    def test_a_later_classification_supersedes_the_earlier(self, db):
        first = _germany(db, effective_from=JAN)
        db.commit()
        second = record_classification(db, country_code="DE",
                                       effective_from=JUN, is_eu=True,
                                       requires_consultation=False)
        db.commit()
        assert second.supersedes_id == first.id
        # And the earlier one is still readable for a past date.
        assert classification_as_of(
            db, country_code="DE", as_of=MAR).requires_consultation is True
        assert classification_as_of(
            db, country_code="DE", as_of=JUN).requires_consultation is False

    def test_two_classifications_cannot_share_an_effective_date(self, db):
        _germany(db, effective_from=JAN)
        db.commit()
        with pytest.raises(IntegrityError):
            record_classification(db, country_code="DE", effective_from=JAN,
                                  is_eu=False)
            db.commit()

    def test_a_past_date_resolves_the_assignment_in_force_then(self, db):
        """Ruling B7. Not today's answer applied backwards."""
        _germany(db)
        record_classification(db, country_code="GB", effective_from=JAN,
                              is_uk=True)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE"],
                        effective_from=JAN)
        db.commit()
        correct_assignment(db, organization_id=ORG, country_codes=["GB"],
                           effective_from=JUN, actor=Actor(role="admin"))
        db.commit()

        march = resolve(db, organization_id=ORG, as_of=MAR)
        june = resolve(db, organization_id=ORG, as_of=JUN)
        assert march.countries == ("DE",)
        assert (march.is_eu, march.is_uk) == (True, False)
        assert june.countries == ("GB",)
        assert (june.is_eu, june.is_uk) == (False, True)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Who may assign (ruling B8: C)
# ═════════════════════════════════════════════════════════════════════════════
class TestAssignmentAuthority:
    def test_the_platform_assigns_without_a_user(self, db):
        row = assign_platform(db, organization_id=ORG, country_codes=["DE"],
                              effective_from=JAN)
        db.commit()
        assert row.source == "PLATFORM"
        assert row.actor_user_id is None

    @pytest.mark.parametrize("role", ["admin", "hr"])
    def test_admin_and_hr_may_correct(self, db, role):
        assign_platform(db, organization_id=ORG, country_codes=["DE"],
                        effective_from=JAN)
        db.commit()
        row = correct_assignment(
            db, organization_id=ORG, country_codes=["FR"], effective_from=JUN,
            actor=Actor(role=role, user_id=ADMIN if role == "admin" else HR))
        db.commit()
        assert row.source == "TENANT"
        assert row.actor_role == role

    @pytest.mark.parametrize("role", ["employee", "manager", "owner", None])
    def test_every_other_role_is_refused(self, db, role):
        with pytest.raises(UnauthorizedAssignment):
            correct_assignment(db, organization_id=ORG, country_codes=["DE"],
                               effective_from=JAN,
                               actor=Actor(role=role, user_id=WORKER))

    def test_an_unattributable_correction_is_refused(self, db):
        with pytest.raises(UnauthorizedAssignment):
            correct_assignment(db, organization_id=ORG, country_codes=["DE"],
                               effective_from=JAN,
                               actor=Actor(role="admin", user_id=None))

    def test_an_actor_may_not_correct_another_tenant(self, db):
        with pytest.raises(UnauthorizedAssignment):
            correct_assignment(
                db, organization_id=ORG, country_codes=["DE"],
                effective_from=JAN,
                actor=Actor(role="admin", user_id=OTHER_ADMIN,
                            organization_id=OTHER_ORG))

    def test_a_correction_supersedes_rather_than_replaces(self, db):
        first = assign_platform(db, organization_id=ORG,
                                country_codes=["DE"], effective_from=JAN)
        db.commit()
        second = correct_assignment(db, organization_id=ORG,
                                    country_codes=["FR"], effective_from=JUN,
                                    actor=Actor(role="admin"))
        db.commit()
        assert second.supersedes_id == first.id
        assert db.get(TenantJurisdictionAssignment, first.id) is not None


# ═════════════════════════════════════════════════════════════════════════════
# 4. Ruling B9: B — the distinction the representation exists for
# ═════════════════════════════════════════════════════════════════════════════
class TestDeclaredEmptyIsNotUnassigned:
    def test_never_assigned_reports_is_assigned_false(self, db):
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.is_assigned is False
        assert r.countries == ()
        assert r.is_declared_empty is False

    def test_an_explicitly_empty_set_reports_is_assigned_true(self, db):
        assign_platform(db, organization_id=ORG, country_codes=[],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.is_assigned is True
        assert r.countries == ()
        assert r.is_declared_empty is True

    def test_the_two_are_distinguishable_from_the_resolution_alone(self, db):
        """A caller must not have to query the tables itself to tell them
        apart — which is what a nullable column with a sentinel would have
        forced."""
        never = resolve(db, organization_id=ORG, as_of=JUN)
        assign_platform(db, organization_id=OTHER_ORG, country_codes=[],
                        effective_from=JAN)
        db.commit()
        empty = resolve(db, organization_id=OTHER_ORG, as_of=JUN)
        assert (never.countries, empty.countries) == ((), ())
        assert never.is_assigned != empty.is_assigned

    def test_an_empty_set_derives_no_obligation(self, db):
        assign_platform(db, organization_id=ORG, country_codes=[],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert (r.is_eu, r.is_uk, r.requires_consultation) == (False, False,
                                                               False)
        assert r.dpia_gate_applies is False


# ═════════════════════════════════════════════════════════════════════════════
# 5. The derived flags (ruling D1: C — two scopes, not one)
# ═════════════════════════════════════════════════════════════════════════════
class TestDerivedFlags:
    def test_the_dpia_gate_is_eu_or_uk_and_the_substantive_rule_is_eu_only(self, db):
        """§40.2's requirement is EU. §44.3's gate is EU/UK. A UK-only Tenant
        is inside the second and outside the first, and one flag could not say
        so."""
        record_classification(db, country_code="GB", effective_from=JAN,
                              is_uk=True)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["GB"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.dpia_gate_applies is True
        assert r.dpia_substantive_applies is False

    def test_flags_are_the_union_across_assigned_countries(self, db):
        _germany(db)
        record_classification(db, country_code="GB", effective_from=JAN,
                              is_uk=True)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE", "GB"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert (r.is_eu, r.is_uk, r.requires_consultation) == (True, True,
                                                               True)

    def test_an_unclassified_country_contributes_nothing_and_is_not_denied(self, db):
        """An unclassified country is an unanswered question. Treating it as
        "not EU" would be inventing the legal classification the platform is
        supposed to govern."""
        assign_platform(db, organization_id=ORG, country_codes=["ZZ"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.countries == ("ZZ",)
        assert (r.is_eu, r.is_uk, r.requires_consultation) == (False, False,
                                                              False)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Append-only, enforced by the database
# ═════════════════════════════════════════════════════════════════════════════
class TestAppendOnly:
    @pytest.mark.parametrize("table", TABLES)
    def test_a_core_update_is_refused(self, db, table):
        """The ORM never sees a Core UPDATE, so only the database can refuse
        one (Ruling M / D18-F1)."""
        _germany(db)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE"],
                        effective_from=JAN)
        db.commit()
        column = {"jurisdiction_classification": "recorded_by",
                  "tenant_jurisdiction_assignment": "actor_label",
                  "tenant_jurisdiction_assignment_country": "country_code"}[table]
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                f"UPDATE {table} SET {column} = 'X'"))  # noqa: S608
            db.commit()

    @pytest.mark.parametrize("table", TABLES)
    def test_a_core_delete_is_refused(self, db, table):
        _germany(db)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE"],
                        effective_from=JAN)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608
            db.commit()

    def test_the_refusal_text_carries_no_apostrophe(self):
        """These strings are embedded in a single-quoted SQLite
        RAISE(ABORT, ...) literal. An apostrophe terminates it and the CREATE
        TRIGGER fails at schema-creation time — a broken schema rather than a
        wrong answer, which is why this is asserted rather than trusted."""
        from app.jurisdiction.append_only_ddl import (
            APPEND_ONLY_DDL_BY_DIALECT,
        )
        for statement in APPEND_ONLY_DDL_BY_DIALECT["sqlite"].values():
            for sql in statement:
                body = sql.split("RAISE(ABORT, '", 1)[1]
                assert "'" == body[body.index("')"):][0]

    def test_postgresql_protection_is_emitted_for_every_table(self):
        """No PostgreSQL database is reachable here, so this is an offline
        check that the statements EXIST for every table — not a claim that
        they were executed."""
        from app.jurisdiction.append_only_ddl import all_append_only_statements

        pg = all_append_only_statements("postgresql")
        for table in TABLES:
            assert any(f"CREATE TRIGGER {table}_no_update" in s for s in pg)
            assert any(f"CREATE TRIGGER {table}_no_delete" in s for s in pg)

    def test_an_unknown_dialect_gets_no_protection_rather_than_partial(self):
        from app.jurisdiction.append_only_ddl import all_append_only_statements

        assert all_append_only_statements("mysql") == ()


# ═════════════════════════════════════════════════════════════════════════════
# 7. Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_no_route_scheduler_or_ui(self):
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            assert not any(m.startswith(("fastapi", "apscheduler", "celery"))
                           for m in imported), path.name

    def test_it_does_not_read_primary_country(self):
        """Explicitly NOT authorized: ``primary_country`` is not ECO authority.
        Asserted by AST attribute scan, so a docstring naming it in order to
        forbid it does not trip the guard."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)}
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            assert "primary_country" not in (attrs | names), path.name

    def test_the_package_composes_the_one_authorization_vocabulary(self):
        """``AUTHORIZED_ROLES`` is imported, never restated. A second copy is a
        second answer to "who may act", and they drift."""
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert any(n.module == "app.eco_threshold_capture.outcomes"
                   and any(a.name == "AUTHORIZED_ROLES" for a in n.names)
                   for n in imports)
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        assert "AUTHORIZED_ROLES" not in assigned


# ═════════════════════════════════════════════════════════════════════════════
# 8. The migration
# ═════════════════════════════════════════════════════════════════════════════
class TestMigration:
    def test_the_executed_schema_matches_the_orm_model(self):
        """Run it rather than read it: the trigger SQL and the CHECK
        constraints are strings a static check would not catch if malformed."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("mig_juris", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        # The creating revision for policy_version, which this one alters.
        for earlier in ("b4e8c2a95d17_policy_version.py",
                        "a3c8e5f10b47_policy_surplus_caps_and_horizon.py"):
            s2 = importlib.util.spec_from_file_location(
                earlier[:12], BACKEND / "alembic" / "versions" / earlier)
            m2 = importlib.util.module_from_spec(s2)
            s2.loader.exec_module(m2)
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    m2.upgrade()
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()

        actual = sa.inspect(eng)
        for model in (JurisdictionClassification, TenantJurisdictionAssignment,
                      TenantJurisdictionAssignmentCountry):
            name = model.__tablename__
            assert name in actual.get_table_names(), name
            assert sorted(c["name"] for c in actual.get_columns(name)) == \
                sorted(c.name for c in model.__table__.columns), name

    def test_the_migrated_schema_is_append_only(self):
        """The triggers must exist in a MIGRATED database, not only in a
        create_all one — otherwise every immutability test above passes
        vacuously against a shape production does not have."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("mig_juris2", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        for earlier in ("b4e8c2a95d17_policy_version.py",
                        "a3c8e5f10b47_policy_surplus_caps_and_horizon.py"):
            s2 = importlib.util.spec_from_file_location(
                earlier[:12] + "_b", BACKEND / "alembic" / "versions" / earlier)
            m2 = importlib.util.module_from_spec(s2)
            s2.loader.exec_module(m2)
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    m2.upgrade()
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()

        with eng.begin() as connection:
            triggers = {r[0] for r in connection.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"))}
        for table in TABLES:
            assert f"{table}_no_update" in triggers, table
            assert f"{table}_no_delete" in triggers, table

    def test_it_seeds_the_governed_mapping_and_nothing_else(self):
        """Ruling B requires DE/NL/AT/FR marked from the start, so this
        revision DOES seed — four platform-governed classification rows.

        What it must still never seed is a Tenant ASSIGNMENT: which
        jurisdictions a real Tenant operates in is a decision about that
        Tenant, not reference data, and B8 gives it a provenance this
        revision could not supply.
        """
        source = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # The statement is an f-string, so the table name is an interpolated
        # NAME rather than a literal -- which is the point: it comes from the
        # package constant. ast.unparse puts the two back together.
        # The statement is an f-string, so the table name is an interpolated
        # NAME rather than a literal -- which is the point: it comes from the
        # package constant. ast.unparse puts the two back together.
        inserts = [ast.unparse(n).upper() for n in ast.walk(tree)
                   if isinstance(n, (ast.JoinedStr, ast.Constant))
                   and "INSERT INTO" in ast.unparse(n).upper()]

        assert any("CLASSIFICATION_TABLE" in t for t in inserts),             "the seed disappeared"
        for forbidden in ("ASSIGNMENT_TABLE", "ASSIGNMENT_COUNTRY_TABLE",
                          "DPIA_TABLE", "CONSULTATION_TABLE",
                          "WORKER_NOTICE_TABLE", "POLICY_VERSION",
                          "TENANT_JURISDICTION", "ECO_DPIA"):
            assert not any(forbidden in t for t in inserts), forbidden


# ═════════════════════════════════════════════════════════════════════════════
# 9. The governed mapping ships populated, and silence is not an answer
# ═════════════════════════════════════════════════════════════════════════════
class TestSpecSeededClassifications:
    """Ruling B: DE, NL, AT and FR marked from the start.

    The constant used to exist and be consumed by nothing, so the table
    shipped empty and every Tenant resolved as "not EU, no consultation".
    These tests hold the seed to its content and to its reach.
    """

    @pytest.fixture()
    def migrated(self):
        """A database built the way production is: by migration, not
        ``create_all``. The seed only exists on this path."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        for name in ("b4e8c2a95d17_policy_version.py",
                     "a3c8e5f10b47_policy_surplus_caps_and_horizon.py",
                     MIGRATION.name):
            path = BACKEND / "alembic" / "versions" / name
            spec = importlib.util.spec_from_file_location(
                name[:12] + "_seed", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with eng.begin() as connection:
                with Operations.context(
                        MigrationContext.configure(connection)):
                    module.upgrade()
        with SaSession(eng, expire_on_commit=False) as s:
            s.info["module"] = module
            yield s

    @pytest.mark.parametrize("code", ["DE", "NL", "AT", "FR"])
    def test_each_named_country_is_eu_and_requires_consultation(self, migrated,
                                                                code):
        row = classification_as_of(migrated, country_code=code, as_of=JUN)
        assert row is not None, f"{code} is not in the governed mapping"
        assert (row.is_eu, row.is_uk, row.requires_consultation) == \
            (True, False, True)
        assert row.recorded_by == "PLATFORM"

    def test_exactly_the_four_named_countries_are_seeded(self, migrated):
        """§40.4 says "and elsewhere". That is an instruction that the mapping
        be EXTENSIBLE, never a licence to infer a list — so GB, IE, BE and
        every other country an implementer might have guessed at are absent."""
        seeded = {r[0] for r in migrated.execute(sa.text(
            "SELECT country_code FROM jurisdiction_classification"))}
        assert seeded == {"AT", "DE", "FR", "NL"}

    def test_the_seed_is_the_package_constant_not_a_second_copy(self):
        """One definition. A migration with its own literal list would drift
        from the vocabulary the resolver exposes."""
        from app.jurisdiction.outcomes import SPEC_CLASSIFICATIONS

        assert {c for c, *_ in SPEC_CLASSIFICATIONS} == \
            set(SPEC_NAMED_CONSULTATION_COUNTRIES)
        source = MIGRATION.read_text(encoding="utf-8")
        assert "SPEC_CLASSIFICATIONS" in source
        tree = ast.parse(source)
        assert any(isinstance(n, ast.ImportFrom)
                   and n.module == "app.jurisdiction.outcomes"
                   for n in ast.walk(tree))

    def test_seeding_twice_adds_nothing(self, migrated):
        """Idempotent. The append-only triggers are never asked to accept a
        duplicate, and re-running an operational step is safe."""
        module = migrated.info["module"]
        before = migrated.execute(sa.text(
            "SELECT count(*) FROM jurisdiction_classification")).scalar()
        added = module.seed_spec_classifications(migrated.connection())
        after = migrated.execute(sa.text(
            "SELECT count(*) FROM jurisdiction_classification")).scalar()
        assert (added, after) == (0, before)

    def test_the_seed_is_subject_to_the_same_immutability(self, migrated):
        """Seeded rows get no privilege the operational path lacks: the
        triggers are installed BEFORE the insert."""
        with pytest.raises(IntegrityError):
            migrated.execute(sa.text(
                "UPDATE jurisdiction_classification SET is_eu = 0"))
            migrated.commit()

    def test_a_seeded_country_can_still_be_superseded(self, migrated):
        """Effective dating survives seeding: the platform can record a later
        classification, and a past date still resolves the seeded one."""
        migrated.rollback()
        record_classification(migrated, country_code="DE",
                              effective_from=date(2027, 1, 1), is_eu=True,
                              requires_consultation=False)
        migrated.commit()
        assert classification_as_of(
            migrated, country_code="DE", as_of=JUN).requires_consultation \
            is True
        assert classification_as_of(
            migrated, country_code="DE",
            as_of=date(2027, 6, 1)).requires_consultation is False


class TestUnclassifiedFailsClosed:
    """An assigned country the platform has said nothing about.

    THE DISTINCTION THIS CLASS EXISTS FOR
    -------------------------------------
    A country explicitly classified ``is_eu=False, is_uk=False,
    requires_consultation=False`` IS an answer, and it reaches NOT_APPLICABLE.
    A country with no classification in force is the absence of an answer, and
    the missing answer might have been "EU". The two produced identical
    resolutions before this was fixed, so silence waived the gate.
    """

    def test_an_unclassified_country_is_reported_not_skipped(self, db):
        assign_platform(db, organization_id=ORG, country_codes=["ZZ"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.is_assigned is True
        assert r.unclassified == ("ZZ",)
        assert r.applicability_resolved is False

    def test_an_explicitly_all_false_country_is_resolved(self, db):
        """The control case. Same three booleans, opposite meaning."""
        record_classification(db, country_code="ZZ", effective_from=JAN,
                              is_eu=False, is_uk=False,
                              requires_consultation=False)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["ZZ"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.unclassified == ()
        assert r.applicability_resolved is True
        assert (r.is_eu, r.is_uk, r.requires_consultation) == (False, False,
                                                               False)

    def test_the_two_are_distinguishable_from_the_resolution_alone(self, db):
        """Identical flags, different facts — and a caller can tell."""
        record_classification(db, country_code="ZZ", effective_from=JAN)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["ZZ"],
                        effective_from=JAN)
        assign_platform(db, organization_id=OTHER_ORG, country_codes=["QQ"],
                        effective_from=JAN)
        db.commit()
        known = resolve(db, organization_id=ORG, as_of=JUN)
        unknown = resolve(db, organization_id=OTHER_ORG, as_of=JUN)
        assert (known.is_eu, known.is_uk, known.requires_consultation) == \
            (unknown.is_eu, unknown.is_uk, unknown.requires_consultation)
        assert known.applicability_resolved is not unknown.applicability_resolved

    def test_one_unclassified_country_poisons_the_whole_set(self, db):
        """A Tenant in DE and ZZ: DE is known, ZZ is not, and ZZ might be EU.
        Partial knowledge is not knowledge."""
        _germany(db)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE", "ZZ"],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.is_eu is True           # what IS known is still reported
        assert r.unclassified == ("ZZ",)
        assert r.applicability_resolved is False

    def test_a_declared_empty_set_is_resolved(self, db):
        """B9 is untouched by this: an empty assignment has no country to be
        unclassified, so it stays NOT_APPLICABLE."""
        assign_platform(db, organization_id=ORG, country_codes=[],
                        effective_from=JAN)
        db.commit()
        r = resolve(db, organization_id=ORG, as_of=JUN)
        assert r.is_declared_empty is True
        assert r.applicability_resolved is True

    def test_a_country_classified_only_from_a_later_date_is_unclassified_before(
            self, db):
        """Effective dating and fail-closed compose: before the classification
        takes effect, the platform has not yet said anything."""
        record_classification(db, country_code="DE", effective_from=JUN,
                              is_eu=True, requires_consultation=True)
        db.commit()
        assign_platform(db, organization_id=ORG, country_codes=["DE"],
                        effective_from=JAN)
        db.commit()
        assert resolve(db, organization_id=ORG,
                       as_of=MAR).applicability_resolved is False
        assert resolve(db, organization_id=ORG,
                       as_of=JUN).applicability_resolved is True
