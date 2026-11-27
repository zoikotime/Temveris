"""
ROUTE SMOKE TEST — every mounted GET must answer, not crash.

Phase 2 introduced a `NameError` that made `/reports/daily-attendance`,
`/reports/custom` and `/reports/summary` return 500 for every tenant. The whole
suite stayed green because no test called those endpoints. Phase 3 found it by
accident.

This walks every mounted GET route as a real authenticated admin and asserts a
non-5xx answer. A 4xx is a PASS: missing required params, wrong role and absent
resources are legitimate outcomes. Only a crash fails.

Deliberately GET-only — this must never mutate data.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from database import models
from database.database import Base

_seq = itertools.count(960000)

# Path params get a plausible value so the handler runs instead of 422-ing at
# the boundary. Anything unmatched falls back to the caller's own employee id.
PARAM_VALUES = {
    "user_id": "{emp}", "emp_id": "{emp}", "employee_id": "{emp}",
    "id": "{emp}", "adj_id": "1", "leave_id": "1", "run_id": "1",
    "team_id": "1", "shift_id": "1", "sid": "1", "aid": "1",
    "session_id": "1", "screenshot_id": "1", "case_id": "1",
    "screenshot_ids": "1", "org_id": "{org}", "organization_id": "{org}",
    "filename": "none.mp4", "platform": "win32", "leave_type_name": "Sick Leave",
    "key": "screenshot_interval", "name": "x", "token": "x", "code": "x",
    "short_code": "x", "plan_id": "1", "invoice_id": "1", "path": "x",
}

# Routes excluded with a stated reason. Nothing is excluded for "it 500s".
SKIP_PREFIXES = (
    "/uploads", "/static", "/attachments", "/email-assets",   # StaticFiles mounts
    "/docs", "/redoc", "/openapi.json",                        # framework
    "/ws",                                                     # websocket
)
SKIP_EXACT = {
    "/api/email-health",        # performs a live SMTP/Resend probe
    "/health/db",               # opens a second engine connection
}

# Routes whose 5xx is caused by the TEST ENVIRONMENT, not by the code. Each
# needs a reason; "it fails" is not one. Anything not listed here must not 5xx.
ENVIRONMENT_5XX = {
    # Object storage is a hard dependency of this route and supabase-py is not
    # installed in the test venv. On a real deployment it resolves or 404s.
    "/leave-attachments/{filename:path}":
        "supabase-py absent in the test environment",
}


@pytest.fixture(scope="module")
def app_(engine):
    Base.metadata.create_all(engine)
    import main
    return main.app


@pytest.fixture(scope="module")
def ctx(app_, engine):
    from sqlalchemy.orm import sessionmaker
    from app.auth.service import create_access_token

    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    oid = next(_seq)
    s.add(models.Organization(id=oid, name="SMOKE-ORG"))
    s.add(models.OrgSettings(organization_id=oid, default_timezone="Asia/Kolkata"))
    s.commit()
    admin = models.User(name="SMOKE-ADMIN", email=f"smoke_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=oid)
    emp = models.User(name="SMOKE-EMP", email=f"smoke_{uuid.uuid4().hex}@x.com",
                      password="x", role="employee", is_active=True,
                      organization_id=oid, team="SMOKE-TEAM")
    s.add_all([admin, emp]); s.commit(); s.refresh(admin); s.refresh(emp)

    now = datetime.now(timezone.utc)
    s.add(models.ClockSession(
        user_id=emp.id, organization_id=oid,
        clock_in_time=now - timedelta(hours=2), clock_out_time=now,
        total_seconds=7200, break_seconds=0, is_active=False))
    s.commit()
    out = {"org": oid, "emp": emp.id, "admin": admin.id,
           "H": {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}}
    s.close()
    return out


def _fill(path: str, ctx) -> str:
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        raw = out[start + 1:end]
        name = raw.split(":", 1)[0]
        value = PARAM_VALUES.get(name, "{emp}")
        value = value.replace("{emp}", str(ctx["emp"])).replace("{org}", str(ctx["org"]))
        out = out[:start] + value + out[end + 1:]
    return out


def _get_routes(app_):
    seen = []
    for r in app_.routes:
        methods = getattr(r, "methods", None) or set()
        if "GET" not in methods:
            continue
        path = getattr(r, "path", "")
        if not path or path in SKIP_EXACT or path.startswith(SKIP_PREFIXES):
            continue
        seen.append(path)
    return sorted(set(seen))


def test_no_mounted_get_route_returns_5xx(app_, ctx):
    client = TestClient(app_, raise_server_exceptions=False)
    routes = _get_routes(app_)
    assert len(routes) > 100, f"only {len(routes)} GET routes discovered — the walk is not covering the app"

    crashes, degraded, counts = [], [], {"2xx": 0, "3xx": 0, "4xx": 0}
    for path in routes:
        url = _fill(path, ctx)
        try:
            r = client.get(url, headers=ctx["H"])
        except Exception as exc:                      # transport-level blow-up
            crashes.append((url, "EXC", (type(exc).__name__ + ": " + str(exc))[:180]))
            continue
        if r.status_code < 500:
            b = str(r.status_code // 100) + "xx"
            counts[b] = counts.get(b, 0) + 1
            continue
        body = " ".join(r.text.split())[:180]
        if path in ENVIRONMENT_5XX:
            degraded.append((url, r.status_code, ENVIRONMENT_5XX[path]))
        elif r.status_code == 503:
            # A HANDLED degradation: the route caught its own failure and
            # answered. Under SQLite this is a table the test schema lacks.
            # Recorded, not failed — it is not an unhandled crash.
            degraded.append((url, 503, body))
        else:
            crashes.append((url, r.status_code, body))

    print("")
    print("=== ROUTE SMOKE: %d GET routes — %d 2xx, %d 4xx, %d handled-503/env, "
          "%d UNHANDLED 5xx ===" % (len(routes), counts.get("2xx", 0),
                                    counts.get("4xx", 0), len(degraded), len(crashes)))
    for u, sc, why in degraded:
        print("  [degraded] %s  %s  — %s" % (sc, u, why[:90]))
    for u, sc, body in crashes:
        print("  [CRASH] %s  %s" % (sc, u))
        print("       %s" % body)

    assert crashes == [], (
        "%d route(s) crashed with an UNHANDLED 5xx:\n" % len(crashes)
        + "\n".join("  %s %s :: %s" % (sc, u, b) for u, sc, b in crashes))


def test_dump_5xx_tracebacks(app_, ctx):
    """Diagnostic companion: the real exception behind any 5xx."""
    import traceback
    client = TestClient(app_, raise_server_exceptions=False)
    strict = TestClient(app_, raise_server_exceptions=True)
    for path in _get_routes(app_):
        url = _fill(path, ctx)
        r = client.get(url, headers=ctx["H"])
        if r.status_code < 500 or r.status_code == 503:
            continue
        print("")
        print("5XX %s -> %s" % (url, r.status_code))
        try:
            strict.get(url, headers=ctx["H"])
        except Exception as e:
            tb = traceback.format_exc().strip().splitlines()
            print("    " + type(e).__name__ + ": " + str(e)[:160])
            for ln in tb[-5:]:
                print("    | " + ln.strip()[:150])
    assert True


# ══════════════════════════════════════════════════════════════════════════
# WRITE ROUTES — validation must REJECT, never crash  (Phase 6)
# ══════════════════════════════════════════════════════════════════════════
#
# 281 POST/PUT/PATCH/DELETE routes were uncovered. A full write smoke would
# have to mutate data, and this suite shares a session-scoped database with
# every other module — that is how the order-dependent failures of Phase 3 were
# born.
#
# So this sends an EMPTY body. A well-formed handler rejects it at the
# validation or authorization layer (4xx) before touching a row, so nothing is
# written; a handler with a broken signature, a missing dependency or an
# undefined name raises during resolution and returns 5xx. That is exactly the
# defect class that made three report endpoints return 500 for every tenant
# while a 900-test suite stayed green.
#
# It is not a substitute for real write coverage — see the report's risk list.

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# Routes that legitimately accept an empty body AND would mutate state.
# Excluded so this stays non-mutating. Each needs a reason.
WRITE_SKIP = {
    "/employees/{emp_id}": "DELETE with no body deactivates an employee",
    "/api/screenshots/{screenshot_id}": "DELETE removes a screenshot",
    "/api/screenshots/timelapse/{filename}": "DELETE removes a rendered video",
    "/employees/time-adjustments/{adj_id}": "DELETE reverts an adjustment",
    "/leave/{leave_id}": "DELETE cancels a leave request",
    "/settings/scoped": "DELETE clears override rows",
    "/org/teams/{team_id}": "DELETE removes a team",
    "/org/teams/{team_id}/members/{user_id}": "DELETE removes a member",
    "/leave/admin/types/{leave_type_name}": "DELETE disables a leave type",
    "/auth/logout": "revokes the caller's own session",
    "/profile/sessions/revoke-others": "revokes the caller's other sessions",
}


def test_no_write_route_crashes_on_an_empty_body(app_, ctx):
    """4xx is the expected, correct answer here. Only 5xx fails."""
    client = TestClient(app_, raise_server_exceptions=False)
    routes, crashes, counts = [], [], {}
    for r in app_.routes:
        methods = (getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}
        path = getattr(r, "path", "")
        if not path or path.startswith(SKIP_PREFIXES) or path in SKIP_EXACT:
            continue
        if path in WRITE_SKIP:
            continue
        for m in sorted(methods & set(WRITE_METHODS)):
            routes.append((m, path))

    assert len(routes) > 150, f"only {len(routes)} write routes discovered"

    for method, path in routes:
        url = _fill(path, ctx)
        try:
            r = client.request(method, url, json={}, headers=ctx["H"])
        except Exception as exc:
            crashes.append((f"{method} {url}", "EXC",
                            (type(exc).__name__ + ": " + str(exc))[:180]))
            continue
        bucket = str(r.status_code // 100) + "xx"
        counts[bucket] = counts.get(bucket, 0) + 1
        # 501 = deliberately Not Implemented (kairos/escalate declares
        # status_code=501); 503 = a handled degradation. Neither is a crash.
        if r.status_code >= 500 and r.status_code not in (501, 503):
            crashes.append((f"{method} {url}", r.status_code,
                            " ".join(r.text.split())[:180]))

    print("")
    print("=== WRITE SMOKE: %d routes — %s, %d UNHANDLED 5xx ==="
          % (len(routes),
             ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())),
             len(crashes)))
    for u, sc, body in crashes:
        print("  [CRASH] %s  %s" % (sc, u))
        print("       %s" % body)

    assert crashes == [], (
        "%d write route(s) crashed on an empty body:\n" % len(crashes)
        + "\n".join("  %s %s :: %s" % (sc, u, b) for u, sc, b in crashes))
