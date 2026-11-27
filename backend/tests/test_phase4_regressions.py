"""
PHASE 4 — SECURITY HARDENING REGRESSION TESTS.

Unlike the Phase-3 files, this one is PERMANENT: every production fix made in
Phase 4 is pinned here. Each test names the defect it guards against, so a
future change that reintroduces it fails with an explanation rather than a
bare assertion.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app import _tz
from app.schedule_policy import keys as K
from app.schedule_policy.resolver import resolve_work_schedule

_org_seq = itertools.count(940000)

MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
MONDAY = date(2026, 8, 3)


@pytest.fixture(scope="session", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db, tz_name="Asia/Kolkata", **rows) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"P4Org{oid}"))
    db.add(models.OrgSettings(organization_id=oid, default_timezone=tz_name))
    for k, v in rows.items():
        db.add(models.AppSettings(organization_id=oid, key=k, value=v))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="P4", email=f"p4_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    q = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == key)
    q = q.filter(models.AppSettings.team_key.is_(None)) if team_key is None \
        else q.filter(models.AppSettings.team_key == team_key)
    q = q.filter(models.AppSettings.user_id.is_(None)) if user_id is None \
        else q.filter(models.AppSettings.user_id == user_id)
    row = q.first()
    if row is not None:
        row.value = value
    else:
        db.add(models.AppSettings(organization_id=org_id, key=key, value=value,
                                  team_key=team_key, user_id=user_id))
    db.commit()
    _tz.invalidate_tz_cache(org_id)


# ══════════════════════════════════════════════════════════════════════════
# STEP 3 — WEBSOCKET TENANT ISOLATION, AT THE DELIVERY LAYER
# ══════════════════════════════════════════════════════════════════════════

class _FakeWS:
    """Records what actually arrived on the wire for one connection."""

    def __init__(self, label):
        self.label = label
        self.received = []

    async def accept(self):
        return None

    async def send_text(self, message):
        self.received.append(message)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def rooms():
    """A real ConnectionManager with one socket per tenant."""
    from websocket_manager import ConnectionManager
    mgr = ConnectionManager()
    a, b = _FakeWS("A"), _FakeWS("B")
    _run(mgr.connect(a, 101))
    _run(mgr.connect(b, 202))
    return mgr, a, b


def test_org_a_event_reaches_only_org_a(rooms):
    """Phase-3 P0. The delivery layer itself, not a spy: A's payload must land
    on A's socket and on no other."""
    mgr, a, b = rooms
    payload = json.dumps({"type": "requires_attention", "user_name": "Priya"})
    _run(mgr.broadcast_to_org(101, payload))
    assert a.received == [payload]
    assert b.received == [], "org A's workforce event reached org B's socket"


def test_org_b_event_reaches_only_org_b(rooms):
    mgr, a, b = rooms
    payload = json.dumps({"type": "break_overtime", "user_name": "Rahul"})
    _run(mgr.broadcast_to_org(202, payload))
    assert b.received == [payload]
    assert a.received == [], "org B's workforce event reached org A's socket"


def test_the_global_broadcast_refuses_business_payloads(rooms):
    """STRUCTURAL guard added in Phase 4: even a caller that reaches for the
    global method cannot leak — the manager drops the frame."""
    mgr, a, b = rooms
    _run(mgr.broadcast(json.dumps({
        "type": "requires_attention", "user_name": "Priya",
        "department": "Finance"})))
    assert a.received == [] and b.received == [], (
        "a business payload was delivered through the GLOBAL broadcast")


def test_the_global_broadcast_still_delivers_control_frames(rooms):
    """The safeguard must not break ping/pong — that is what the method is for."""
    mgr, a, b = rooms
    _run(mgr.broadcast('{"type":"ping"}'))
    assert a.received == ['{"type":"ping"}']
    assert b.received == ['{"type":"ping"}']


def test_an_unauthenticated_socket_never_receives_business_data(rooms):
    """`broadcast()` fans out to the unauthenticated pool too, which is how the
    Phase-3 leak also reached clients with no tenant at all."""
    from websocket_manager import ConnectionManager
    mgr = ConnectionManager()
    anon = _FakeWS("anon")
    _run(mgr.connect(anon, None))          # no org -> unauthenticated pool
    member = _FakeWS("member")
    _run(mgr.connect(member, 101))

    _run(mgr.broadcast(json.dumps({"type": "time_adjustment_updated",
                                   "reason": "power cut", "added_by": "HR"})))
    assert anon.received == []
    _run(mgr.broadcast_to_org(101, json.dumps({"type": "x"})))
    assert anon.received == [], "an unauthenticated socket received tenant data"
    assert len(member.received) == 1


# ══════════════════════════════════════════════════════════════════════════
# STEP 8 — SYSTEM ACCOUNTS ARE PER-ORGANIZATION
# ══════════════════════════════════════════════════════════════════════════

def test_administrator_is_a_system_seat_in_one_org_and_an_employee_in_another(db):
    """Org A's seeded owner seat is called "Administrator"; Org B has a REAL
    employee of that name. Org B's person must keep their attendance."""
    from app._system_admin import (
        SETTING_NON_WORKING_NAMES, is_system_admin, exclude_system_admin,
    )
    org_a = _mk_org(db)
    org_b = _mk_org(db)
    _set(db, org_b, SETTING_NON_WORKING_NAMES, "")     # B excludes nobody

    a_seat = _mk_user(db, org_a); a_seat.name = "Administrator"
    b_person = _mk_user(db, org_b); b_person.name = "Administrator"
    db.commit()

    assert is_system_admin(a_seat, db) is True
    assert is_system_admin(b_person, db) is False
    assert exclude_system_admin([a_seat], db) == []
    assert exclude_system_admin([b_person], db) == [b_person], (
        "org B's real employee was filtered out because org A calls that name "
        "a system seat")

    # A third org can name its own seat something else entirely.
    org_c = _mk_org(db)
    _set(db, org_c, SETTING_NON_WORKING_NAMES, "Owner Account, Reception")
    c_seat = _mk_user(db, org_c); c_seat.name = "Reception"
    c_emp = _mk_user(db, org_c); c_emp.name = "Administrator"
    db.commit()
    assert is_system_admin(c_seat, db) is True
    assert is_system_admin(c_emp, db) is False


# ══════════════════════════════════════════════════════════════════════════
# STEP 9 — RISK THRESHOLDS ARE ONE POLICY, READ THE SAME WAY EVERYWHERE
# ══════════════════════════════════════════════════════════════════════════

def test_both_risk_surfaces_read_the_same_threshold(db):
    """`/employees/risk-users` honoured attendance.risk_deficit_percent while
    the Intelligence risk list hard-coded 0.75, so the same employee appeared
    at risk on one screen and healthy on the other."""
    from app.attendance_policy import (
        policy_float, SETTING_RISK_DEFICIT_PCT, SETTING_RISK_HIGH_PCT,
    )
    from app.intelligence import risk_engine

    org = _mk_org(db)
    u = _mk_user(db, org)
    _set(db, org, SETTING_RISK_DEFICIT_PCT, "90")
    _set(db, org, SETTING_RISK_HIGH_PCT, "60")

    assert policy_float(db, u, SETTING_RISK_DEFICIT_PCT) == 90.0
    assert risk_engine._deficit_ratio(db, u) == 0.90
    assert risk_engine._high_ratio(db, u) == 0.60

    other = _mk_org(db)
    u2 = _mk_user(db, other)
    assert risk_engine._deficit_ratio(db, u2) == 0.75      # system default
    assert risk_engine._high_ratio(db, u2) == 0.50


def test_expected_hours_for_a_period_counts_working_days_not_calendar_days(db):
    """A Mon-Fri employee measured over 7 calendar days was billed 7 x 8h
    instead of 5 x 8h, so a fully-compliant worker read as 71% utilised."""
    from app.intelligence import risk_engine
    org = _mk_org(db, **{K.SETTING_WORKING_DAYS: json.dumps(MON_FRI),
                         K.SETTING_EXPECTED_WORK_HOURS: "8:00"})
    u = _mk_user(db, org)
    start, end = date(2026, 8, 3), date(2026, 8, 9)       # Mon..Sun
    secs = risk_engine._expected_seconds_over(db, u, start, end)
    assert secs == 5 * 8 * 3600, "calendar days were counted as working days"


# ══════════════════════════════════════════════════════════════════════════
# STEP 12 / 14 — TENANT-LOCAL DATES FOR BUSINESS GATES
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tz_name,expected", [
    ("Pacific/Auckland", date(2026, 8, 4)),    # UTC+12 -> already tomorrow
    ("Asia/Kolkata", date(2026, 8, 4)),        # UTC+5:30 -> just past midnight
    ("America/New_York", date(2026, 8, 3)),    # UTC-4 -> still yesterday
])
def test_leave_gates_use_the_organizations_calendar_day(db, tz_name, expected,
                                                        monkeypatch):
    """Advance notice, new-joiner eligibility and the comp-leave worked-date
    check all hard-block with 4xx, so they must be decided on the tenant's own
    date. At 22:00 UTC three tenants are on two different days."""
    org = _mk_org(db, tz_name=tz_name)
    instant = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr("app._tz.datetime", _FrozenDT)
    assert _tz.local_date(db, org) == expected


# ══════════════════════════════════════════════════════════════════════════
# STEP 22 — API PARAMETER HONESTY
# ══════════════════════════════════════════════════════════════════════════

def test_leave_requests_user_id_is_honoured_not_ignored(db):
    """Phase-3 R7: /leave/requests took a user_id and silently returned the
    whole organization — a filter that looks like it works and does not.

    Driven over HTTP, because the defect was in what the endpoint DID, not in
    what its source contained."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    org_a, org_b = _mk_org(db), _mk_org(db)
    admin = models.User(name="LR-ADMIN", email=f"lr_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=org_a)
    e1 = _mk_user(db, org_a); e1.name = "LR-ONE"
    e2 = _mk_user(db, org_a); e2.name = "LR-TWO"
    outsider = _mk_user(db, org_b); outsider.name = "LR-OUTSIDER"
    db.add(admin); db.commit()
    for u, tag in ((e1, "ONE"), (e2, "TWO"), (outsider, "OUTSIDER")):
        db.add(models.LeaveRequest(
            user_id=u.id, organization_id=u.organization_id,
            leave_type="Sick Leave", from_date=datetime.now(timezone.utc),
            to_date=datetime.now(timezone.utc), status="approved",
            reason=f"LR-REASON-{tag}"))
    db.commit()

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    everything = c.get("/leave/requests", headers=H)
    assert everything.status_code == 200
    assert "LR-REASON-ONE" in everything.text and "LR-REASON-TWO" in everything.text
    assert "LR-REASON-OUTSIDER" not in everything.text, "cross-tenant leak"

    filtered = c.get(f"/leave/requests?user_id={e1.id}", headers=H)
    assert filtered.status_code == 200
    assert "LR-REASON-ONE" in filtered.text
    assert "LR-REASON-TWO" not in filtered.text, (
        "user_id was accepted and ignored — the whole org came back")

    # A foreign id is a 404, never an empty 200 that reads as "no leave".
    foreign = c.get(f"/leave/requests?user_id={outsider.id}", headers=H)
    assert foreign.status_code == 404, foreign.text[:200]


# ══════════════════════════════════════════════════════════════════════════
# STEP 6 — A SCOPE THE CONSUMER CANNOT SEE MUST NOT BE OFFERED
# ══════════════════════════════════════════════════════════════════════════

def test_org_only_keys_are_not_offered_at_team_or_employee_scope(db):
    """Phase-3 P1_BAD_CASCADE. `work_hours_threshold` and the 21 active_time.*
    keys are read at the ORG tier by their consumers, but the admin UI listed
    them for team/employee scope: the write succeeded, was audit-logged and
    rendered as an active override, and changed nothing."""
    from app.settings import scope as settings_scope
    from app.settings.router import DEFAULTS, DESCRIPTIONS

    org = _mk_org(db)
    u = _mk_user(db, org, team="Nights")

    org_only = {k for k, v in settings_scope.KEY_META.items()
                if v.get("org_only") and k in DEFAULTS}
    assert "work_hours_threshold" in org_only
    assert any(k.startswith("active_time.") for k in org_only)

    at_org = {r["key"] for r in settings_scope.scoped_view(
        db, org, DEFAULTS, DESCRIPTIONS, scope="org")}
    at_team = {r["key"] for r in settings_scope.scoped_view(
        db, org, DEFAULTS, DESCRIPTIONS, scope="team", team_key="Nights")}
    at_emp = {r["key"] for r in settings_scope.scoped_view(
        db, org, DEFAULTS, DESCRIPTIONS, scope="employee", user_id=u.id)}

    assert "work_hours_threshold" in at_org, "still editable where it works"
    assert not (org_only & at_team), f"org-only keys offered per team: {org_only & at_team}"
    assert not (org_only & at_emp), f"org-only keys offered per employee: {org_only & at_emp}"
    # Everything else must still be offered at every scope.
    cascading = {k for k in settings_scope.KEY_META if k in DEFAULTS} - org_only
    assert cascading <= at_team and cascading <= at_emp


def test_scoped_write_rejects_an_org_only_key(db):
    """The UI no longer offers it; the API must not accept it either, or a
    direct PUT still writes an inert row."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    org = _mk_org(db)
    admin = models.User(name="A4", email=f"a4_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=org)
    db.add(admin); db.commit(); db.refresh(admin)
    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    r = c.put("/settings/scoped",
              json={"scope": "team", "team": "Nights",
                    "settings": {"work_hours_threshold": "6:00"}}, headers=H)
    assert r.status_code == 400, f"org-only key accepted at team scope: {r.text[:200]}"
    assert "organization-wide" in r.text

    # The same key at org scope still works.
    r = c.put("/settings/scoped",
              json={"scope": "org", "settings": {"work_hours_threshold": "6:00"}},
              headers=H)
    assert r.status_code == 200, r.text[:200]
    # And a cascading key at team scope still works.
    r = c.put("/settings/scoped",
              json={"scope": "team", "team": "Nights",
                    "settings": {"schedule.grace_minutes": "40"}}, headers=H)
    assert r.status_code == 200, r.text[:200]


# ══════════════════════════════════════════════════════════════════════════
# STEP 13 — LEAVE IDENTITY: metadata where it exists, ONE place where it does not
# ══════════════════════════════════════════════════════════════════════════

def test_leave_identity_is_asked_in_exactly_one_place():
    """R1/R2/R3 are product decisions Phase 4 must not guess. What Phase 4 CAN
    do is stop asking the question in nine scattered places, so the eventual
    answer is implemented once. This fails if a literal comparison returns."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    banned = re.compile(
        r'leave_type\s*==\s*"|actual_type\s*==\s*"|'
        r'==\s*"(Annual|Unpaid|Sick|Medical|Compensatory) Leave"')
    offenders = []
    for f in root.rglob("*.py"):
        if f.name == "identity.py":
            continue          # the one module allowed to name them
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if banned.search(code):
                offenders.append(f"{f.relative_to(root)}:{i}")
    assert offenders == [], (
        "leave policy decided by display name outside leave/identity.py: %s"
        % offenders)


def test_identity_behaviour_is_unchanged_today():
    """The refactor must be a NO-OP for existing tenants: same answers as the
    literal comparisons it replaced."""
    from app.leave import identity as ident
    assert ident.is_pool_master("Annual Leave") is True
    assert ident.is_pool_master("PTO") is False
    assert ident.is_unpaid("Unpaid Leave") is True
    assert ident.is_unpaid("Leave Without Pay") is False
    assert ident.is_sick("Sick Leave") is True
    assert ident.is_sick("Medical Leave") is False
    assert ident.pool_master_name({}) == "Annual Leave"
    assert ident.unpaid_name({}) == "Unpaid Leave"


def test_identity_already_honours_a_policy_flag_when_one_exists():
    """The eventual fix is additive: a flag, once writable, wins immediately.
    This pins the contract Phase 5 will implement against."""
    from app.leave import identity as ident
    assert ident.is_pool_master("PTO", {"pool_master": True}) is True
    assert ident.is_unpaid("Leave Without Pay", {"is_unpaid": True}) is True
    assert ident.is_sick("Illness", {"is_sick": True}) is True
    assert ident.pool_master_name({"PTO": {"pool_master": True},
                                   "Annual Leave": {}}) == "PTO"
    assert ident.unpaid_name({"LWP": {"is_unpaid": True}}) == "LWP"


@pytest.mark.xfail(reason="BY DESIGN since Phase 5: a role is EXPLICIT policy, "
                          "never inferred from a name. An unflagged 'PTO' "
                          "resolves to the legacy pool master until an admin "
                          "assigns pool_master (or the backfill runs). Kept "
                          "because legacy production data had 0 of 4 tenants "
                          "migrated at the Phase 6 readiness review.", strict=True)
def test_KNOWN_LIMITATION_renamed_pool_master_is_not_recognised():
    """Pins the CURRENT production reality: a tenant that renamed its vacation
    bucket but has not assigned the role still resolves via the legacy bridge.

    Phase 5 did not "fix" this by guessing — inference from the name, the
    largest entitlement or catalogue order is exactly what produced the
    original defect. The fix is the flag, and
    `test_identity_already_honours_a_policy_flag_when_one_exists` proves the
    flag wins. Delete this xfail once every tenant is backfilled and the
    LEGACY_* bridge is removed."""
    from app.leave import identity as ident
    policies = {"PTO": {"limit": 25, "shared_pool": True}}   # no pool_master flag
    assert ident.pool_master_name(policies) == "PTO"


@pytest.mark.xfail(reason="BY DESIGN since Phase 5 (see above): an unflagged "
                          "'Leave Without Pay' resolves to the legacy unpaid "
                          "type until is_unpaid is assigned. Kept because "
                          "production is 0/4 migrated.", strict=True)
def test_KNOWN_LIMITATION_renamed_unpaid_type_is_not_recognised():
    from app.leave import identity as ident
    assert ident.unpaid_name({"Leave Without Pay": {"limit": 0}}) == "Leave Without Pay"


def test_comp_leave_evidence_uses_the_earned_flag_not_a_name(db):
    """Unlike R1-R3 this needed no decision: `earned` already means "claimed
    against overtime already worked", and Compensatory Leave carries it."""
    from app.leave.router import LEAVE_POLICIES
    assert LEAVE_POLICIES["Compensatory Leave"].get("earned") is True
    import inspect
    from app.leave import router as lr
    src = inspect.getsource(lr)
    assert 'if leave_type == "Compensatory Leave":' not in src


# ══════════════════════════════════════════════════════════════════════════
# STEP 26 — ONE CANONICAL IMPLEMENTATION PER POLICY
# ══════════════════════════════════════════════════════════════════════════

def test_worked_seconds_has_exactly_one_implementation():
    """Every consumer must delegate to app/_worked.py. A module that
    re-derives (clock_out - clock_in - breaks) will drift from it — that is
    how reports came to double-subtract breaks and payroll to ignore offline
    time."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(
        r"clock_out_time\s*-\s*.*clock_in_time|"
        r"total_seconds\s*or\s*0\s*\)\s*-\s*\(.*break_seconds")
    # WRITERS and one deliberate raw-wall-time consumer. A writer PRODUCES
    # total_seconds, so it necessarily does the subtraction once; the fraud
    # check needs raw wall time precisely to compare it against logged work.
    ALLOWED = {
        "employees/router.py": "clock-out / admin-clock WRITE the canonical total",
        "timesheets/time_tracking.py": "a clock-out writer",
        "wec/service.py": "time-inflation check needs RAW wall time by design",
    }
    offenders = []
    for f in root.rglob("*.py"):
        if f.name == "_worked.py":
            continue
        if f.relative_to(root).as_posix() in ALLOWED:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pattern.search(code):
                offenders.append(f"{f.relative_to(root)}:{i}")
    assert offenders == [], (
        "worked-seconds re-implemented outside app/_worked.py: %s" % offenders)


def test_expected_hours_over_a_period_has_one_implementation(db):
    """The three surfaces that answer "what did this employee owe over a
    range" must agree: /employees/risk-users, /reports/workload and the
    Intelligence rollup all route through _expected_seconds_over."""
    from app.intelligence.risk_engine import _expected_seconds_over
    import inspect
    from app.reports import router as rr
    from app.employees import router as er

    assert "_expected_seconds_over" in inspect.getsource(rr.workload_analysis)
    assert "_expected_seconds_over" in inspect.getsource(er.risk_users)

    org = _mk_org(db, **{K.SETTING_WORKING_DAYS: json.dumps(MON_FRI),
                         K.SETTING_EXPECTED_WORK_HOURS: "7:30"})
    u = _mk_user(db, org)
    # Mon..Sun contains five working days for this tenant.
    assert _expected_seconds_over(db, u, date(2026, 8, 3), date(2026, 8, 9)) \
        == 5 * (7 * 3600 + 1800)


def test_local_date_has_one_implementation():
    """`_tz` is the only place an organization's "today" is derived. A module
    computing datetime.now(SOME_FIXED_OFFSET).date() is a second answer."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = re.compile(r"timedelta\(hours=5,\s*minutes=30\)|tz\(td\(hours=5")
    offenders = []
    for f in list((root / "app").rglob("*.py")) + [root / "main.py"]:
        if f.name == "_tz.py":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line.split("#", 1)[0]):
                offenders.append(f"{f.relative_to(root)}:{i}")
    assert offenders == [], f"fixed +05:30 offset outside app/_tz.py: {offenders}"
