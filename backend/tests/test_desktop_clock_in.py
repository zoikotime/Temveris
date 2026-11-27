"""Desktop-only clock-in policy (app/attendance_desktop_policy.py).

Now a per-organization SETTING (`attendance.desktop_only_clock_in`) rather than
an environment variable listing numeric organization ids. The old form hardcoded
`organization_id == 1` as its default, so a fresh deployment gave whichever
customer happened to be org 1 another company's clock-in restriction, and no
tenant could switch the rule on or off for themselves.

The policy is asserted from both directions: an organization that enabled it IS
blocked from a browser, and everything else — organizations that did not enable
it, privileged roles, the desktop client, and clock-OUT — is NOT affected.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from fastapi import HTTPException

from database import models
from database.database import Base

from app import attendance_desktop_policy as policy
from app.attendance_desktop_policy import (
    DESKTOP_APP_REQUIRED,
    desktop_clock_in_required,
    enforce_desktop_clock_in,
)
from app.employees.router import clock_in

_org_seq = itertools.count(93000)
_uid_seq = itertools.count(90000)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def restricted_org(db):
    """An organization that has switched the policy ON."""
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Restricted{oid}"))
    db.add(models.AppSettings(organization_id=oid,
                              key="attendance.desktop_only_clock_in",
                              value="true"))
    db.commit()
    return oid


@pytest.fixture
def other_org(db):
    """An organization that has not — i.e. the default."""
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Other{oid}"))
    db.commit()
    return oid


class _Req:
    """Minimal stand-in for fastapi.Request — is_desktop_client reads .headers."""

    def __init__(self, headers=None):
        self.headers = headers or {}


BROWSER = _Req({"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/126.0 Safari/537.36"})
DESKTOP_HEADER = _Req({"x-client-type": "desktop"})
DESKTOP_UA = _Req({
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0) Chrome/126.0 Electron/31.0.0 "
        "Safari/537.36 temveris-desktop/1.0.8"
    )
})


def _mk_user(db, org_id, role="employee"):
    u = models.User(
        name="Policy Worker",
        email=f"dp_{uuid.uuid4().hex}@example.com",
        role=role,
        is_active=True,
        organization_id=org_id,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _clear_sessions(db, user_id):
    db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user_id
    ).delete()
    db.commit()


# ── predicate ────────────────────────────────────────────────────────────────

def test_predicate_true_only_where_the_setting_is_on(db, restricted_org):
    assert desktop_clock_in_required(_mk_user(db, restricted_org, "employee"), db) is True


def test_default_is_off(db, other_org):
    """The rule must ship OFF. It used to default to organization id 1."""
    assert desktop_clock_in_required(_mk_user(db, other_org, "employee"), db) is False


def test_team_scope_can_exempt_a_team(db, restricted_org):
    """Field staff without a laptop still need to clock in."""
    u = _mk_user(db, restricted_org, "employee")
    u.team = "Field"
    db.add(models.AppSettings(organization_id=restricted_org, team_key="Field",
                              key="attendance.desktop_only_clock_in", value="false"))
    db.commit()
    assert desktop_clock_in_required(u, db) is False


def test_employee_scope_can_exempt_one_person(db, restricted_org):
    u = _mk_user(db, restricted_org, "employee")
    db.add(models.AppSettings(organization_id=restricted_org, user_id=u.id,
                              key="attendance.desktop_only_clock_in", value="false"))
    db.commit()
    assert desktop_clock_in_required(u, db) is False


def test_no_hardcoded_organization_id_remains():
    """Guard: the numeric-tenant rule must not come back."""
    import inspect
    src = inspect.getsource(policy)
    for banned in ("DESKTOP_ONLY_CLOCK_IN_ORG_IDS", "_restricted_org_ids",
                   "_DEFAULT_ORG_IDS"):
        assert banned not in src.split('"""', 2)[-1], f"{banned} is back"


@pytest.mark.parametrize(
    "role", ["admin", "hr", "owner", "manager", "billing_admin", "super_admin"]
)
def test_predicate_false_for_every_privileged_role(db, restricted_org, role):
    """Requirement 3: only role == 'employee' is restricted."""
    assert desktop_clock_in_required(_mk_user(db, restricted_org, role), db) is False


def test_predicate_false_for_employee_in_another_org(db, other_org):
    """Requirement 4: organizations that did not enable it are untouched."""
    assert desktop_clock_in_required(_mk_user(db, other_org, "employee"), db) is False


def test_predicate_false_when_org_is_null(db):
    """super_admin rows carry organization_id = NULL."""
    assert desktop_clock_in_required(_mk_user(db, None, "super_admin"), db) is False


def test_role_match_is_case_and_whitespace_insensitive(db, restricted_org):
    u = _mk_user(db, restricted_org, "employee")
    u.role = "  Employee  "
    assert desktop_clock_in_required(u, db) is True


def test_predicate_false_without_a_session(db, restricted_org):
    """Call sites that have no db must fail OPEN, not guess."""
    assert desktop_clock_in_required(_mk_user(db, restricted_org, "employee")) is False


def test_predicate_fails_open_on_unexpected_error(db, restricted_org, monkeypatch):
    """A bug in this module must never block an org from clocking in."""
    import app.attendance_policy as ap

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ap, "policy_bool", boom)

    class _U:
        role = "employee"
        organization_id = restricted_org

    assert desktop_clock_in_required(_U(), db) is False


# ── enforcement ──────────────────────────────────────────────────────────────

def test_enforce_blocks_browser_for_covered_employee(db, restricted_org):
    user = _mk_user(db, restricted_org, "employee")
    with pytest.raises(HTTPException) as exc:
        enforce_desktop_clock_in(user, BROWSER, db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == DESKTOP_APP_REQUIRED
    assert "Desktop App" in exc.value.detail["message"]


@pytest.mark.parametrize("req", [DESKTOP_HEADER, DESKTOP_UA])
def test_enforce_allows_desktop_client(db, restricted_org, req):
    """Both detection signals work: the explicit header and the Electron UA."""
    enforce_desktop_clock_in(_mk_user(db, restricted_org, "employee"), req, db)


def test_enforce_allows_admin_from_browser(db, restricted_org):
    enforce_desktop_clock_in(_mk_user(db, restricted_org, "admin"), BROWSER, db)


def test_enforce_allows_other_org_employee_from_browser(db, other_org):
    enforce_desktop_clock_in(_mk_user(db, other_org, "employee"), BROWSER, db)


def test_enforce_allows_when_request_is_missing(db, other_org):
    """`request` is Optional on the endpoint; a missing one must not 500."""
    enforce_desktop_clock_in(_mk_user(db, other_org, "employee"), None, db)


# ── endpoint wiring ──────────────────────────────────────────────────────────

def test_clock_in_endpoint_rejects_covered_employee_from_browser(db, restricted_org):
    """Requirement 7/10: enforced server-side, not just by hiding the button."""
    user = _mk_user(db, restricted_org, "employee")
    _clear_sessions(db, user.id)

    with pytest.raises(HTTPException) as exc:
        clock_in(db=db, user=user, request=BROWSER)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == DESKTOP_APP_REQUIRED

    # And no session row was created — the guard runs before any DB write.
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id
    ).count() == 0


def test_clock_in_endpoint_accepts_covered_employee_from_desktop(db, restricted_org):
    user = _mk_user(db, restricted_org, "employee")
    _clear_sessions(db, user.id)

    res = clock_in(db=db, user=user, request=DESKTOP_HEADER)
    assert res.get("success") is not False
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id,
        models.ClockSession.clock_out_time == None,  # noqa: E711
    ).count() == 1


def test_clock_in_endpoint_accepts_other_org_employee_from_browser(db, other_org, monkeypatch):
    """Requirement 4 at the endpoint level.

    Scoped to the per-org policy: attendance actions are now desktop-app only
    product-wide (WEB_ATTENDANCE_DISABLED), which blocks browser clock-in for
    every org. This test pins the narrower policy's own behaviour — that an
    org WITHOUT the setting is not restricted by IT — so it turns the global
    rule off. Remove this monkeypatch if the global rule is ever lifted.
    """
    from app import attendance_desktop_policy as _adp
    monkeypatch.setattr(_adp, "WEB_ATTENDANCE_DISABLED", False)
    user = _mk_user(db, other_org, "employee")
    _clear_sessions(db, user.id)

    res = clock_in(db=db, user=user, request=BROWSER)
    assert res.get("success") is not False
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id,
        models.ClockSession.clock_out_time == None,  # noqa: E711
    ).count() == 1


def test_clock_out_is_not_gated(db, restricted_org, monkeypatch):
    """Requirement 6: a worker whose desktop app died must not be trapped.

    Clock in from the desktop, then clock out. The desktop-only CLOCK-IN policy
    never reaches clock-out — this test pins that. (`clock_out` now accepts a
    `request` for the separate desktop-app-only attendance rule; calling it
    without one, as here, is an in-process call and is not gated.)

    The first call returns EARLY_CLOCK_OUT_REASON_REQUIRED because the fixture
    worked 0 seconds; that is the pre-existing early-clock-out gate, not this
    policy. Supplying a reason completes the clock-out, proving the whole exit
    path stays open from a browser.
    """
    from app.employees import router as _emp_router
    from app.employees.router import clock_out, ClockOutBody
    from fastapi import BackgroundTasks

    # The Early Clock Out feature is temporarily disabled in production; this
    # test asserts the desktop policy does not gate the reason path, so it turns
    # the preserved gate back on. See EARLY_CLOCK_OUT_FEATURE_ENABLED.
    monkeypatch.setattr(_emp_router, "EARLY_CLOCK_OUT_FEATURE_ENABLED", True)

    user = _mk_user(db, restricted_org, "employee")
    _clear_sessions(db, user.id)
    clock_in(db=db, user=user, request=DESKTOP_HEADER)

    first = clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=None)
    # Blocked by the early-clock-out reason gate, NOT by the desktop policy.
    assert first.get("code") == "EARLY_CLOCK_OUT_REASON_REQUIRED"

    clock_out(
        background_tasks=BackgroundTasks(),
        db=db,
        user=user,
        body=ClockOutBody(reason="Desktop app crashed, leaving from the website."),
    )

    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id,
        models.ClockSession.clock_out_time == None,  # noqa: E711
    ).count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# WEB ATTENDANCE LOCKOUT — attendance actions are desktop-app only
# ══════════════════════════════════════════════════════════════════════════════
# A separate, BROADER rule than the per-org policy above: every role, every
# organization (existing and future), no per-tenant configuration. These tests
# use a plain organization with NO settings rows at all, which is exactly what a
# newly provisioned tenant looks like.

_ATTENDANCE_ROLES = ["employee", "admin", "hr", "manager", "owner", "billing_admin"]


@pytest.fixture
def plain_org(db):
    """A brand-new organization: no AppSettings rows, nothing configured."""
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Fresh{oid}"))
    db.commit()
    return oid


def _detail(exc_info):
    return exc_info.value.detail


@pytest.mark.parametrize("role", _ATTENDANCE_ROLES)
def test_browser_clock_in_blocked_for_every_role(db, plain_org, role):
    user = _mk_user(db, plain_org, role)
    _clear_sessions(db, user.id)
    with pytest.raises(HTTPException) as ei:
        clock_in(db=db, user=user, request=BROWSER)
    assert ei.value.status_code == 403
    assert _detail(ei)["code"] == DESKTOP_APP_REQUIRED
    # Nothing was written — the guard runs before the session tables are touched.
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id
    ).count() == 0


@pytest.mark.parametrize("role", _ATTENDANCE_ROLES)
def test_browser_clock_out_blocked_for_every_role(db, plain_org, role):
    from app.employees.router import clock_out
    from fastapi import BackgroundTasks

    user = _mk_user(db, plain_org, role)
    _clear_sessions(db, user.id)
    clock_in(db=db, user=user, request=DESKTOP_HEADER)
    with pytest.raises(HTTPException) as ei:
        clock_out(background_tasks=BackgroundTasks(), db=db, user=user,
                  body=None, request=BROWSER)
    assert ei.value.status_code == 403
    assert _detail(ei)["code"] == DESKTOP_APP_REQUIRED
    # The session is still OPEN — a blocked call must not close anything.
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id,
        models.ClockSession.clock_out_time == None,  # noqa: E711
    ).count() == 1


@pytest.mark.parametrize("role", ["employee", "admin", "hr", "manager"])
def test_browser_break_endpoints_blocked_for_every_role(db, plain_org, role):
    from app.employees.router import start_break, resume_work
    from app.breaks.router import start_break as breaks_start, resume_from_break

    user = _mk_user(db, plain_org, role)
    for call in (
        lambda: start_break(body=None, request=BROWSER, db=db, user=user),
        lambda: resume_work(request=BROWSER, db=db, user=user),
        lambda: breaks_start(body=None, request=BROWSER, db=db, user=user),
    ):
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 403
        assert _detail(ei)["code"] == DESKTOP_APP_REQUIRED
    # /break/resume takes a required body; assert it the same way.
    from app.breaks.router import ResumePayload
    with pytest.raises(HTTPException) as ei:
        resume_from_break(body=ResumePayload(reason="back at my desk"),
                          request=BROWSER, db=db, user=user)
    assert ei.value.status_code == 403


@pytest.mark.parametrize("role", _ATTENDANCE_ROLES)
@pytest.mark.parametrize("desktop_req", ["header", "ua"])
def test_desktop_app_still_works_for_every_role(db, plain_org, role, desktop_req):
    """The desktop app is untouched — both identification paths keep working."""
    from app.employees.router import clock_out
    from fastapi import BackgroundTasks

    req = DESKTOP_HEADER if desktop_req == "header" else DESKTOP_UA
    user = _mk_user(db, plain_org, role)
    _clear_sessions(db, user.id)

    assert clock_in(db=db, user=user, request=req).get("success") is not False
    assert start_break_ok(db, user, req)
    assert resume_work_ok(db, user, req)
    clock_out(background_tasks=BackgroundTasks(), db=db, user=user,
              body=None, request=req)
    assert db.query(models.ClockSession).filter(
        models.ClockSession.user_id == user.id,
        models.ClockSession.clock_out_time == None,  # noqa: E711
    ).count() == 0


def start_break_ok(db, user, req):
    from app.employees.router import start_break
    res = start_break(body=None, request=req, db=db, user=user)
    return res.get("allowed") is not False and res.get("success") is not False


def resume_work_ok(db, user, req):
    from app.employees.router import resume_work
    return resume_work(request=req, db=db, user=user) is not None


def test_internal_callers_are_not_blocked(db, plain_org):
    """`request=None` means the call did not arrive over HTTP (scheduled job,
    another service function, a direct call). Those must never be blocked —
    only real network traffic is classified."""
    from app.employees.router import clock_out
    from fastapi import BackgroundTasks

    user = _mk_user(db, plain_org, "employee")
    _clear_sessions(db, user.id)
    assert clock_in(db=db, user=user).get("success") is not False
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=None)
    assert res.get("success") is True


def test_rule_is_tenant_agnostic(db, plain_org, other_org, restricted_org):
    """Same answer for a fresh org, an org with unrelated settings, and an org
    that configured the older per-org policy — no tenant-specific branch."""
    for oid in (plain_org, other_org, restricted_org):
        user = _mk_user(db, oid, "admin")
        _clear_sessions(db, user.id)
        with pytest.raises(HTTPException) as ei:
            clock_in(db=db, user=user, request=BROWSER)
        assert ei.value.status_code == 403


def test_flag_off_restores_website_attendance(db, plain_org, monkeypatch):
    """The rule is one constant — flipping it back restores the old behaviour
    with no other change. Nothing was deleted."""
    monkeypatch.setattr(policy, "WEB_ATTENDANCE_DISABLED", False)
    user = _mk_user(db, plain_org, "admin")
    _clear_sessions(db, user.id)
    assert clock_in(db=db, user=user, request=BROWSER).get("success") is not False
