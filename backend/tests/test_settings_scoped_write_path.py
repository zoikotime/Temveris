"""Organization Settings write path — end to end over HTTP.

Why this file exists
--------------------
The scoped-settings API had never successfully written a row in production:
0 ``schedule.*`` settings, 0 ``ScopedSet`` audit entries, ever. The admin UI
rendered, read and inherited correctly, but every Save returned 422 before the
handler ran.

Cause: ``ScopedPayload.team`` / ``user_id`` were annotated ``str = None`` /
``int = None``. Under Pydantic 2 that is a REQUIRED string/int with a default
used only when the key is ABSENT — an explicit ``null`` is a validation error.
``scopedBody()`` in ``OrganizationSettingsTab.jsx`` always sends both keys,
explicitly null for the scopes they do not apply to, so:

    org      -> team=null AND user_id=null  -> 422 (2 errors)
    team     -> user_id=null                -> 422 (1 error)
    employee -> team=null                   -> 422 (1 error)

Why the existing tests missed it: they called the endpoint with the
inapplicable keys OMITTED, which is the one shape that validates. Nothing
exercised the payload the browser actually sends.

So every test here sends the EXACT frontend body shape, explicit nulls
included. The point is not to re-test the cascade — ``test_settings_tier_
isolation.py`` covers that — it is to prove the request the UI sends is
accepted and persisted.
"""
from __future__ import annotations

import itertools
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import models
from database.database import Base, get_db

_org_seq = itertools.count(61000)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    db.commit()
    return oid


def _mk_user(db, org_id, role="employee", team=None, **kw):
    u = models.User(name="WP", email=f"wp_{uuid.uuid4().hex}@x.com",
                    role=role, is_active=True, organization_id=org_id,
                    team=team, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def api(db):
    """The real settings router, with auth and the DB session overridden.

    A minimal app rather than importing `main` so the test does not drag in
    schedulers, Stripe or websockets — the router under test is the same object
    production mounts.
    """
    from app.settings.router import router as settings_router
    from app.auth.service import get_current_user

    org_id = _mk_org(db)
    admin = _mk_user(db, org_id, role="admin")

    app = FastAPI()
    # No prefix here — the router already declares prefix="/settings", exactly
    # as main.py mounts it. Adding one would serve /settings/settings/... and
    # test a path production does not have.
    app.include_router(settings_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin

    with TestClient(app) as c:
        yield c, db, org_id, admin


def scoped_body(scope, *, team=None, user_id=None, **extra):
    """Byte-for-byte what OrganizationSettingsTab.jsx scopedBody() produces."""
    return {
        "scope": scope,
        "team": team if scope == "team" else None,
        "user_id": int(user_id) if scope == "employee" else None,
        **extra,
    }


KEY = "schedule.grace_minutes"


# ── the three scopes, with the payload the browser actually sends ───────────

def test_org_scope_save(api):
    c, db, org_id, admin = api
    r = c.put("/settings/scoped", json=scoped_body("org", **{"settings": {KEY: "25"}}))
    assert r.status_code == 200, r.text

    row = db.query(models.AppSettings).filter_by(
        organization_id=org_id, key=KEY, team_key=None, user_id=None).one()
    assert row.value == "25"


def test_team_scope_save(api):
    c, db, org_id, admin = api
    _mk_user(db, org_id, team="Engineering")
    r = c.put("/settings/scoped",
              json=scoped_body("team", team="Engineering", **{"settings": {KEY: "30"}}))
    assert r.status_code == 200, r.text

    row = db.query(models.AppSettings).filter_by(
        organization_id=org_id, key=KEY, team_key="Engineering").one()
    assert row.value == "30"


def test_employee_scope_save(api):
    c, db, org_id, admin = api
    emp = _mk_user(db, org_id)
    r = c.put("/settings/scoped",
              json=scoped_body("employee", user_id=emp.id, **{"settings": {KEY: "35"}}))
    assert r.status_code == 200, r.text

    row = db.query(models.AppSettings).filter_by(
        organization_id=org_id, key=KEY, user_id=emp.id).one()
    assert row.value == "35"


def test_scoped_reset_accepts_the_same_shape(api):
    """"Reset to inherited" sends the identical body and was 422ing too."""
    c, db, org_id, admin = api
    emp = _mk_user(db, org_id)
    assert c.put("/settings/scoped", json=scoped_body(
        "employee", user_id=emp.id, **{"settings": {KEY: "35"}})).status_code == 200

    r = c.request("DELETE", "/settings/scoped",
                  json=scoped_body("employee", user_id=emp.id, **{"keys": [KEY]}))
    assert r.status_code == 200, r.text
    assert db.query(models.AppSettings).filter_by(
        organization_id=org_id, key=KEY, user_id=emp.id).count() == 0


# ── the regression itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("scope,team,user_id", [
    ("org", None, None), ("team", "Engineering", None), ("employee", None, 1),
])
def test_explicit_nulls_are_accepted(api, scope, team, user_id):
    """The exact failure: explicit nulls must not be a validation error.

    Guards the Pydantic 2 nullability rule directly, so reverting `Optional`
    fails here loudly rather than silently breaking every Save again.
    """
    c, db, org_id, admin = api
    if scope == "employee":
        user_id = _mk_user(db, org_id).id
    if scope == "team":
        _mk_user(db, org_id, team="Engineering")
    r = c.put("/settings/scoped", json=scoped_body(
        scope, team=team, user_id=user_id, **{"settings": {KEY: "20"}}))
    assert r.status_code != 422, f"explicit nulls rejected again: {r.text}"
    assert r.status_code == 200, r.text


def test_audit_row_is_written(api):
    """0 ScopedSet entries ever was the tell that no save had ever landed."""
    c, db, org_id, admin = api
    emp = _mk_user(db, org_id)
    assert c.put("/settings/scoped", json=scoped_body(
        "employee", user_id=emp.id, **{"settings": {KEY: "40"}})).status_code == 200

    audit = db.query(models.AuditLog).filter_by(
        organization_id=org_id, entity="Settings", action="ScopedSet").all()
    assert len(audit) == 1
    assert audit[0].target_id == emp.id
    assert KEY in (audit[0].payload or {}).get("keys", [])


# ── persistence, inheritance, and the resolver ──────────────────────────────

def test_value_persists_and_inheritance_still_works(api):
    """Employee override wins; a sibling with no override inherits the org."""
    c, db, org_id, admin = api
    emp = _mk_user(db, org_id)
    other = _mk_user(db, org_id)

    assert c.put("/settings/scoped", json=scoped_body(
        "org", **{"settings": {KEY: "10"}})).status_code == 200
    assert c.put("/settings/scoped", json=scoped_body(
        "employee", user_id=emp.id, **{"settings": {KEY: "45"}})).status_code == 200

    # Re-read through the endpoint the page calls on reload.
    view = c.get(f"/settings/scoped?scope=employee&user_id={emp.id}")
    assert view.status_code == 200, view.text

    from app.settings import scope as settings_scope
    from app.settings.router import DEFAULTS
    assert settings_scope.resolve_effective(db, emp, DEFAULTS)[KEY] == "45"
    assert settings_scope.resolve_effective(db, other, DEFAULTS)[KEY] == "10"


def test_employment_type_update_reaches_the_employee_record(db):
    """Approach A: at Employee scope, Employment type is NOT a settings row.

    `save()` diverts it to PUT /employees/{id}, so the write path is only
    proven once both halves are. Mounted separately because it lives on a
    different router.
    """
    from app.employees.router import router as employees_router
    from app.auth.service import get_current_user

    org_id = _mk_org(db)
    admin = _mk_user(db, org_id, role="admin")
    emp = _mk_user(db, org_id, employment_type="full_time")

    app = FastAPI()
    app.include_router(employees_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin

    with TestClient(app) as c:
        r = c.put(f"/employees/{emp.id}", json={"employment_type": "part_time"})
        assert r.status_code == 200, r.text

    db.refresh(emp)
    assert emp.employment_type == "part_time"

    # And the resolver must pick it up — Approach A says the employee record is
    # authoritative for an individual, ahead of any org/team cascade value.
    from datetime import date
    from app.schedule_policy.resolver import resolve_work_schedule
    sched = resolve_work_schedule(db, emp, date(2026, 8, 6))
    assert sched.employment_type == "part_time"
    assert sched.sources["employment_type"] == "employee"


def test_resolver_reads_the_saved_value(api):
    """The whole point of the write path: the resolver must see it.

    Grace is the cleanest probe — it has no legacy-column fallback unless
    `default_clock_in_time` is set, so a saved value is the only source.
    """
    c, db, org_id, admin = api
    emp = _mk_user(db, org_id)
    assert c.put("/settings/scoped", json=scoped_body(
        "employee", user_id=emp.id, **{"settings": {KEY: "50"}})).status_code == 200

    from datetime import date
    from app.schedule_policy.resolver import resolve_work_schedule
    sched = resolve_work_schedule(db, emp, date(2026, 8, 6))
    assert sched.grace_minutes == 50
    assert sched.sources["grace_minutes"] == "employee"
