"""
PHASE 6 — ARCHITECTURAL TENANT GUARDS.

Four models carry no `organization_id` column and depend entirely on a
`user_id` join for tenant scope:

    TimeAdjustment · UserSession · UserPreferences · ActivityDailyScore

Three phases of black-box probing found no leak through any of them, and every
current caller passes a tenant-derived id. The risk is not today's code — it is
that the isolation is IMPLICIT, so a future query written in good faith can
lose it silently and no test would notice.

Adding the column is a migration against live payroll-adjacent data; these
tests buy most of the same protection for none of that risk. They fail when a
new call site appears that does not obviously derive its ids from a
tenant-checked source, which is the moment a human should look.
"""
from __future__ import annotations

import ast
import itertools
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import models
from database.database import Base

_seq = itertools.count(990000)
APP = pathlib.Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(scope="session", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db) -> int:
    oid = next(_seq)
    db.add(models.Organization(id=oid, name=f"P6Org{oid}"))
    db.add(models.OrgSettings(organization_id=oid, default_timezone="Asia/Kolkata"))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="P6", email=f"p6_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u); db.commit(); db.refresh(u)
    return u


# ══════════════════════════════════════════════════════════════════════════
# STEP 5 — TimeAdjustment
# ══════════════════════════════════════════════════════════════════════════

# Every module allowed to query TimeAdjustment, and WHY its ids are safe.
# Adding a name here should be a deliberate decision reviewed by a human.
TIME_ADJ_CALLERS = {
    "_time_adjustments.py":
        "the aggregation helpers themselves — they receive ids, never choose them",
    "employees/router.py":
        "every path re-queries User with organization_id == admin.organization_id "
        "and 404s first (create: L818, list-for-employee: L997, revert: L1041); "
        "the org-wide list filters user_id.in_(org_user_ids)",
}


def _modules_touching(model_name: str) -> set:
    out = set()
    for f in APP.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            if f"models.{model_name}" in code or f"{model_name}." in code:
                out.add(f.relative_to(APP).as_posix())
                break
    return out


def test_no_new_module_queries_time_adjustment_without_review():
    """TimeAdjustment has no organization_id. Its isolation is the caller's
    responsibility, so WHICH callers exist is architecture."""
    touching = {m for m in _modules_touching("TimeAdjustment")
                if not m.startswith("payroll/")}   # payroll goes via the helpers
    unexpected = touching - set(TIME_ADJ_CALLERS)
    assert unexpected == set(), (
        "new TimeAdjustment call site(s) %s — confirm the user_id is derived "
        "from a tenant-checked source, then add it to TIME_ADJ_CALLERS with "
        "the reason." % sorted(unexpected))


def test_time_adjustment_write_paths_tenant_check_before_writing(db):
    """The three write/read-by-id paths must 404 on another tenant's employee.
    Driven over HTTP so it tests the router, not the query."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    a, b = _mk_org(db), _mk_org(db)
    admin_a = models.User(name="A6", email=f"a6_{uuid.uuid4().hex}@x.com",
                          password="x", role="admin", is_active=True,
                          organization_id=a)
    db.add(admin_a); db.commit(); db.refresh(admin_a)
    emp_b = _mk_user(db, b)
    row = models.TimeAdjustment(
        user_id=emp_b.id, adjustment_date=datetime.now(timezone.utc).date(),
        seconds_added=3600, reason="CANARY-B", is_active=True)
    db.add(row); db.commit(); db.refresh(row)

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin_a.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    # READ another tenant's employee
    r = c.get(f"/employees/{emp_b.id}/time-adjustments", headers=H)
    assert r.status_code == 404, r.text[:200]
    assert "CANARY-B" not in r.text

    # CREATE for another tenant's employee
    r = c.post(f"/employees/{emp_b.id}/time-adjustments", headers=H,
               json={"date": datetime.now(timezone.utc).date().isoformat(),
                     "seconds_added": 600, "reason": "PWNED"})
    assert r.status_code == 404, r.text[:200]

    # REVERT another tenant's row, by its real id
    r = c.delete(f"/employees/time-adjustments/{row.id}", headers=H)
    assert r.status_code == 404, r.text[:200]

    db.refresh(row)
    assert row.is_active is True, "org A reverted org B's adjustment"
    assert db.query(models.TimeAdjustment).filter(
        models.TimeAdjustment.reason == "PWNED").count() == 0


def test_org_wide_time_adjustment_list_is_scoped(db):
    """The recent-adjustments list has no employee id to check, so its safety
    rests entirely on user_id.in_(org_user_ids)."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    a, b = _mk_org(db), _mk_org(db)
    admin_a = models.User(name="A6b", email=f"a6b_{uuid.uuid4().hex}@x.com",
                          password="x", role="admin", is_active=True,
                          organization_id=a)
    db.add(admin_a); db.commit(); db.refresh(admin_a)
    emp_a, emp_b = _mk_user(db, a), _mk_user(db, b)
    today = datetime.now(timezone.utc).date()
    db.add(models.TimeAdjustment(user_id=emp_a.id, adjustment_date=today,
                                 seconds_added=60, reason="MINE-A", is_active=True))
    db.add(models.TimeAdjustment(user_id=emp_b.id, adjustment_date=today,
                                 seconds_added=60, reason="THEIRS-B", is_active=True))
    db.commit()

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin_a.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)
    r = c.get("/employees/time-adjustments/recent", headers=H)
    assert r.status_code == 200, r.text[:200]
    assert "MINE-A" in r.text
    assert "THEIRS-B" not in r.text, "the org-wide list leaked another tenant"


# ══════════════════════════════════════════════════════════════════════════
# STEP 6 — ActivityDailyScore
# ══════════════════════════════════════════════════════════════════════════

def test_activity_daily_score_map_is_org_scoped(db):
    """Phase-4 fix. `get_daily_map_for_today` had no org filter and anchored on
    a fixed IST day; both are now the organization's."""
    from app.active_time import service as at
    from app.active_time.service import day_anchor_utc, local_today

    a, b = _mk_org(db), _mk_org(db)
    ua, ub = _mk_user(db, a), _mk_user(db, b)
    anchor = day_anchor_utc(local_today(db, a))
    db.add(models.ActivityDailyScore(user_id=ua.id, day=anchor, active_seconds=111))
    db.add(models.ActivityDailyScore(user_id=ub.id, day=anchor, active_seconds=222))
    db.commit()

    ma = at.get_daily_map_for_today(db, a)
    mb = at.get_daily_map_for_today(db, b)
    assert set(ma) == {ua.id}, "org A's map contained another tenant's rows"
    assert set(mb) == {ub.id}
    assert ma[ua.id].active_seconds == 111
    assert mb[ub.id].active_seconds == 222

    # No org at all still must not silently return one tenant's rows as another's.
    everyone = at.get_daily_map_for_today(db)
    assert {ua.id, ub.id} <= set(everyone), (
        "the unscoped call changed shape — check every caller passes an org")


def test_activity_daily_score_day_anchor_is_per_tenant(db):
    """Two tenants on different clocks must not share a day bucket."""
    from app import _tz
    from app.active_time.service import local_today
    a, b = _mk_org(db), _mk_org(db)
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == b).update(
            {"default_timezone": "Pacific/Auckland"})
    db.commit()
    _tz.invalidate_tz_cache(a); _tz.invalidate_tz_cache(b)
    # Both resolve on their own clock; they may coincide, but neither is a
    # fixed +05:30 for the other.
    assert local_today(db, a) == _tz.local_date(db, a)
    assert local_today(db, b) == _tz.local_date(db, b)


# ══════════════════════════════════════════════════════════════════════════
# STEP 7 — UserSession / UserPreferences are SELF-scoped
# ══════════════════════════════════════════════════════════════════════════

def test_user_preferences_are_only_ever_read_for_the_caller():
    """`UserPreferences` has no organization_id and needs none: every query
    filters on the AUTHENTICATED user's own id. A query keyed by a
    client-supplied id would be a cross-tenant path."""
    src = (APP / "profile" / "router.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "user_id"
                and isinstance(left.value, ast.Attribute)
                and left.value.attr == "UserPreferences"):
            continue
        rhs = node.comparators[0]
        txt = ast.unparse(rhs)
        # Safe: the caller's own id, or a local bound from it.
        if txt not in ("current_user.id", "user_id", "user.id"):
            bad.append(f"line {node.lineno}: UserPreferences.user_id == {txt}")
    assert bad == [], f"UserPreferences read for a non-caller id: {bad}"


def test_user_session_lookups_derive_from_the_callers_own_token():
    """`UserSession` is resolved by the `sid` claim inside the caller's JWT or
    by a hash of the caller's own token — never by a client-supplied id."""
    src = (APP / "profile" / "router.py").read_text(encoding="utf-8")
    assert "models.UserSession.id == sid" in src
    assert "_current_sid(token)" in src
    assert "models.UserSession.token_hash == th" in src
    # And the revoke path must scope to the caller.
    assert "UserSession.user_id == current_user.id" in src or \
           "user_id=current_user.id" in src, (
        "session revocation is not scoped to the calling user")


def test_profile_endpoints_do_not_accept_a_client_supplied_user_id(db):
    """The profile surface is self-scoped by design; /profile/users/{id} is the
    one public projection and must stay inside the tenant."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    a, b = _mk_org(db), _mk_org(db)
    ua, ub = _mk_user(db, a), _mk_user(db, b)
    ub.name = "CANARY-OTHER-TENANT"
    db.commit()
    H = {"Authorization": f"Bearer {create_access_token({'user_id': ua.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    r = c.get(f"/profile/users/{ub.id}", headers=H)
    assert r.status_code == 404 or "CANARY-OTHER-TENANT" not in r.text, r.text[:200]

    r = c.get("/profile/me", headers=H)
    assert r.status_code == 200
    assert "CANARY-OTHER-TENANT" not in r.text


# ══════════════════════════════════════════════════════════════════════════
# STEP 9 / 10 — ELECTRON PRIVACY PROPERTIES (source-level, provable here)
# ══════════════════════════════════════════════════════════════════════════
#
# The pixel-level behaviour of applyPrivacyBlur() needs a display to exercise
# and could NOT be run in this environment — see the report. What IS provable
# without a display is the property that actually protects the employee: there
# must be no path from "blur requested" to "raw image uploaded".

TRACKER = pathlib.Path(__file__).resolve().parents[2] / "desktop-tracker" / "main.js"


def test_a_raw_screenshot_can_never_be_uploaded_when_blur_is_requested():
    src = TRACKER.read_text(encoding="utf-8")

    # The blur runs BEFORE the buffer is encoded for upload.
    blur_at = src.index("applyPrivacyBlur(pngBuf)")
    encode_at = src.index('imageBase64 = pngBuf.toString("base64")')
    assert blur_at < encode_at, "the image is encoded before it is blurred"

    # The failure branch RETURNS — it must not fall through to the upload.
    window = src[blur_at:encode_at]
    assert "return;" in window, (
        "blur failure does not abort the capture — the raw image would be "
        "uploaded, silently breaking the tenant's privacy setting")
    assert "refusing to upload an unblurred image" in window

    # blur_applied is the per-capture fact, never the setting echoed back.
    assert "blur_applied: blurAppliedThisCapture," in src
    assert "blur_applied: screenshotBlur" not in src
    assert "let blurAppliedThisCapture = false;" in src


def test_the_removed_surveillance_signal_is_really_gone():
    """Phase 5 chose to REMOVE the automation-process scan. Verify nothing of
    it survives on either side — a dead client upload, a dead ingest, or a
    setting advertising a control that no longer exists."""
    tracker = TRACKER.read_text(encoding="utf-8")
    for dead in ("scanAutomationProcesses", "automation_processes",
                 "automationWatchlist", "integrityEnabled",
                 "integrity.enabled"):
        assert dead not in tracker, f"{dead} still present in the tracker"

    for f in APP.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]      # prose explaining the removal is fine
            assert "automation_processes" not in code, (
                f"backend ingest at {f.relative_to(APP)}:{i}")

    from app.settings.router import DEFAULTS
    assert not [k for k in DEFAULTS if k.startswith("integrity.")], (
        "integrity.* is registered but has no consumer — that advertises a "
        "control that does nothing")


def test_every_settings_key_the_tracker_reads_is_registered():
    """A key the tracker consumes but the backend never delivers is a policy
    the admin cannot actually set."""
    import re
    from app.settings.router import DEFAULTS
    src = TRACKER.read_text(encoding="utf-8")
    keys = {a or b for a, b in
            re.findall(r's\?\.\["?([a-z_.]+)"?\]|s\?\.([a-z_]+)', src)}
    missing = sorted(k for k in keys if k not in DEFAULTS)
    assert missing == [], f"tracker reads unregistered settings: {missing}"


# ══════════════════════════════════════════════════════════════════════════
# FINAL LOCAL VALIDATION — naive/aware datetime regressions
# ══════════════════════════════════════════════════════════════════════════
#
# `/employees/team` is the admin dashboard's main endpoint. It compares stored
# timestamps against tz-aware boundaries in five places. Four of them lacked
# the module's own `_utc_aware()` guard, so the endpoint returned 500 for the
# WHOLE organization as soon as one employee was clocked in or on a break.
#
# The columns are DateTime(timezone=True), so Postgres returns aware values and
# production is unaffected — but `_utc_aware`'s own docstring says legacy rows
# can be naive there too, which is why the guard exists. Found by running the
# real stack locally; no unit test covered the clocked-in path.

def test_team_status_survives_naive_stored_timestamps(db):
    """Drives /employees/team with a live clocked-in session AND an open
    break — the exact state that produced the 500."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    org = _mk_org(db)
    admin = models.User(name="TS-ADM", email=f"ts_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=org)
    db.add(admin); db.commit(); db.refresh(admin)
    emp = _mk_user(db, org)

    # NAIVE timestamps, as SQLite (and a legacy Postgres row) return them.
    now_naive = datetime.utcnow()
    sess = models.ClockSession(
        user_id=emp.id, organization_id=org,
        clock_in_time=now_naive - timedelta(hours=2),
        clock_out_time=None, is_active=True, total_seconds=0, break_seconds=0)
    db.add(sess); db.commit(); db.refresh(sess)
    db.add(models.BreakSession(
        user_id=emp.id, organization_id=org, session_id=sess.id,
        break_start=now_naive - timedelta(minutes=10), break_end=None,
        is_active=True, break_type="break"))
    db.add(models.Activity(
        user_id=emp.id, organization_id=org,
        timestamp=now_naive - timedelta(minutes=1), is_active=True))
    db.commit()

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)
    r = c.get("/employees/team", headers=H)
    assert r.status_code == 200, (
        "/employees/team crashed with a clocked-in employee and an open "
        f"break: HTTP {r.status_code} {r.text[:200]}")

    # And it must still be crash-free once the break is closed.
    db.query(models.BreakSession).filter(
        models.BreakSession.session_id == sess.id).update(
            {"break_end": now_naive, "is_active": False})
    db.commit()
    r = c.get("/employees/team", headers=H)
    assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"


def test_every_day_boundary_comparison_is_guarded():
    """A grep-style guard: comparing a raw stored timestamp against
    `day_start_utc` is the shape that produced four separate 500s."""
    import re
    src = (APP / "employees" / "router.py").read_text(encoding="utf-8")
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("#", 1)[0]
        if re.search(r"max\((?!_utc_aware)[a-z_]+\.clock_in_time,\s*day_start_utc\)", code):
            bad.append(f"employees/router.py:{i}")
    assert bad == [], (
        "unguarded naive/aware comparison against day_start_utc at: %s" % bad)


# ══════════════════════════════════════════════════════════════════════════
# PERFORMANCE PASS — the two changes whose SAFETY is tenant isolation
# ══════════════════════════════════════════════════════════════════════════
#
# Both were made to cut admin-dashboard refresh latency, and both are correct
# only because of an isolation property that nothing else in the suite pins:
#
#   1. `get_current_organization` gained a 300 s in-process cache. Measured
#      against production (2026-08-17), one indexed query from Cloud Run costs
#      ~147 ms p50, and every tenant-scoped endpoint depends on this call — so
#      a dashboard refresh paid it ~12 times. The cache is safe ONLY while it
#      is keyed per organization and stores no authorization state, exactly the
#      kind of property a later "also return the org name" change would break.
#
#   2. Two Activity aggregates in `team_status` filtered on the timestamp
#      window alone, so one tenant's dashboard scanned every tenant's rows.
#      They are now scoped by `user_id.in_(user_ids)` rather than by
#      `Activity.organization_id` — deliberately, because that column is
#      nullable and legacy rows carry NULL. Both halves of that choice are
#      pinned below.


@pytest.fixture
def _clear_org_cache():
    """The cache is module-level; leaking entries between tests would mask
    exactly the class of bug under test. (Same reasoning as
    test_reports_cache_tenant_isolation.py.)"""
    from app.auth import service as auth
    auth._ORG_CACHE.clear()
    yield
    auth._ORG_CACHE.clear()


class _RaisingSession:
    """Any DB access through this session is a test failure."""

    def query(self, *_a, **_kw):
        raise AssertionError("a cache hit still queried the database")


def test_org_cache_first_call_queries_then_caches(db, _clear_org_cache):
    from app.auth import service as auth

    org = _mk_org(db)
    user = _mk_user(db, org)

    assert auth._ORG_CACHE == {}, "cache should start empty"
    assert auth.get_current_organization(current_user=user, db=db).id == org
    assert org in auth._ORG_CACHE, "first call must populate the cache"


def test_org_cache_hit_performs_zero_db_queries(db, _clear_org_cache):
    """The entire point of the change. If this regresses the latency win is
    gone and nothing else would notice."""
    from app.auth import service as auth

    org = _mk_org(db)
    user = _mk_user(db, org)
    auth.get_current_organization(current_user=user, db=db)          # populate

    assert auth.get_current_organization(
        current_user=user, db=_RaisingSession()).id == org


def test_org_cache_never_serves_one_tenant_to_another(db, _clear_org_cache):
    from app.auth import service as auth

    a, b = _mk_org(db), _mk_org(db)
    ua, ub = _mk_user(db, a), _mk_user(db, b)

    assert auth.get_current_organization(current_user=ua, db=db).id == a
    assert auth.get_current_organization(current_user=ub, db=db).id == b
    # ...and again, now that both are cached.
    assert auth.get_current_organization(current_user=ua, db=_RaisingSession()).id == a
    assert auth.get_current_organization(current_user=ub, db=_RaisingSession()).id == b
    assert set(auth._ORG_CACHE) == {a, b}


def test_org_cache_does_not_pin_a_missing_organization(db, _clear_org_cache):
    """A 404 during provisioning must not be cached, or a tenant created a
    second later stays broken for the whole TTL."""
    from fastapi import HTTPException
    from app.auth import service as auth

    user = _mk_user(db, _mk_org(db))
    user.organization_id = 8_675_309                     # no such row

    with pytest.raises(HTTPException) as e:
        auth.get_current_organization(current_user=user, db=db)
    assert e.value.status_code == 404
    assert 8_675_309 not in auth._ORG_CACHE


def test_org_cache_entry_expires(db, _clear_org_cache):
    """TTL is honoured: an expired entry re-queries instead of being served
    forever."""
    from app.auth import service as auth

    org = _mk_org(db)
    user = _mk_user(db, org)
    auth.get_current_organization(current_user=user, db=db)
    assert auth._org_cache_get(org) is not None

    auth._ORG_CACHE[org]["expires"] -= (auth._ORG_CACHE_TTL + 1)
    assert auth._org_cache_get(org) is None, "expired entry was still served"

    # An expired entry must go back to the DB, not raise.
    assert auth.get_current_organization(current_user=user, db=db).id == org


def _team(db, org_id):
    """Drive the real /employees/team for one org and return its rows."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    from app.auth import service as auth
    from app.employees import router as emp
    import main

    admin = models.User(name="PERF-ADM", email=f"perf_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=org_id)
    db.add(admin); db.commit(); db.refresh(admin)
    # Both caches are module-level and would otherwise carry state between
    # tests in this file.
    auth._ORG_CACHE.clear()
    emp._TEAM_CACHE.clear()

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)
    r = c.get("/employees/team", headers=H)
    assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"
    return r.json()


def test_team_activity_signals_never_read_another_tenants_rows(db):
    """Org B's activity must not influence org A's response, and org A's own
    worker must still be returned."""
    a, b = _mk_org(db), _mk_org(db)
    ua, ub = _mk_user(db, a), _mk_user(db, b)

    now = datetime.now(timezone.utc)
    for uid, oid in ((ua.id, a), (ub.id, b)):
        db.add(models.Activity(user_id=uid, organization_id=oid,
                               timestamp=now - timedelta(minutes=1),
                               app_name="Code.exe", is_active=True,
                               idle_seconds=0))
    db.commit()

    ids = {row["id"] for row in _team(db, a)}
    assert ua.id in ids, "org A's own worker vanished"
    assert ub.id not in ids, "org B's worker leaked into org A's /employees/team"


def test_team_counts_activity_rows_whose_org_column_is_null(db):
    """Legacy rows predate `activities.organization_id` and carry NULL. The
    scope is `user_id.in_(...)` precisely so those rows still count — an
    `Activity.organization_id ==` filter would drop them and silently zero out
    a real worker's idle signal."""
    org = _mk_org(db)
    user = _mk_user(db, org)

    db.add(models.Activity(user_id=user.id, organization_id=None,
                           timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
                           app_name="Legacy.exe", is_active=True, idle_seconds=0))
    db.commit()

    assert user.id in {row["id"] for row in _team(db, org)}, (
        "worker with only NULL-org activity disappeared")


def test_team_handles_an_organization_with_no_workers(db):
    """A tenant with no staff beyond its own admin must return just that admin
    and never 500 — the endpoint has an empty-workspace fast path, and the
    scoped Activity aggregates must not trip over an empty id list."""
    org = _mk_org(db)
    rows = _team(db, org)

    org_user_ids = {u.id for u in db.query(models.User)
                    .filter(models.User.organization_id == org).all()}
    assert rows, "the org's own admin should still be returned"
    assert {r["id"] for r in rows} <= org_user_ids, (
        "a user from outside the organization appeared in an otherwise empty tenant")


def test_every_activity_query_in_team_status_is_tenant_scoped():
    """Source guard. Two of these filtered on the timestamp window ALONE, so
    one tenant's dashboard aggregated every tenant's `activities` rows. The
    accepted scopes are the org's resolved user ids, the org column, or a join
    onto a subquery that already carries one of those."""
    src = (APP / "employees" / "router.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "team_status")

    MARK = "db.query(models.Activity"
    SCOPES = ("Activity.user_id.in_",
              "Activity.organization_id",
              "Activity.user_id == latest_ts_subq.c.user_id")

    leaves = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.stmt) or MARK not in ast.unparse(node):
            continue
        if not [c for c in ast.iter_child_nodes(node)
                if isinstance(c, ast.stmt) and MARK in ast.unparse(c)]:
            leaves.append(node)

    unscoped = [f"line {s.lineno}: {ast.unparse(s)[:100]}"
                for s in leaves if not any(k in ast.unparse(s) for k in SCOPES)]
    assert unscoped == [], (
        "unscoped Activity query in team_status — reads every tenant's rows:\n  "
        + "\n  ".join(unscoped))
