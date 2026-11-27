"""Who the attendance alert paths are allowed to email about.

The production bug these tests pin (reported 2026-09-02): a workspace seat
displayed as "Super Admin" received an "Absent Alert – Super Admin" email every
working day. Two guards should have caught it and neither did.

  * ``exclude_system_admin`` matches on DISPLAY NAME, against the
    per-organization ``attendance.non_working_account_names`` setting whose
    system default is the single string ``"administrator"``. A seat named
    "Super Admin" is not that string.
  * The alert paths carried their own ROLE list, ``{"admin", "hr"}`` — written
    once in ``attendance_alerts`` and re-typed as a literal ``("admin", "hr")``
    tuple twice more in ``employees/router``. Meanwhile ``ROLE_CATALOG``
    declares TEN system roles, so eight of them were treated as clock-in-tracked
    workers who had failed to turn up.

Three copies of one policy is why it drifted, so the set now lives in exactly
one place and these tests assert both halves: the governance seats are silent,
and — the half that is far easier to get wrong — every real worker role is
STILL alerted. Silently withdrawing a legitimate absence alert is the same
defect pointed the other way, and nobody reports an email that never arrived.
"""
import itertools
import uuid

import pytest

from database import models
from database.database import Base

from app._system_admin import (
    NON_WORKING_ROLES,
    is_attendance_tracked,
    is_non_working_role,
)

# ids must not collide because organizations.id is explicit in these fixtures
# and the engine is session-scoped. 68000 is test_timezone_hardening, 70000 is
# test_schedule_prefetch, 77000 is test_multi_tenant_policy — 74000 is the gap.
_org_seq = itertools.count(74000)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _new_org(db) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    db.commit()
    return oid


def _mk_user(db, org_id, role="employee", name="W"):
    u = models.User(name=name, email=f"w_{uuid.uuid4().hex}@x.com",
                    role=role, is_active=True, organization_id=org_id)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── The reported seat ────────────────────────────────────────────────────────

def test_a_super_admin_seat_inside_a_tenant_is_never_alerted(db):
    """THE REPORTED BUG. A properly provisioned platform super-admin escapes the
    sweep because ``auth.service`` nulls their ``organization_id`` on login and
    ``_users_by_org`` skips org-less rows. This is the other case: a row that
    still carries a tenant org_id, which ``employees/router.py:653`` already
    documents as existing in production."""
    from app import attendance_alerts

    org = _new_org(db)
    seat = _mk_user(db, org, role="super_admin", name="Super Admin")
    worker = _mk_user(db, org, role="employee")

    population = attendance_alerts._users_by_org(db).get(org, [])
    ids = {u.id for u in population}
    assert seat.id not in ids
    assert worker.id in ids


def test_the_display_name_guard_alone_does_not_catch_it(db):
    """Why the role check was needed at all. The name list defaults to
    "administrator", so the name-based guard passes this seat straight through —
    which is exactly how it reached the mail queue."""
    from app._system_admin import is_system_admin

    org = _new_org(db)
    seat = _mk_user(db, org, role="super_admin", name="Super Admin")
    assert is_system_admin(seat, db) is False       # name guard: not caught
    assert is_non_working_role(seat) is True        # role guard: caught
    assert is_attendance_tracked(seat, db) is False


@pytest.mark.parametrize("role", sorted(NON_WORKING_ROLES))
def test_no_governance_seat_is_alerted(db, role):
    from app import attendance_alerts

    org = _new_org(db)
    seat = _mk_user(db, org, role=role)
    ids = {u.id for u in attendance_alerts._users_by_org(db).get(org, [])}
    assert seat.id not in ids, role


# ── The half that is easy to break: real workers must stay alerted ───────────

@pytest.mark.parametrize("role", [
    "employee",           # ROLE_CATALOG worker, and the model default
    "manager",            # what the Add Employee form actually writes
    "people_manager",     # the only other ROLE_CATALOG role with self.* perms
    "user", "worker",     # legacy synonyms in commercial/projection.py
    "shift_supervisor",   # a tenant-minted custom role slug
    "",                   # never set
])
def test_every_worker_role_is_still_alerted(db, role):
    """The DENYLIST property. ``User.role`` is a free String(50) with no enum and
    no CHECK constraint, so unrecognised values genuinely exist in production
    (``workspace/router.py:318`` surfaces them as pseudo-custom role cards).
    An allowlist of worker roles would silently untrack every one of these."""
    from app import attendance_alerts

    org = _new_org(db)
    worker = _mk_user(db, org, role=role)
    ids = {u.id for u in attendance_alerts._users_by_org(db).get(org, [])}
    assert worker.id in ids, role or "<empty>"


def test_a_role_none_row_is_still_alerted(db):
    """`role` is nullable in practice; None must not read as a governance seat."""
    from app import attendance_alerts

    org = _new_org(db)
    worker = _mk_user(db, org)
    worker.role = None
    db.commit()
    ids = {u.id for u in attendance_alerts._users_by_org(db).get(org, [])}
    assert worker.id in ids


def test_people_manager_is_not_in_the_non_working_set():
    """Stated explicitly because it is the trap. ``people_manager`` sounds like
    a governance role but ROLE_CATALOG gives it the
    self.profile.edit / self.timesheet.view / self.leave.submit triple, i.e. it
    owns a timesheet. Same for ``manager``, which the Add Employee form writes
    and which is not in the catalogue at all."""
    for role in ("employee", "people_manager", "manager", "user", "worker"):
        assert role not in NON_WORKING_ROLES, role


# ── Robustness of the predicate ──────────────────────────────────────────────

@pytest.mark.parametrize("stored", [
    "SUPER_ADMIN", "Super_Admin", "  super_admin  ", "Owner", "HR", "Admin",
])
def test_the_role_test_is_case_and_whitespace_insensitive(db, stored):
    """``User.role`` has no database constraint. The Access Governance Center
    validates case-sensitively on write, but nothing stops a script or a
    migration writing a display-cased value."""
    org = _new_org(db)
    seat = _mk_user(db, org, role=stored)
    assert is_non_working_role(seat) is True, stored


def test_the_predicate_tolerates_none():
    assert is_non_working_role(None) is False
    assert is_attendance_tracked(None) is False


# ── One policy, one definition ───────────────────────────────────────────────

def test_the_alert_paths_share_one_role_set():
    """The anti-drift guard. This bug existed because the same policy was
    written in three places and only one of them was ever updated. Any
    re-typed literal role tuple in an alert filter is the shape of the bug.

    Scoped to the two functions that decide who gets emailed, NOT to whole
    modules: ``("admin", "hr")`` is also a legitimate AUTHORIZATION literal
    elsewhere in employees/router (e.g. "Only admin or HR can invite
    employees"), and those must keep saying exactly what they say. Who may call
    an endpoint and who owns a timesheet are different questions that happen to
    have shared two role names.
    """
    import inspect

    from app import attendance_alerts
    from app.employees import router as employees_router

    assert attendance_alerts._SKIP_ROLES is NON_WORKING_ROLES

    targets = {
        "attendance_alerts._users_by_org":
            inspect.getsource(attendance_alerts._users_by_org),
        "employees.router.team_status":
            inspect.getsource(employees_router.team_status),
    }
    for where, src in targets.items():
        # EXECUTABLE lines only. The comments in both functions quote the old
        # literal on purpose, to record what the bug looked like.
        code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        for literal in ('("admin", "hr")', '{"admin", "hr"}',
                        "('admin', 'hr')", "{'admin', 'hr'}"):
            offenders = [ln.strip() for ln in code if literal in ln]
            assert not offenders, f"{where}: {literal} -> {offenders}"
        # and each must actually route through the shared predicate
        assert any("is_non_working_role" in ln for ln in code), where


def test_the_governance_set_covers_every_catalogue_role_without_a_timesheet():
    """Membership is DERIVED from ROLE_CATALOG rather than guessed: a role that
    grants ``self.timesheet.view`` ("View own clock sessions and timesheets")
    owns a timesheet and must stay tracked; every other system role does not.
    If a new system role is added to the catalogue this test fails until
    somebody classifies it, which is the intended prompt."""
    from app.workspace.router import ROLE_CATALOG

    for role in ROLE_CATALOG:
        has_timesheet = "self.timesheet.view" in role["permissions"]
        in_set = role["code"] in NON_WORKING_ROLES
        assert in_set is not has_timesheet, (
            f"{role['code']}: timesheet={has_timesheet} non_working={in_set}"
        )


# ── End-to-end: the real sweep, the reported scenario ────────────────────────
#
# Everything above tests the population and the predicates. This drives the
# actual function the 5-minute monitor runs (`attendance_alerts._scan_org`) and
# asserts on the emails it would send, because that is the artefact the user
# actually received. Both halves of the 2026-09-02 incident in one scenario:
# a seat named "Super Admin" and a worker whose shift starts at 13:00.

@pytest.fixture
def sweep(monkeypatch):
    """Run `_scan_org` at a chosen org-local wall clock, capturing sends."""
    from datetime import date as _date, datetime as _datetime, time as _time
    from datetime import timedelta as _timedelta, timezone as _timezone

    from app import _tz, attendance_alerts

    IST = _timezone(_timedelta(hours=5, minutes=30))
    WORKDAY = _date(2026, 8, 6)          # a Thursday

    sent = {"absent": [], "late": []}

    def _run(db, org_id, users, at_hour, at_minute=0):
        now_local = _datetime.combine(WORKDAY, _time(at_hour, at_minute), tzinfo=IST)
        monkeypatch.setattr(_tz, "local_now", lambda *a, **k: now_local)
        monkeypatch.setattr(_tz, "org_timezone_name", lambda *a, **k: "Asia/Kolkata")
        monkeypatch.setattr(_tz, "local_day_bounds", lambda *a, **k: (
            _datetime.combine(WORKDAY, _time(0, 0), tzinfo=IST),
            _datetime.combine(WORKDAY, _time(0, 0), tzinfo=IST) + _timedelta(days=1),
        ))
        # Never touch a mail provider from a test, and record the audience.
        monkeypatch.setattr(attendance_alerts, "alert_recipients",
                            lambda *a, **k: ["hr@example.com"])
        monkeypatch.setattr(attendance_alerts, "send_absent_alert_to_admin_hr",
                            lambda **kw: sent["absent"].append(kw["employee_name"]))
        monkeypatch.setattr(attendance_alerts, "send_late_arrival_alert_to_admin_hr",
                            lambda **kw: sent["late"].append(kw["employee_name"]))
        # `_scan_org` resolves schedules for WORKDAY via `now_local.date()`.
        attendance_alerts._scan_org(db, org_id, users)
        return sent

    _run.sent = sent
    return _run


def _configure_shift(db, org_id, start, user_id=None):
    from app.schedule_policy import keys as K
    db.add(models.AppSettings(organization_id=org_id, user_id=user_id,
                              key=K.SETTING_SHIFT_START_TIME, value=start))
    db.commit()


def test_the_reported_scenario_produces_neither_email(db, sweep):
    """THE ACCEPTANCE TEST for 2026-09-02. At 13:00 org-local — the minute the
    1 PM worker's shift begins, and an hour after the old noon cutoff — neither
    the "Super Admin" seat nor the 1 PM worker may be emailed about."""
    org = _new_org(db)
    seat = _mk_user(db, org, role="super_admin", name="Super Admin")
    mukul = _mk_user(db, org, role="employee", name="Mukul")
    _configure_shift(db, org, "13:00", user_id=mukul.id)

    sent = sweep(db, org, [seat, mukul], 13, 0)

    assert sent["absent"] == []
    assert sent["late"] == []


def test_the_one_pm_worker_is_still_alerted_when_genuinely_absent(db, sweep):
    """The other half: the fix must not make absence unreportable. Two hours
    after their shift start, with no clock-in and no leave, the same worker IS
    emailed about — the alert is delayed to a defensible time, not removed."""
    org = _new_org(db)
    mukul = _mk_user(db, org, role="employee", name="Mukul")
    _configure_shift(db, org, "13:00", user_id=mukul.id)

    sent = sweep(db, org, [mukul], 15, 0)

    assert sent["absent"] == ["Mukul"]


def test_a_governance_seat_is_never_alerted_however_late_it_gets(db, sweep):
    """The seat must be silent at every hour, not merely at 13:00."""
    org = _new_org(db)
    seat = _mk_user(db, org, role="super_admin", name="Super Admin")
    _configure_shift(db, org, "09:00", user_id=seat.id)

    sent = sweep(db, org, [seat], 23, 0)

    assert sent["absent"] == []
    assert sent["late"] == []
