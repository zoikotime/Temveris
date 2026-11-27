"""Offboarding tenant-isolation tests (§1, §9 release gate).

Release gate: a case created under one tenant MUST be invisible to another, the
repository MUST scope every read by tenant, and cross-tenant authorization MUST
be denied. These tests use the in-memory SQLite DB from conftest.
"""
from app.offboarding.rbac import AuthzContext, ResourceAttrs, Role, evaluate
from app.offboarding.repository import Repository
from app.offboarding.schemas import CaseCreate
from app.offboarding.services import OffboardingContext, OffboardingService


def _ctx(db, tenant):
    authz = AuthzContext(user_id=1, tenant_id=tenant, roles=frozenset({Role.SUPER_ADMIN}))
    return OffboardingContext(db=db, authz=authz, correlation_id="cor_test")


def _create(db, tenant, engagement):
    svc = OffboardingService(_ctx(db, tenant))
    return svc.create_case(CaseCreate(engagement_id=engagement))


class TestTenantIsolation:
    def test_case_invisible_to_other_tenant(self, db):
        case = _create(db, "tenant_a", "ENG-A")
        assert Repository(db, "tenant_a").get_case(case.id) is not None
        # A different tenant cannot read it — returns None (→ 404 at the edge).
        assert Repository(db, "tenant_b").get_case(case.id) is None

    def test_list_is_scoped_per_tenant(self, db):
        _create(db, "tenant_x", "ENG-X1")
        _create(db, "tenant_x", "ENG-X2")
        _create(db, "tenant_y", "ENG-Y1")
        assert len(Repository(db, "tenant_x").list_cases()) == 2
        assert len(Repository(db, "tenant_y").list_cases()) == 1

    def test_active_case_lookup_is_scoped(self, db):
        _create(db, "tenant_m", "SHARED-ENG")
        # Same engagement id under a different tenant is NOT a conflict.
        assert Repository(db, "tenant_n").active_case_for_engagement("SHARED-ENG") is None
        other = OffboardingService(_ctx(db, "tenant_n")).create_case(
            CaseCreate(engagement_id="SHARED-ENG"))
        assert other is not None


class TestCrossTenantAuthorization:
    def test_cross_tenant_action_denied(self, db):
        case = _create(db, "tenant_a", "ENG-Z")
        # Actor in tenant_b, resource in tenant_a → denied regardless of role.
        actor_b = AuthzContext(user_id=2, tenant_id="tenant_b",
                               roles=frozenset({Role.SUPER_ADMIN}))
        res = ResourceAttrs(tenant_id=case.tenant_id, case_status=case.status,
                            legal_hold_status=case.legal_hold_status)
        assert not evaluate(actor_b, "case.view", res).allowed
