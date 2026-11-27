"""`GET /employees?active_only=true` — the selectable-employee roster.

Bug this locks down
-------------------
Organization Settings → Employee kept offering accounts that had been removed
from the workspace. The dropdown reads `GET /employees`, whose only filter was
`organization_id`, so every deactivated row came back as a pickable settings
target.

`User.is_active` is this codebase's single canonical "no longer a valid user"
state: there is no soft-delete column on `users` and no membership table, the
offboarding access-revocation engine flips exactly this flag
(`app/offboarding/services.py::_revoke_internal`), and the auth gate in
`app/auth/service.py::get_current_user` rejects on it. So "selectable" means
what the canonical roster query in `get_team_status` already means:
org + `is_active` + no non-working seat.

The unfiltered default is asserted here too, because two surfaces depend on it:
Workers & Users renders an Inactive badge and an is_active toggle, and the
offboarding directory has to resolve a FORMER worker's id back to a name. That
is why the filter is opt-in rather than applied to every caller.

Style follows tests/test_null_org_isolation.py: a dedicated in-memory engine and
direct calls into the router function, so no auth/HTTP plumbing is involved.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db          # noqa: E402
from database import models                   # noqa: E402
from app.employees.router import get_employees  # noqa: E402

_org_seq = itertools.count(7100)


@pytest.fixture(scope="module")
def eng():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = e
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=e)
    for t in (models.Organization, models.User, models.OrgSettings, models.AppSettings):
        t.__table__.create(e, checkfirst=True)
    try:
        yield e
    finally:
        _db.engine, _db.SessionLocal = prev_engine, prev_session


@pytest.fixture
def db(eng):
    s = _db.SessionLocal()
    yield s
    s.close()


def _org() -> int:
    return next(_org_seq)


def _user(db, org_id, name, *, is_active=True, role="employee",
          is_password_set=True, is_invited=False, team=None):
    u = models.User(
        name=name,
        email=f"{name.lower().replace(' ', '.')}.{next(_org_seq)}@example.com",
        role=role,
        organization_id=org_id,
        is_active=is_active,
        is_invited=is_invited,
        is_password_set=is_password_set,
        team=team,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _picker(db, caller):
    """What the Employee dropdown asks for."""
    return get_employees(skip=0, limit=None, active_only=True,
                         db=db, current_user=caller)


def _names(rows):
    return {r["name"] for r in rows}


# ── The dropdown's contract ──────────────────────────────────────────────────

def test_active_employee_is_selectable(db):
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Active Worker")

    assert "Active Worker" in _names(_picker(db, admin))


def test_deactivated_employee_is_not_selectable(db):
    """The reported bug: accounts removed from the workspace kept appearing."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Kept Worker")
    removed = _user(db, org, "Removed Worker", is_active=False)

    names = _names(_picker(db, admin))
    assert "Removed Worker" not in names
    assert "Kept Worker" in names
    # Nothing was written to make that true — the row is untouched.
    db.refresh(removed)
    assert removed.is_active is False


def test_deactivated_pending_invite_is_not_selectable(db):
    """Revocation wins over invite state, whatever stage the account is at."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Revoked Invitee", is_active=False,
          is_invited=True, is_password_set=False)

    assert "Revoked Invitee" not in _names(_picker(db, admin))


def test_active_pending_invite_stays_selectable(db):
    """An invited-but-not-yet-activated worker is still a valid member, and the
    canonical roster query counts them — an admin can pre-configure their
    schedule before day one. Only revocation makes someone unselectable."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "New Hire", is_invited=True, is_password_set=False)

    assert "New Hire" in _names(_picker(db, admin))


def test_super_admin_is_not_an_employee(db):
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Platform Support", role="super_admin")

    assert "Platform Support" not in _names(_picker(db, admin))


def test_non_working_seat_is_not_selectable(db):
    """The seeded owner/CEO seat is excluded by the org's
    `attendance.non_working_account_names` setting, same as everywhere else."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Administrator", role="admin")

    assert "Administrator" not in _names(_picker(db, admin))


def test_other_organizations_employees_never_appear(db):
    org_a, org_b = _org(), _org()
    admin_a = _user(db, org_a, "A Admin", role="admin")
    _user(db, org_a, "A Worker")
    _user(db, org_b, "B Worker")
    _user(db, org_b, "B Removed", is_active=False)

    names = _names(_picker(db, admin_a))
    assert names == {"A Admin", "A Worker"}


def test_empty_organization_returns_no_employees(db):
    org = _org()
    admin = _user(db, _org(), "Elsewhere Admin", role="admin")
    admin.organization_id = org          # an org with only this caller in it
    db.commit()

    # Only the caller is in it; nothing else leaks in from other orgs.
    assert _names(_picker(db, admin)) == {"Elsewhere Admin"}


def test_org_less_caller_gets_nothing_rather_than_every_org_less_row(db):
    """`organization_id == None` compiles to `IS NULL`, which would hand back
    every org-less row (platform super-admins) instead of matching nothing."""
    platform = _user(db, None, "Platform Root", role="super_admin")
    _user(db, None, "Other Org Less", role="super_admin")

    assert _picker(db, platform) == []


# ── The unfiltered default must not change ───────────────────────────────────

def test_default_response_still_includes_deactivated_rows(db):
    """Workers & Users and the offboarding directory read the plain list and
    need former workers in it. Filtering by default would break both."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    _user(db, org, "Still Here")
    _user(db, org, "Gone", is_active=False)

    rows = get_employees(skip=0, limit=None, active_only=False,
                         db=db, current_user=admin)
    names = _names(rows)
    assert {"Still Here", "Gone"} <= names
    gone = next(r for r in rows if r["name"] == "Gone")
    assert gone["is_active"] is False
    assert gone["onboarding_status"] == "inactive"


def test_filtering_writes_nothing(db):
    """A read endpoint must not mutate lifecycle state to produce its answer."""
    org = _org()
    admin = _user(db, org, "Org Admin", role="admin")
    rows = [_user(db, org, "W1"), _user(db, org, "W2", is_active=False)]
    before = {u.id: (u.name, u.is_active, u.role, u.organization_id) for u in rows}

    _picker(db, admin)
    get_employees(skip=0, limit=None, active_only=False, db=db, current_user=admin)

    for u in rows:
        db.refresh(u)
        assert before[u.id] == (u.name, u.is_active, u.role, u.organization_id)
