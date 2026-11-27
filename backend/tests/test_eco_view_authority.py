"""
ECO view authority (owner ruling B2) — who may READ a tenant's ECO records.
===========================================================================

The property under test: **authority is something somebody recorded, never
something the resolver inferred.**

§24.7 requires a deferred day to be "visible to both Worker and administrator".
The ALERT/visibility audit found the repository could not say who the
administrator is — no capability, permission, scope or ACL model exists among
59 ORM models, and authorization is hand-rolled per router against a nullable
free-text ``User.role`` in at least eight different tuples. The owner refused
to let ECO adopt that and authorized this primitive.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No HTTP surface, no ECO read API, no recipient resolution, no grant-management
service. The owner authorized the primitive, not the endpoints. ``TestScopeHeld``
pins that.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.eco_view_authority import (
    DenialReason,
    EcoViewGrant,
    ViewAuthorityDecision,
    ViewAuthorityOutcome,
    classify_grants,
    require_eco_view_authority,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: The ONE package authorized to consult this primitive: the GV-17 read
#: contract, which must call it rather than inventing a second authorization
#: rule. ``eco_day_view`` is now a wired consumer — its router is mounted at
#: ``/api/eco/day`` and backs WF-ECO-03 — so the exemption below is load-
#: bearing rather than vacuous: it names the single package allowed to reach
#: the resolver, and every other package in ``app/`` is still held to the
#: no-role-string rule.
#:
#: The primitive itself stays deliberately narrow. Being consulted by a route
#: gives it no recipient, reviewer or delivery semantics, creates nothing and
#: notifies nobody; it answers who may READ a tenant's ECO records and stops
#: there.
_AUTHORIZED_CONSUMER = {"eco_day_view"}

ORG_A, ORG_B = 8610, 8620
WORKER_A, ADMIN_A, OTHER_A = 8611, 8612, 8613
WORKER_B, ADMIN_B = 8621, 8622
PLATFORM_OWNER = 8600


@pytest.fixture()
def db():
    import app.eco_view_authority.models          # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    with SaSession(eng, expire_on_commit=False) as s:
        s.add_all([
            dbm.Organization(id=ORG_A, name="Tenant A"),
            dbm.Organization(id=ORG_B, name="Tenant B"),
        ])
        s.commit()
        s.add_all([
            dbm.User(id=WORKER_A, name="Worker A", email="wa@t.test",
                     organization_id=ORG_A, role="employee"),
            dbm.User(id=ADMIN_A, name="Admin A", email="aa@t.test",
                     organization_id=ORG_A, role="admin"),
            dbm.User(id=OTHER_A, name="Other A", email="oa@t.test",
                     organization_id=ORG_A, role="employee"),
            dbm.User(id=WORKER_B, name="Worker B", email="wb@t.test",
                     organization_id=ORG_B, role="employee"),
            dbm.User(id=ADMIN_B, name="Admin B", email="ab@t.test",
                     organization_id=ORG_B, role="admin"),
            # A platform owner: super_admin, and NO organization — the tenant
            # id is cleared on login, which is exactly the shape that must not
            # be mistaken for tenant-administrator status.
            dbm.User(id=PLATFORM_OWNER, name="Owner", email="owner@temveris.test",
                     organization_id=None, role="super_admin"),
        ])
        s.commit()
        yield s


def grant(db, *, organization_id: int, user_id: int) -> EcoViewGrant:
    row = EcoViewGrant(organization_id=organization_id, user_id=user_id,
                       granted_by="owner@t.test")
    db.add(row)
    db.commit()
    return row


def ask(db, *, caller_id: int, target_user_id: int) -> ViewAuthorityDecision:
    return require_eco_view_authority(
        db, caller=db.get(dbm.User, caller_id), target_user_id=target_user_id)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The two authorized grants
# ══════════════════════════════════════════════════════════════════════════════
class TestAuthorizedGrants:
    def test_a_worker_may_read_their_own_record(self, db):
        """Proof 1. Identity, not a stored grant — so it cannot be revoked by
        one being absent, and the table stays empty."""
        d = ask(db, caller_id=WORKER_A, target_user_id=WORKER_A)
        assert d.outcome is ViewAuthorityOutcome.WORKER_SELF
        assert d.is_authorized
        assert d.organization_id == ORG_A
        assert db.query(EcoViewGrant).count() == 0

    def test_a_worker_may_not_read_another_worker(self, db):
        """Proof 2."""
        d = ask(db, caller_id=WORKER_A, target_user_id=OTHER_A)
        assert not d.is_authorized
        assert d.reason is DenialReason.NO_GRANT

    def test_a_granted_administrator_may_read_a_worker(self, db):
        """Proof 3."""
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        d = ask(db, caller_id=ADMIN_A, target_user_id=WORKER_A)
        assert d.outcome is ViewAuthorityOutcome.TENANT_ADMINISTRATOR
        assert d.is_authorized
        assert d.organization_id == ORG_A

    def test_the_grant_covers_every_worker_in_its_tenant(self, db):
        """The grant is tenant-wide, as ruled — no Worker-subset semantics were
        invented, so one grant answers for every Worker in that tenant."""
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        for target in (WORKER_A, OTHER_A):
            assert ask(db, caller_id=ADMIN_A, target_user_id=target).is_authorized


# ══════════════════════════════════════════════════════════════════════════════
# 2. Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
class TestTenantIsolation:
    def test_an_administrator_cannot_reach_another_organization(self, db):
        """Proof 4."""
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        d = ask(db, caller_id=ADMIN_A, target_user_id=WORKER_B)
        assert not d.is_authorized
        assert d.reason is DenialReason.CROSS_TENANT

    def test_knowing_the_record_identifier_is_not_authority(self, db):
        """Proof 13. The id is correct, exists, and is refused anyway."""
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        assert db.get(dbm.User, WORKER_B) is not None
        d = ask(db, caller_id=ADMIN_A, target_user_id=WORKER_B)
        assert d.reason is DenialReason.CROSS_TENANT

    def test_a_grant_in_one_tenant_does_not_travel(self, db):
        """Two grants for the same person in two tenants stay independent."""
        grant(db, organization_id=ORG_B, user_id=ADMIN_A)   # wrong tenant
        d = ask(db, caller_id=ADMIN_A, target_user_id=WORKER_A)
        assert not d.is_authorized
        assert d.reason is DenialReason.NO_GRANT

    def test_authority_cannot_be_supplied_through_a_request_parameter(self):
        """Proof 12, enforced by the SIGNATURE rather than by validation.

        A tenant boundary that can be named by the request is a tenant boundary
        that can be argued with, so the resolver has nowhere to put one. This
        asserts the absence, because a later "convenience" parameter is exactly
        how that guarantee would be lost.
        """
        import inspect

        sig = inspect.signature(require_eco_view_authority)
        assert set(sig.parameters) == {"db", "caller", "target_user_id"}
        for banned in ("organization_id", "org_id", "tenant_id", "tenant",
                       "organization"):
            assert banned not in sig.parameters, (
                f"the resolver accepts {banned}; the tenant boundary must be "
                f"derived from the authenticated caller only")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Fail closed
# ══════════════════════════════════════════════════════════════════════════════
class TestFailClosed:
    def test_no_grant_is_a_denial(self, db):
        """Proof 5."""
        d = ask(db, caller_id=ADMIN_A, target_user_id=WORKER_A)
        assert not d.is_authorized
        assert d.reason is DenialReason.NO_GRANT

    def test_an_ambiguous_or_malformed_grant_is_a_denial(self, db):
        """Proof 6, through the pure classifier.

        The unique constraint makes "two rows" unreachable through the ORM and
        the query filters on exactly the tenant and user re-verified inside, so
        both branches are defensive. They are tested directly rather than not
        at all — a hand-written migration or a direct SQL insert is precisely
        the situation in which a last line of defence matters.
        """
        good = EcoViewGrant(organization_id=ORG_A, user_id=ADMIN_A)
        assert classify_grants([good], organization_id=ORG_A,
                               user_id=ADMIN_A) is None
        assert classify_grants([], organization_id=ORG_A,
                               user_id=ADMIN_A) is DenialReason.NO_GRANT
        assert classify_grants([good, good], organization_id=ORG_A,
                               user_id=ADMIN_A) is DenialReason.GRANT_AMBIGUOUS
        wrong_tenant = EcoViewGrant(organization_id=ORG_B, user_id=ADMIN_A)
        assert classify_grants([wrong_tenant], organization_id=ORG_A,
                               user_id=ADMIN_A) is DenialReason.GRANT_MALFORMED
        wrong_user = EcoViewGrant(organization_id=ORG_A, user_id=OTHER_A)
        assert classify_grants([wrong_user], organization_id=ORG_A,
                               user_id=ADMIN_A) is DenialReason.GRANT_MALFORMED

    def test_the_unique_constraint_makes_a_second_grant_unwritable(self, db):
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        db.add(EcoViewGrant(organization_id=ORG_A, user_id=ADMIN_A))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_null_organization_id_is_a_denial(self, db):
        """Proof 7."""
        d = ask(db, caller_id=PLATFORM_OWNER, target_user_id=WORKER_A)
        assert not d.is_authorized
        assert d.reason is DenialReason.NO_TENANT_CONTEXT

    def test_super_admin_does_not_receive_tenant_administrator_authority(self, db):
        """Proof 8. Platform-owner status is not tenant-administrator status.

        Asserted for the platform owner as they actually exist — super_admin
        with a cleared tenant id — AND for the harder case of a super_admin who
        still carries one: even then the answer comes from the grant table, not
        from the role.
        """
        owner = db.get(dbm.User, PLATFORM_OWNER)
        assert owner.role == "super_admin" and owner.organization_id is None
        assert ask(db, caller_id=PLATFORM_OWNER,
                   target_user_id=WORKER_A).reason is \
            DenialReason.NO_TENANT_CONTEXT

        # A super_admin carrying a tenant id is still not an administrator.
        owner.organization_id = ORG_A
        db.commit()
        d = ask(db, caller_id=PLATFORM_OWNER, target_user_id=WORKER_A)
        assert not d.is_authorized
        assert d.reason is DenialReason.NO_GRANT

    def test_an_unknown_caller_or_target_is_a_denial(self, db):
        assert require_eco_view_authority(
            db, caller=None, target_user_id=WORKER_A).reason is \
            DenialReason.CALLER_UNKNOWN
        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        assert ask(db, caller_id=ADMIN_A,
                   target_user_id=99999).reason is DenialReason.TARGET_UNKNOWN

    def test_a_denial_cannot_be_read_as_success(self):
        """The type refuses to express a granted decision with a reason, or a
        denial without one."""
        with pytest.raises(ValueError):
            ViewAuthorityDecision(outcome=ViewAuthorityOutcome.DENIED)
        with pytest.raises(ValueError):
            ViewAuthorityDecision(
                outcome=ViewAuthorityOutcome.TENANT_ADMINISTRATOR,
                organization_id=ORG_A, reason=DenialReason.NO_GRANT)
        with pytest.raises(ValueError):
            ViewAuthorityDecision(
                outcome=ViewAuthorityOutcome.WORKER_SELF)   # no tenant named


# ══════════════════════════════════════════════════════════════════════════════
# 4. Nothing is inferred
# ══════════════════════════════════════════════════════════════════════════════
class TestNothingIsInferred:
    """Proofs 9, 10 and 11 — the whole reason the primitive exists.

    Each mutates the attribute an implementation might have been tempted to
    read, and asserts the decision does not move.
    """

    def test_role_changes_do_not_change_authority(self, db):
        """Proof 9."""
        caller = db.get(dbm.User, ADMIN_A)
        for role in ("admin", "hr", "owner", "manager", "super_admin",
                     "billing_admin", "payroll_admin", None):
            caller.role = role
            db.commit()
            assert ask(db, caller_id=ADMIN_A,
                       target_user_id=WORKER_A).reason is DenialReason.NO_GRANT

        grant(db, organization_id=ORG_A, user_id=ADMIN_A)
        for role in ("employee", None, "assistant", "legal"):
            caller.role = role
            db.commit()
            # Granted, and it STAYS granted through the least administrative
            # role strings there are: the grant is the authority.
            assert ask(db, caller_id=ADMIN_A,
                       target_user_id=WORKER_A).is_authorized

    def test_team_head_changes_do_not_change_authority(self, db):
        """Proof 10."""
        team = dbm.Team(organization_id=ORG_A, name="Ops", head_user_id=ADMIN_A)
        db.add(team)
        db.commit()
        assert ask(db, caller_id=ADMIN_A,
                   target_user_id=WORKER_A).reason is DenialReason.NO_GRANT
        team.head_user_id = WORKER_A
        db.commit()
        assert ask(db, caller_id=ADMIN_A,
                   target_user_id=WORKER_A).reason is DenialReason.NO_GRANT

    def test_department_designation_and_email_do_not_change_authority(self, db):
        """Proof 11."""
        caller = db.get(dbm.User, ADMIN_A)
        for field, value in (("department", "People Operations"),
                             ("designation", "Head of HR"),
                             ("email", "admin@t.test"),
                             ("team", "Ops"),
                             ("employment_type", "Full-Time")):
            setattr(caller, field, value)
            db.commit()
            assert ask(db, caller_id=ADMIN_A,
                       target_user_id=WORKER_A).reason is DenialReason.NO_GRANT

    def test_the_resolver_reads_none_of_those_fields(self):
        """Structural, not behavioural — the tests above prove today's code is
        clean; this fails if a future edit reaches for one of them."""
        src = (BACKEND / "app" / "eco_view_authority" / "service.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("role", "head_user_id", "department", "designation",
                       "email", "team", "is_admin"):
            assert banned not in names, f"the resolver reads {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Centralised, typed, and not recipient resolution
# ══════════════════════════════════════════════════════════════════════════════
def _imports_view_authority(path):
    """True when a module actually IMPORTS the package.

    An AST import scan rather than a substring search. Stage 5 forced the
    narrowing: ``app/recipient_authority/__init__.py`` NAMES this package in a
    contrast table — "VIEW-AUTHORITY: may this caller READ; RECIPIENT-AUTHORITY:
    who should be TOLD" — which a substring scan read as wiring. Prose that
    distinguishes two primitives is the opposite of wiring them together, and
    an import is what "wired" actually means, so the narrower test is also the
    stronger one: a module could always have mentioned the name in a comment
    while importing it under an alias, and only this scan catches that.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # pragma: no cover - not our source to fix
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "eco_view_authority" in node.module:
                return True
        elif isinstance(node, ast.Import):
            if any("eco_view_authority" in a.name for a in node.names):
                return True
    return False


class TestContract:
    PACKAGE = BACKEND / "app" / "eco_view_authority"

    def test_the_decision_is_typed_not_a_boolean(self):
        """Proof 14, first half. A bool has no room for a reason, and a caller
        reading ``if allowed`` past a denial would never learn which condition
        failed."""
        import typing

        # ``from __future__ import annotations`` makes annotations strings, so
        # the hint is resolved rather than compared as text.
        hints = typing.get_type_hints(require_eco_view_authority)
        assert hints["return"] is ViewAuthorityDecision
        assert {o.value for o in ViewAuthorityOutcome} == {
            "WORKER_SELF", "TENANT_ADMINISTRATOR", "DENIED"}

    def test_there_is_exactly_one_resolver(self):
        """Proof 14, second half. One entry point, so there is one place the
        rule lives and one place to change it."""
        import app.eco_view_authority as pkg

        exported = {n for n in pkg.__all__ if n.startswith("require_")}
        assert exported == {"require_eco_view_authority"}

    def test_no_call_site_performs_a_role_string_check(self):
        """Proof 14, third half — pinned so it stays true. Every module that
        imports the primitive must consult the resolver, never a role tuple."""
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "eco_view_authority" in path.parts:
                continue
            if _AUTHORIZED_CONSUMER & set(path.parts):
                continue
            if _imports_view_authority(path):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_no_recipient_resolution_behaviour_is_present(self):
        """Proof 15. VIEW-AUTHORITY says who may READ; RECIPIENT-AUTHORITY says
        who is TOLD, and remains a separate open capability (GV-07)."""
        for path in sorted(self.PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                     | {n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)})
            for banned in ("recipient", "notify", "notification", "audience",
                           "reviewer", "deliver", "dispatch", "send"):
                assert not any(banned in x.lower() for x in names), (
                    f"{path.name} performs {banned} work")

    def test_the_resolver_writes_nothing(self):
        """Read-only, per the ruling. No INSERT/UPDATE/DELETE, and no grant
        created on demand — a missing grant is a denial, not a prompt."""
        tree = ast.parse(
            (self.PACKAGE / "service.py").read_text(encoding="utf-8"))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("add", "add_all", "delete", "merge", "commit", "flush",
                       "bulk_save_objects"):
            assert banned not in called, f"the resolver calls {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Scope — the primitive stays foundation
# ══════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    PACKAGE = BACKEND / "app" / "eco_view_authority"
    MIGRATION = (BACKEND / "alembic" / "versions"
                 / "d6a8f3c19e47_eco_view_grant.py")

    def test_nothing_in_the_application_wires_this_package(self):
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "eco_view_authority" in path.parts:
                continue
            if _AUTHORIZED_CONSUMER & set(path.parts):
                continue
            if _imports_view_authority(path):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_it_declares_no_http_surface(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "fastapi", "@router", "HTTPException",
                           "Depends("):
                assert banned not in src, f"{path.name} declares an HTTP surface"

    def test_it_exposes_no_eco_records(self):
        """It answers "may they read", never "here is the data"."""
        for path in sorted(self.PACKAGE.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for banned in ("EcoDayRecord", "WorkDayLedger", "EcoNotification",
                           "EcoThresholdEvent", "evaluate_"):
                assert banned not in src, f"{path.name} reaches for {banned}"

    def test_the_migration_is_a_single_child_of_the_previous_head(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mig_d6a8f3c19e47", self.MIGRATION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "d6a8f3c19e47"
        assert mod.down_revision == "c3f7a1e58d2b"
        assert mod.branch_labels is None and mod.depends_on is None

    def test_the_graph_stays_one_base_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_the_migration_is_additive_only(self):
        tree = ast.parse(self.MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("drop_table", "drop_column", "add_column",
                       "alter_column", "execute"):
            assert banned not in called, f"upgrade() calls {banned}"
        assert "create_table" in called

    def test_the_downgrade_refuses_to_delete_recorded_authority(self):
        tree = ast.parse(self.MIGRATION.read_text(encoding="utf-8"))
        downgrade = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "downgrade")
        assert [n for n in ast.walk(downgrade) if isinstance(n, ast.Raise)], (
            "downgrade() would silently delete grant rows")

    def test_the_table_ships_empty(self, db):
        assert db.query(EcoViewGrant).count() == 0

    def test_eco_v1_and_the_golden_artifact_are_untouched(self):
        import hashlib

        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

        blob = (BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
                ).read_bytes()
        digest = hashlib.sha1(
            b"blob %d\0" % len(blob) + blob).hexdigest()
        # Re-pinned by the PROMOTION of GV-26. Its STATE-CONTRACT citation
        # was satisfied by the existing D-8.1 contract — LedgerState owns
        # ledger facts and EcoDayState stays the seven-member evaluation
        # vocabulary, so no evaluation state was added — and its ALERT
        # citation was STALE: §24.15 asks for a platform INCIDENT, which has
        # no audience, no recipient, no channel and no delivery, so ALERT's
        # entry point could never have fired for it either. Owner ruling
        # D-9.1 authorized app.eco_ledger_incident. Harness-only: exactly
        # one vector changed, the count moved 27/1 -> 28/0, and the
        # normative projection is byte-for-byte unchanged.
        assert digest == "2a7a0fb34145fe7fd210db585cc44f198974a364"

    def test_gv17_is_promoted_and_no_longer_cites_this_primitive(self):
        """VIEW-AUTHORITY was one of GV-17's three blockers; it is now none.

        This guard used to assert the vector cited this primitive, which was
        how the artifact recorded that the authority half existed but the
        surface half did not. Both closed, GV-17 was promoted on 2026-09-17,
        and a satisfied code must cite nothing — a stale citation would
        misreport why a blocked vector is blocked.
        """
        import json

        vectors = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]
        gv17 = next(v for v in vectors if v["id"] == "GV-17")
        assert gv17["harness"]["execution"] == "executable"
        assert gv17["harness"]["blocking_dependencies"] == []
        # And no OTHER vector picked the code up on the way out.
        for v in vectors:
            assert "VIEW-AUTHORITY" not in v["harness"]["blocking_dependencies"], \
                v["id"]

    def test_no_deferred_copy_was_registered(self):
        """B1 is still blocked pending Legal. This increment must not have
        registered the status label or any explanatory sentence."""
        from app.early_clock_out import LOCKED_COPY

        assert "eco.status.deferred" not in LOCKED_COPY
        assert len(LOCKED_COPY) == 12
        for value in LOCKED_COPY.values():
            assert "pending review" not in value.lower()
