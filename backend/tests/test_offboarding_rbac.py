"""Offboarding RBAC + ABAC tests (§4).

Covers the permission matrix, the ABAC constraints (cross-tenant, manager
department scope, legal-hold gating), legacy-role mapping, and field-level
redaction. Pure — no DB.
"""
from app.offboarding.rbac import (
    AuthzContext, Permission, ResourceAttrs, Role, can, evaluate,
    permissions_for, redact_case, roles_for_user,
)


def _ctx(roles, tenant="t1", department=None):
    return AuthzContext(user_id=1, tenant_id=tenant, roles=frozenset(roles),
                        department=department)


def _res(tenant="t1", department=None, legal_hold_status="none", status="draft"):
    return ResourceAttrs(tenant_id=tenant, department=department,
                         legal_hold_status=legal_hold_status, case_status=status)


class _FakeUser:
    def __init__(self, role=None, offboarding_roles=None):
        self.role = role
        if offboarding_roles is not None:
            self.offboarding_roles = offboarding_roles


class TestRoleMapping:
    def test_super_admin_maps(self):
        assert Role.SUPER_ADMIN in roles_for_user(_FakeUser(role="super_admin"))

    def test_hr_maps_to_hr_admin(self):
        assert roles_for_user(_FakeUser(role="hr")) == frozenset({Role.HR_ADMIN})

    def test_employee_has_no_offboarding_role(self):
        assert roles_for_user(_FakeUser(role="employee")) == frozenset()

    def test_explicit_roles_take_precedence(self):
        roles = roles_for_user(_FakeUser(role="employee", offboarding_roles=["payroll_lead"]))
        assert roles == frozenset({Role.PAYROLL_LEAD})


class TestPermissions:
    def test_super_admin_has_everything(self):
        assert permissions_for(frozenset({Role.SUPER_ADMIN})) == frozenset(Permission)

    def test_separation_of_duties(self):
        # Payroll admin can close payroll but NOT approve a case or close billing.
        perms = permissions_for(frozenset({Role.PAYROLL_ADMIN}))
        assert Permission.PAYROLL_CLOSE in perms
        assert Permission.CASE_APPROVE not in perms
        assert Permission.BILLING_CLOSE not in perms

    def test_manager_cannot_approve(self):
        assert not can(_ctx({Role.MANAGER}), "case.approve", _res())

    def test_hr_lead_can_approve(self):
        assert can(_ctx({Role.HR_LEAD}), "case.approve", _res())


class TestABAC:
    def test_cross_tenant_is_denied_even_for_super_admin(self):
        d = evaluate(_ctx({Role.SUPER_ADMIN}, tenant="t1"), "case.view", _res(tenant="t2"))
        assert not d.allowed
        assert d.reason == "cross_tenant_denied"

    def test_manager_confined_to_own_department(self):
        ctx = _ctx({Role.MANAGER}, department="Sales")
        assert can(ctx, "case.view", _res(department="Sales"))
        assert not can(ctx, "case.view", _res(department="Engineering"))

    def test_legal_hold_blocks_destructive_steps(self):
        # An HR lead may normally cancel/generate; a live legal hold blocks it.
        ctx = _ctx({Role.HR_LEAD})
        assert not can(ctx, "case.cancel", _res(legal_hold_status="active"))
        # Legal admin (holds release permission) is allowed through.
        assert can(_ctx({Role.LEGAL_ADMIN, Role.HR_LEAD}), "case.cancel",
                   _res(legal_hold_status="active"))

    def test_missing_permission_is_denied(self):
        assert not can(_ctx({Role.SUPPORT_ADMIN}), "case.approve", _res())


class TestFieldRedaction:
    def test_termination_reason_redacted_without_permission(self):
        data = {"termination_reason": "misconduct", "notes": "ok"}
        out = redact_case(data, _ctx({Role.SUPPORT_ADMIN}))
        assert out["termination_reason"] == "***redacted***"
        assert out["notes"] == "ok"  # non-sensitive untouched

    def test_hr_admin_can_see_termination_reason(self):
        data = {"termination_reason": "misconduct"}
        out = redact_case(data, _ctx({Role.HR_ADMIN}))
        assert out["termination_reason"] == "misconduct"

    def test_payroll_notes_only_for_payroll_roles(self):
        data = {"payroll_notes": "final settlement"}
        assert redact_case(data, _ctx({Role.HR_ADMIN}))["payroll_notes"] == "***redacted***"
        assert redact_case(data, _ctx({Role.PAYROLL_ADMIN}))["payroll_notes"] == "final settlement"
