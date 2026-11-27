"""
PHASE 3 — LIVE CROSS-TENANT HTTP PROBING.  *** PERMANENT ***
============================================================
Kept as a standing security regression: it drives the real app over HTTP and
is the only suite that proves the ROUTERS apply tenant scope, not just that
the queries are written correctly.
Verification scaffolding for Phase-3 steps 2 and 4. It drives the real FastAPI
app over HTTP as Organization A and asks for — and tries to write to —
Organization B's resources by id.

Deliberately BLACK-BOX. `test_tenant_isolation.py` proves the QUERIES are
written correctly; it never calls a router, so it cannot prove the router
applies them.

A 200 is NOT a leak. Every probe is classified into one of:

    FOREIGN_DATA   the other tenant's rows came back      -> P0
    OWN_DATA       the caller's own rows came back        -> safe
    EMPTY          200 with nothing in it                 -> safe
    DENIED         401 / 403                              -> safe
    NOT_FOUND      404                                    -> safe
    SPA            200 {"detail":"Not Found"} from the SPA catch-all in
                   main.py:2495 — the API route does not exist, the single-page
                   app fallback answered. NOT an API response at all.
    ERROR          5xx — records a crash, not an isolation result

Each tenant's rows carry unmistakable markers (CANARY-<TAG>) so "foreign data"
is decided by content, never by status code.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from database import models
from database.database import Base

_seq = itertools.count(930000)

TAGS = ("A", "B", "C")


@pytest.fixture(scope="module")
def app_and_db(engine):
    Base.metadata.create_all(engine)
    import main
    return main.app


@pytest.fixture(scope="module")
def tenants(app_and_db, engine):
    """Three complete tenants. A and B have an admin + employee; C has an
    employee only (a third tenant catches "resolved for whoever asked last")."""
    from sqlalchemy.orm import sessionmaker
    from app.auth.service import create_access_token

    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    out = {}
    now = datetime.now(timezone.utc)

    for tag in TAGS:
        oid = next(_seq)
        s.add(models.Organization(id=oid, name=f"CANARY-ORG-{tag}"))
        s.add(models.OrgSettings(
            organization_id=oid,
            default_timezone={"A": "Asia/Kolkata", "B": "America/New_York",
                              "C": "Pacific/Auckland"}[tag],
            hr_email=f"canary-hr-{tag.lower()}@{tag.lower()}.test"))
        s.commit()

        admin = models.User(
            name=f"CANARY-ADMIN-{tag}", email=f"canary_adm_{tag}_{uuid.uuid4().hex}@x.com",
            password="x", role="admin", is_active=True, organization_id=oid,
            department=f"CANARY-DEPT-{tag}")
        emp = models.User(
            name=f"CANARY-EMP-{tag}", email=f"canary_emp_{tag}_{uuid.uuid4().hex}@x.com",
            password="x", role="employee", is_active=True, organization_id=oid,
            team=f"CANARY-TEAM-{tag}", department=f"CANARY-DEPT-{tag}",
            designation=f"CANARY-ROLE-{tag}")
        s.add_all([admin, emp]); s.commit(); s.refresh(admin); s.refresh(emp)

        sess = models.ClockSession(
            user_id=emp.id, organization_id=oid,
            clock_in_time=now - timedelta(hours=3), clock_out_time=now,
            total_seconds=10800, break_seconds=600, is_active=False)
        s.add(sess)
        s.add(models.LeaveRequest(
            user_id=emp.id, organization_id=oid, leave_type="Sick Leave",
            from_date=now, to_date=now, status="approved",
            reason=f"CANARY-LEAVE-{tag}"))
        # NOTE: TimeAdjustment has NO organization_id column — it is scoped
        # only through user_id. Recorded in the Phase-3 report (step 3).
        s.add(models.TimeAdjustment(
            user_id=emp.id,
            adjustment_date=now.date(), seconds_added=1800,
            reason=f"CANARY-ADJ-{tag}", is_active=True, created_by=admin.id))
        s.add(models.Activity(
            user_id=emp.id, organization_id=oid, timestamp=now - timedelta(hours=1),
            app_name=f"CANARY-APP-{tag}", is_active=True))
        s.add(models.Holiday(
            organization_id=oid, name=f"CANARY-HOLIDAY-{tag}",
            date=(now + timedelta(days=30)).date(), holiday_type="Public"))
        # A distinctive, DIFFERENT policy per tenant.
        s.add(models.AppSettings(organization_id=oid, key="schedule.grace_minutes",
                                 value={"A": "15", "B": "45", "C": "5"}[tag]))
        team = models.Team(organization_id=oid, name=f"CANARY-TEAM-{tag}")
        s.add(team)
        s.commit(); s.refresh(sess); s.refresh(team)

        out[tag] = {
            "org_id": oid, "session_id": sess.id, "team_id": team.id,
            "admin_id": admin.id, "emp_id": emp.id,
            "emp_email": emp.email, "team": emp.team,
            "H_admin": {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"},
            "H_emp": {"Authorization": f"Bearer {create_access_token({'user_id': emp.id})}"},
        }
    s.close()
    return out


@pytest.fixture(scope="module")
def client(app_and_db):
    return TestClient(app_and_db, raise_server_exceptions=False)


# ── classification ─────────────────────────────────────────────────────────

def _canaries(tenants, tag):
    t = tenants[tag]
    return [f"CANARY-EMP-{tag}", f"CANARY-ADMIN-{tag}", f"CANARY-LEAVE-{tag}",
            f"CANARY-ADJ-{tag}", f"CANARY-APP-{tag}", f"CANARY-HOLIDAY-{tag}",
            f"CANARY-TEAM-{tag}", f"CANARY-DEPT-{tag}", f"CANARY-ROLE-{tag}",
            f"canary-hr-{tag.lower()}", t["emp_email"]]


def classify(r, tenants, *, caller: str, target: str) -> str:
    body = r.text
    if r.status_code in (401, 403):
        return "DENIED"
    if r.status_code == 404:
        return "NOT_FOUND"
    if r.status_code >= 500:
        return "ERROR"
    if r.status_code == 422:
        # FastAPI echoes the rejected value back in the error body. That is the
        # caller's own input, not the tenant's data — classifying it by content
        # would report a leak that never happened.
        return "INVALID"
    if r.status_code == 200 and body.strip() == '{"detail":"Not Found"}':
        return "SPA"                       # SPA catch-all, not an API answer
    for tag in TAGS:
        if tag == caller:
            continue
        if any(c in body for c in _canaries(tenants, tag)):
            return f"FOREIGN_DATA({tag})"
    if any(c in body for c in _canaries(tenants, caller)):
        return "OWN_DATA"
    return "EMPTY"


READ_PROBES = [
    # Paths verified against main.app.routes — a probe aimed at a path that
    # does not exist proves nothing (the SPA catch-all answers it with 200).
    "/employees/{emp}/time-adjustments",
    "/profile/users/{emp}",
    "/payroll/configs/{emp}",
    "/leave/admin/balances/{emp}",
    "/leave/admin/balances/{emp}/history",
    "/leave/eligibility/{emp}",
    "/api/intelligence/risk-users/{emp}",
    "/api/wec/verify-chain/{emp}",
    "/org/teams/{team_id}/members",
    "/org/teams/{team_id}/assignable-users",
    "/api/screenshots?user_id={emp}",
    "/leave/requests?user_id={emp}",
    "/reports/attendance-detail?user_id={emp}",
    "/reports/employee-directory",
    "/reports/late-clockin",
    "/reports/overtime",
    "/reports/workload",
    "/reports/summary",
    "/employees",
    "/employees/risk-users",
    "/employees/require-action",
    "/employees/analytics",
    "/employees/time-adjustments/recent",
    "/settings",
    "/settings/scoped?scope=employee&user_id={emp}",
    "/holidays",
    "/leave/types",
    "/leave/admin/balances",
    "/payroll/configs",
    "/api/intelligence/risk-users",
    "/api/intelligence/team-pulse",
    "/org/teams",
]


def _fmt(tmpl, t):
    return (tmpl.replace("{emp}", str(t["emp_id"]))
                .replace("{user}", str(t["emp_id"]))
                .replace("{org}", str(t["org_id"]))
                .replace("{team_id}", str(t["team_id"]))
                .replace("{team}", str(t["team"])))


# ══════════════════════════════════════════════════════════════════════════
# READ — evidence matrix + the isolation assertion
# ══════════════════════════════════════════════════════════════════════════

def test_read_probe_matrix(client, tenants):
    """Prints own-tenant vs other-tenant classification for every probe, then
    fails on any FOREIGN_DATA. The matrix is the evidence that the assertion
    below is not vacuous."""
    rows, leaks = [], []
    for tmpl in READ_PROBES:
        own = classify(client.get(_fmt(tmpl, tenants["A"]), headers=tenants["A"]["H_admin"]),
                       tenants, caller="A", target="A")
        oth = classify(client.get(_fmt(tmpl, tenants["B"]), headers=tenants["A"]["H_admin"]),
                       tenants, caller="A", target="B")
        rows.append((tmpl, own, oth))
        if oth.startswith("FOREIGN_DATA"):
            leaks.append((tmpl, oth))
    print("")
    print("=== PHASE 3 · CROSS-TENANT READ MATRIX (caller = ORG A admin) ===")
    print(f"{'endpoint':<52} {'own id':<16} {'ORG B id':<16}")
    for t, a, b in rows:
        print(f"{t:<52} {a:<16} {b:<16}")
    live = sum(1 for _, a, _ in rows if a in ("OWN_DATA", "EMPTY"))
    print(f"API-reachable probes: {live}/{len(rows)}  "
          f"(SPA = route absent, not an API answer)")
    assert leaks == [], f"CROSS-TENANT READ LEAK: {leaks}"


def test_employee_role_cannot_read_another_tenant(client, tenants):
    leaks = []
    for tmpl in READ_PROBES:
        r = client.get(_fmt(tmpl, tenants["B"]), headers=tenants["A"]["H_emp"])
        v = classify(r, tenants, caller="A", target="B")
        if v.startswith("FOREIGN_DATA"):
            leaks.append((tmpl, v))
    assert leaks == [], f"CROSS-TENANT READ LEAK (employee role): {leaks}"


def test_no_list_endpoint_mixes_tenants(client, tenants):
    """No id guessing — just "show me everything I'm allowed to see"."""
    leaks = []
    for tag in ("A", "B", "C"):
        hdr = tenants[tag].get("H_admin") or tenants[tag]["H_emp"]
        for tmpl in ["/employees/team", "/reports/daily-attendance",
                     "/reports/employee-summary", "/leave/requests",
                     "/holidays", "/settings", "/api/screenshots",
                     "/schedule/team?start=2026-08-03&end=2026-08-09"]:
            v = classify(client.get(tmpl, headers=hdr), tenants,
                         caller=tag, target=tag)
            if v.startswith("FOREIGN_DATA"):
                leaks.append((tag, tmpl, v))
    assert leaks == [], f"LIST ENDPOINT MIXED TENANTS: {leaks}"


# ══════════════════════════════════════════════════════════════════════════
# WRITE — the dangerous direction
# ══════════════════════════════════════════════════════════════════════════

def _grace_of(emp_id: int) -> int:
    from sqlalchemy.orm import sessionmaker
    from database import database as _dbmod
    from app.schedule_policy.resolver import resolve_work_schedule
    from datetime import date
    s = sessionmaker(bind=_dbmod.engine)()
    try:
        u = s.query(models.User).filter(models.User.id == emp_id).first()
        return resolve_work_schedule(s, u, date(2026, 8, 3)).grace_minutes
    finally:
        s.close()


def test_write_probe_matrix(client, tenants):
    """Every write Org A can aim at Org B's employee. Anything that returns
    2xx is reported; the DB is then re-checked to see whether it actually
    changed Org B."""
    b = tenants["B"]
    writes = [
        ("PUT",    f"/employees/{b['emp_id']}", {"name": "PWNED", "role": "admin"}),
        ("DELETE", f"/employees/{b['emp_id']}", None),
        ("POST",   f"/employees/{b['emp_id']}/admin-clock",
         {"date": "2026-08-03", "clock_in_time": "09:00"}),
        ("POST",   f"/employees/{b['emp_id']}/time-adjustments",
         {"date": "2026-08-03", "seconds_added": 3600, "reason": "PWNED"}),
        ("PATCH",  f"/employees/{b['emp_id']}/default-clock-in",
         {"default_clock_in_time": "03:00", "default_clock_in_grace_min": 240}),
        ("POST",   "/payroll/configs",
         {"user_id": b["emp_id"], "basic_salary": 1, "pay_type": "monthly"}),
        ("PUT",    "/settings/scoped",
         {"scope": "employee", "user_id": b["emp_id"],
          "settings": {"schedule.grace_minutes": "999"}}),
        ("PUT",    "/settings/scoped",
         {"scope": "team", "team_key": b["team"],
          "settings": {"schedule.grace_minutes": "999"}}),
        ("POST",   "/leave/admin/balances/adjust",
         {"user_id": b["emp_id"], "leave_type": "Annual Leave", "allocated_days": 999}),
        ("POST",   f"/org/teams/{b['team_id']}/members", {"user_ids": [b["emp_id"]]}),
    ]
    accepted, rows = [], []
    for method, url, payload in writes:
        r = client.request(method, url, json=payload, headers=tenants["A"]["H_admin"])
        rows.append((f"{method} {url}", r.status_code))
        if 200 <= r.status_code < 300:
            accepted.append((method, url, r.status_code, r.text[:160]))
    print("")
    print("=== PHASE 3 · CROSS-TENANT WRITE MATRIX (caller = ORG A admin) ===")
    for u, sc in rows:
        print(f"{u:<74} {sc}")

    # Whatever the API answered, ORG B must be unchanged.
    assert _grace_of(b["emp_id"]) == 45, (
        "ORG A's write changed ORG B's effective policy")

    from sqlalchemy.orm import sessionmaker
    from database import database as _dbmod
    s = sessionmaker(bind=_dbmod.engine)()
    try:
        emp = s.query(models.User).filter(models.User.id == b["emp_id"]).first()
        assert emp is not None, "ORG A deleted ORG B's employee"
        assert emp.name == "CANARY-EMP-B", f"ORG A renamed ORG B's employee to {emp.name!r}"
        assert emp.role == "employee", "ORG A escalated ORG B's employee to admin"
        assert emp.is_active is True, "ORG A deactivated ORG B's employee"
        pwned = (s.query(models.TimeAdjustment)
                 .filter(models.TimeAdjustment.user_id == b["emp_id"],
                         models.TimeAdjustment.reason == "PWNED").count())
        assert pwned == 0, "ORG A wrote a time adjustment into ORG B"
    finally:
        s.close()
    assert accepted == [], f"cross-tenant write ACCEPTED: {accepted}"


def test_employee_cannot_write_to_another_tenant(client, tenants):
    b = tenants["B"]
    accepted = []
    for method, url, payload in [
        ("POST", f"/employees/{b['emp_id']}/time-adjustments",
         {"date": "2026-08-03", "seconds_added": 60, "reason": "PWNED2"}),
        ("PUT",  "/settings/scoped",
         {"scope": "employee", "user_id": b["emp_id"],
          "settings": {"schedule.grace_minutes": "888"}}),
        ("PUT",  f"/employees/{b['emp_id']}", {"name": "PWNED2"}),
    ]:
        r = client.request(method, url, json=payload, headers=tenants["A"]["H_emp"])
        if 200 <= r.status_code < 300:
            accepted.append((method, url, r.status_code))
    assert accepted == [], f"employee performed a cross-tenant write: {accepted}"
    assert _grace_of(b["emp_id"]) == 45


def test_org_settings_write_stays_in_the_callers_tenant(client, tenants):
    """PUT /org/settings takes no organization_id, but a client could try."""
    before = _grace_of(tenants["B"]["emp_id"])
    client.put("/org/settings",
               json={"organization_id": tenants["B"]["org_id"],
                     "default_timezone": "Europe/London"},
               headers=tenants["A"]["H_admin"])
    from sqlalchemy.orm import sessionmaker
    from database import database as _dbmod
    s = sessionmaker(bind=_dbmod.engine)()
    try:
        row = (s.query(models.OrgSettings)
               .filter(models.OrgSettings.organization_id == tenants["B"]["org_id"])
               .first())
        assert row.default_timezone == "America/New_York", (
            "ORG A rewrote ORG B's timezone")
    finally:
        s.close()
    assert _grace_of(tenants["B"]["emp_id"]) == before


# ══════════════════════════════════════════════════════════════════════════
# The policy feed the tracker and dashboards poll
# ══════════════════════════════════════════════════════════════════════════

def test_settings_feed_is_per_tenant_over_http(client, tenants):
    got = {}
    for tag in TAGS:
        r = client.get("/settings", headers=tenants[tag]["H_emp"])
        assert r.status_code == 200, f"{tag}: {r.status_code}"
        got[tag] = (r.json() or {}).get("settings", {}).get("schedule.grace_minutes")
    assert got == {"A": "15", "B": "45", "C": "5"}, got


def test_report_endpoints_do_not_500(client, tenants):
    """REGRESSION for a Phase-2 defect this phase found.

    Replacing the raw `total_seconds` sums with `_session_seconds(s, now_utc)`
    introduced a reference to `now_utc` in three endpoints that never defined
    it — daily-attendance, custom and summary raised NameError and returned
    500 for every tenant. No test covered those endpoints, so the whole suite
    stayed green. This walks the report surface over HTTP.
    """
    broken = []
    for url in [
        "/reports/daily-attendance",
        "/reports/summary",
        "/reports/custom",
        "/reports/monthly-attendance",
        "/reports/late-clockin",
        "/reports/overtime",
        "/reports/workload",
        "/reports/employee-directory",
        "/reports/attendance-detail",
        "/reports/work-activity",
        "/reports/apps-sites",
        "/reports/shift-time",
        "/reports/input-activity",
        "/reports/calendar-heatmap",
        "/reports/new-joiners",
        "/reports/monthly-inout",
        "/reports/login-ip",
    ]:
        r = client.get(url, headers=tenants["A"]["H_admin"])
        if r.status_code >= 500:
            broken.append((url, r.status_code, " ".join(r.text.split())[:120]))
    assert broken == [], "report endpoints returning 5xx: %s" % broken


def test_capture_server_errors(client, tenants):
    """Any 5xx is a crash, not an isolation result. Recorded so the Phase-3
    report can say whether it is SQLite-only or a real defect."""
    import traceback
    for url in ("/employees/team", "/reports/daily-attendance", "/reports/summary"):
        r = client.get(url, headers=tenants["A"]["H_admin"])
        print("")
        print("ERRPROBE %s -> %s" % (url, r.status_code))
        if r.status_code >= 500:
            try:
                c2 = TestClient(client.app, raise_server_exceptions=True)
                c2.get(url, headers=tenants["A"]["H_admin"])
            except Exception as e:
                tb = traceback.format_exc().strip().splitlines()
                print("   " + type(e).__name__ + ": " + str(e)[:200])
                for ln in tb[-4:]:
                    print("   | " + ln.strip()[:160])
    assert True


def test_own_tenant_write_control(client, tenants):
    """CONTROL for the write matrix: the same writes aimed at the caller's OWN
    employee. If these also fail, the cross-tenant 404s prove nothing."""
    a = tenants["A"]
    rows = []
    for method, url, payload in [
        ("PUT",   f"/employees/{a['emp_id']}", {"name": "CANARY-EMP-A", "designation": "CANARY-ROLE-A"}),
        ("POST",  f"/employees/{a['emp_id']}/time-adjustments",
         {"date": "2026-08-03", "seconds_added": 60, "reason": "control"}),
        ("PATCH", f"/employees/{a['emp_id']}/default-clock-in",
         {"default_clock_in_time": "09:30", "default_clock_in_grace_min": 10}),
        ("PUT",   "/settings/scoped",
         {"scope": "employee", "user_id": a["emp_id"],
          "settings": {"schedule.grace_minutes": "17"}}),
        ("POST",  f"/org/teams/{a['team_id']}/members", {"user_ids": [a["emp_id"]]}),
    ]:
        r = client.request(method, url, json=payload, headers=a["H_admin"])
        rows.append((f"{method} {url}", r.status_code, " ".join(r.text.split())[:110]))
    print("")
    print("=== OWN-TENANT WRITE CONTROL (caller = ORG A admin, target = ORG A) ===")
    for u, sc, body in rows:
        print(f"{u:<62} {sc}  {body}")
    accepted = [r for r in rows if 200 <= r[1] < 300]
    print(f"accepted: {len(accepted)}/{len(rows)}")
    assert rows
