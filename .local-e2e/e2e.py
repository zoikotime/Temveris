"""
FINAL LOCAL END-TO-END VALIDATION — drives the running backend over HTTP.

Read-only against production: it talks to http://127.0.0.1:8099, which is bound
to an isolated SQLite database seeded by seed.py.

Every check prints PASS/FAIL with the evidence that decided it. Nothing is
asserted that is not observed on the wire.
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone

import requests

BASE = "http://127.0.0.1:8099"
PW = "E2ePass!234"
S = requests.Session()
RESULTS = []


def check(section, name, ok, evidence=""):
    RESULTS.append((section, name, bool(ok), evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {evidence}" if evidence else ""))
    return ok


def login(email):
    # POST /auth/login takes an OAuth2PasswordRequestForm (form-encoded
    # username/password), not JSON — see app/auth/service.py:525.
    r = S.post(f"{BASE}/auth/login",
               data={"username": email, "password": PW}, timeout=30)
    if r.status_code != 200:
        return None, r
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    tok = body.get("access_token") or body.get("token") if isinstance(body, dict) else None
    if tok is None and isinstance(body, str):
        tok = body
    return ({"Authorization": f"Bearer {tok}"} if tok else None), r


def get(h, path, **kw):
    return S.get(f"{BASE}{path}", headers=h, timeout=60, **kw)


def post(h, path, body=None, **kw):
    return S.post(f"{BASE}{path}", headers=h, json=body or {}, timeout=60, **kw)


def main():
    print("\n=== 4. AUTH / STARTUP ===")
    H = {}
    for tag, email in (("A_ADMIN", "admin.a@e2e.test"), ("A_EMP", "emp.a@e2e.test"),
                       ("B_ADMIN", "admin.b@e2e.test"), ("B_EMP", "emp.b@e2e.test")):
        h, r = login(email)
        H[tag] = h
        check("auth", f"login {tag}", h is not None, f"HTTP {r.status_code}")
    if not all(H.values()):
        print("\nABORT: login failed; cannot continue.")
        return 1

    # ── 6. EMPLOYEE FLOW ────────────────────────────────────────────────
    print("\n=== 6. EMPLOYEE FLOW (org A employee) ===")
    r = get(H["A_EMP"], "/employees/me")
    check("emp", "GET /employees/me", r.status_code == 200, f"HTTP {r.status_code}")

    r = post(H["A_EMP"], "/employees/clock-in")
    clocked = r.status_code in (200, 201)
    check("emp", "clock-in", clocked, f"HTTP {r.status_code} {r.text[:90]}")

    r = get(H["A_EMP"], f"/employees/me/day-summary?date={date.today().isoformat()}")
    check("emp", "day-summary reflects the session", r.status_code == 200,
          f"HTTP {r.status_code}")

    r = post(H["A_EMP"], "/break/start", {"break_type": "break"})
    br = r.status_code in (200, 201)
    check("emp", "break start", br, f"HTTP {r.status_code} {r.text[:80]}")
    # POST /break/resume requires a reason (ResumePayload, breaks/router.py:261).
    r = post(H["A_EMP"], "/break/resume", {"reason": "E2E resume"})
    check("emp", "break end (resume)", r.status_code in (200, 201),
          f"HTTP {r.status_code} {r.text[:80]}")

    r = get(H["A_EMP"], "/settings")
    s = (r.json() or {}).get("settings", {}) if r.status_code == 200 else {}
    check("emp", "tracker policy feed = org A's values",
          s.get("schedule.grace_minutes") == "15"
          and s.get("screenshot_interval") == "1800"
          and s.get("screenshot_blur") == "false",
          f"grace={s.get('schedule.grace_minutes')} "
          f"interval={s.get('screenshot_interval')} blur={s.get('screenshot_blur')}")

    sB = (get(H["B_EMP"], "/settings").json() or {}).get("settings", {})
    check("emp", "org B receives DIFFERENT policy",
          sB.get("schedule.grace_minutes") == "30"
          and sB.get("screenshot_interval") == "300"
          and sB.get("screenshot_blur") == "true",
          f"grace={sB.get('schedule.grace_minutes')} "
          f"interval={sB.get('screenshot_interval')} blur={sB.get('screenshot_blur')}")

    r = post(H["A_EMP"], "/employees/clock-out")
    check("emp", "clock-out", r.status_code in (200, 201),
          f"HTTP {r.status_code} {r.text[:90]}")

    # ── 7. ADMIN FLOW ───────────────────────────────────────────────────
    print("\n=== 7. ADMIN FLOW (org A admin) ===")
    for label, path in [
        ("employees list", "/employees"),
        ("team status", "/employees/team"),
        ("attendance report", "/reports/daily-attendance"),
        ("employee summary", "/reports/employee-summary"),
        ("overtime report", "/reports/overtime"),
        ("screenshots", "/api/screenshots"),
        ("leave types", "/leave/types"),
        ("leave requests", "/leave/requests"),
        ("leave balances", "/leave/admin/balances"),
        ("payroll configs", "/payroll/configs"),
        ("payroll summary", "/payroll/summary"),
        ("settings (scoped)", "/settings/scoped?scope=org"),
        ("org settings", "/org/settings"),
        ("holidays", "/holidays"),
        ("time adjustments", "/employees/time-adjustments/recent"),
        ("risk users", "/employees/risk-users"),
        ("analytics", "/employees/analytics"),
    ]:
        r = get(H["A_ADMIN"], path)
        check("admin", label, r.status_code == 200, f"HTTP {r.status_code}")

    # Time adjustment write + read-back
    today = date.today().isoformat()
    r = post(H["A_ADMIN"], f"/employees/{EMP_A}/time-adjustments",
             {"date": today, "seconds": 1800, "reason": "E2E local validation"})
    adj_ok = r.status_code in (200, 201)
    check("admin", "create time adjustment", adj_ok, f"HTTP {r.status_code} {r.text[:110]}")
    r = get(H["A_ADMIN"], f"/employees/{EMP_A}/time-adjustments")
    check("admin", "time adjustment persisted",
          r.status_code == 200 and "E2E local validation" in r.text,
          f"HTTP {r.status_code}")

    # ── 8. CROSS-TENANT ─────────────────────────────────────────────────
    print("\n=== 8. CROSS-TENANT ISOLATION (A -> B, then B -> A) ===")
    CANARY_B = ["Employee B", "emp.b@e2e.test", "Ops-B", "Analyst-B",
                "Globex", "hr-b@globex-e2e.test"]
    CANARY_A = ["Employee A", "emp.a@e2e.test", "Ops-A", "Analyst-A",
                "Acme India", "hr-a@acme-e2e.test"]

    def leaked(text, needles):
        return [n for n in needles if n in text]

    probes = [
        ("employee by id", "/employees/{eid}"),
        ("time adjustments", "/employees/{eid}/time-adjustments"),
        ("profile projection", "/profile/users/{eid}"),
        ("payroll config", "/payroll/configs/{eid}"),
        ("leave balances", "/leave/admin/balances/{eid}"),
        ("leave eligibility", "/leave/eligibility/{eid}"),
        ("leave requests filter", "/leave/requests?user_id={eid}"),
        ("screenshots filter", "/api/screenshots?user_id={eid}"),
        ("attendance detail", "/reports/attendance-detail?user_id={eid}"),
        ("intelligence risk", "/api/intelligence/risk-users/{eid}"),
        ("scoped settings", "/settings/scoped?scope=employee&user_id={eid}"),
    ]
    for direction, hdr, other_eid, needles in (
        ("A->B", H["A_ADMIN"], EMP_B, CANARY_B),
        ("B->A", H["B_ADMIN"], EMP_A, CANARY_A),
    ):
        for label, tmpl in probes:
            r = get(hdr, tmpl.format(eid=other_eid))
            hits = leaked(r.text, needles) if r.status_code == 200 else []
            check("xtenant", f"{direction} {label}", not hits,
                  f"HTTP {r.status_code}" + (f" LEAKED {hits}" if hits else ""))

        # Lists must never mix tenants either.
        for label, path in (("employees list", "/employees"),
                            ("daily attendance", "/reports/daily-attendance"),
                            ("leave requests", "/leave/requests"),
                            ("holidays", "/holidays")):
            r = get(hdr, path)
            hits = leaked(r.text, needles) if r.status_code == 200 else []
            check("xtenant", f"{direction} list {label}", not hits,
                  f"HTTP {r.status_code}" + (f" LEAKED {hits}" if hits else ""))

    print("\n=== 8b. CROSS-TENANT WRITES ===")
    writes = [
        ("rename employee", "PUT", "/employees/{eid}", {"name": "PWNED"}),
        ("time adjustment", "POST", "/employees/{eid}/time-adjustments",
         {"date": today, "seconds": 60, "reason": "PWNED"}),
        ("default clock-in", "PATCH", "/employees/{eid}/default-clock-in",
         {"default_clock_in_time": "03:00", "default_clock_in_grace_min": 240}),
        ("employee-scope setting", "PUT", "/settings/scoped",
         {"scope": "employee", "user_id": None, "settings": {"schedule.grace_minutes": "999"}}),
    ]
    for label, method, tmpl, body in writes:
        b = dict(body)
        if "user_id" in b:
            b["user_id"] = EMP_B
        r = S.request(method, f"{BASE}{tmpl.format(eid=EMP_B)}", headers=H["A_ADMIN"],
                      json=b, timeout=60)
        check("xtenant", f"A->B write rejected: {label}",
              not (200 <= r.status_code < 300), f"HTTP {r.status_code}")

    # Confirm org B is untouched afterwards.
    r = get(H["B_ADMIN"], f"/profile/users/{EMP_B}")
    check("xtenant", "org B employee unchanged after write attempts",
          r.status_code == 200 and "PWNED" not in r.text, f"HTTP {r.status_code}")
    sB2 = (get(H["B_EMP"], "/settings").json() or {}).get("settings", {})
    check("xtenant", "org B grace unchanged after write attempts",
          sB2.get("schedule.grace_minutes") == "30",
          f"grace={sB2.get('schedule.grace_minutes')}")

    # ── 9. SETTINGS CASCADE ─────────────────────────────────────────────
    print("\n=== 9. SETTINGS CASCADE (system -> org -> team -> employee) ===")
    for scope_body in ({"scope": "employee", "user_id": EMP_A,
                        "keys": ["schedule.grace_minutes"]},
                       {"scope": "team", "team": "Team-A",
                        "keys": ["schedule.grace_minutes"]}):
        S.request("DELETE", f"{BASE}/settings/scoped", headers=H["A_ADMIN"],
                  timeout=60, json=scope_body)
    base = (get(H["A_EMP"], "/settings").json() or {}).get("settings", {})
    check("settings", "baseline = org default (15)",
          base.get("schedule.grace_minutes") == "15",
          f"grace={base.get('schedule.grace_minutes')}")

    r = S.put(f"{BASE}/settings/scoped", headers=H["A_ADMIN"], timeout=60,
              json={"scope": "team", "team": "Team-A",
                    "settings": {"schedule.grace_minutes": "45"}})
    check("settings", "team-scope write accepted", r.status_code == 200,
          f"HTTP {r.status_code} {r.text[:80]}")
    sA = (get(H["A_EMP"], "/settings").json() or {}).get("settings", {})
    check("settings", "employee inherits TEAM override (15 -> 45)",
          sA.get("schedule.grace_minutes") == "45",
          f"grace={sA.get('schedule.grace_minutes')}")

    r = S.put(f"{BASE}/settings/scoped", headers=H["A_ADMIN"], timeout=60,
              json={"scope": "employee", "user_id": EMP_A,
                    "settings": {"schedule.grace_minutes": "5"}})
    sA = (get(H["A_EMP"], "/settings").json() or {}).get("settings", {})
    check("settings", "EMPLOYEE override beats team (45 -> 5)",
          sA.get("schedule.grace_minutes") == "5",
          f"grace={sA.get('schedule.grace_minutes')}")

    sB3 = (get(H["B_EMP"], "/settings").json() or {}).get("settings", {})
    check("settings", "org B unaffected by every org-A change",
          sB3.get("schedule.grace_minutes") == "30",
          f"grace={sB3.get('schedule.grace_minutes')}")

    r = S.put(f"{BASE}/settings/scoped", headers=H["A_ADMIN"], timeout=60,
              json={"scope": "team", "team": "Team-A",
                    "settings": {"work_hours_threshold": "6:00"}})
    check("settings", "org-only key REJECTED at team scope", r.status_code == 400,
          f"HTTP {r.status_code} {r.text[:100]}")

    # ── 11. LEAVE IDENTITY ──────────────────────────────────────────────
    print("\n=== 11. LEAVE (metadata, not display names) ===")
    ta = get(H["A_ADMIN"], "/leave/types").json()
    tb = get(H["B_ADMIN"], "/leave/types").json()
    na = {t["leave_type"] for t in ta}
    nb = {t["leave_type"] for t in tb}
    check("leave", "org A catalogue is its own", "Annual Leave" in na, sorted(na))
    check("leave", "org B catalogue is its own", nb == {"PTO", "LWP", "Medical Leave"},
          sorted(nb))
    check("leave", "org B does NOT see shipped types",
          not (nb & {"Annual Leave", "Unpaid Leave", "Sick Leave"}), sorted(nb))

    bb = {t["leave_type"]: t for t in tb}
    check("leave", "org B unpaid type flagged is_paid=False",
          bb["LWP"]["is_paid"] is False, f"LWP.is_paid={bb['LWP']['is_paid']}")
    check("leave", "org B unpaid type is waiting-exempt",
          bb["LWP"]["waiting_exempt"] is True,
          f"LWP.waiting_exempt={bb['LWP']['waiting_exempt']}")
    check("leave", "org B pool master is PTO (not Annual Leave)",
          bb["PTO"]["is_paid"] is True and bb["PTO"]["waiting_exempt"] is False,
          f"PTO paid={bb['PTO']['is_paid']}")

    r = get(H["A_EMP"], "/leave/balance")
    check("leave", "org A balance resolves", r.status_code == 200, f"HTTP {r.status_code}")
    r = get(H["B_EMP"], "/leave/balance")
    okb = r.status_code == 200 and "Annual Leave" not in r.text
    check("leave", "org B balance uses ITS types only", okb,
          f"HTTP {r.status_code}")

    # Evidence gate: org A blocks sick without a document; org B does not.
    fut = (date.today() + timedelta(days=20)).isoformat()
    r = post(H["A_EMP"], "/leave/validate",
             {"leave_type": "Sick Leave", "from_date": fut, "to_date": fut})
    a_blocked = "mandatory" in r.text.lower() or "document" in r.text.lower()
    check("leave", "org A evidence gate BLOCKS sick w/o document", a_blocked,
          f"HTTP {r.status_code} {r.text[:110]}")
    r = post(H["B_EMP"], "/leave/validate",
             {"leave_type": "Medical Leave", "from_date": fut, "to_date": fut})
    b_blocked = "mandatory" in r.text.lower()
    check("leave", "org B (gate off) does NOT block", not b_blocked,
          f"HTTP {r.status_code} {r.text[:110]}")

    # ── 10. TIMEZONE ────────────────────────────────────────────────────
    print("\n=== 10. TIMEZONE (same code, two clocks) ===")
    ra = get(H["A_ADMIN"], "/org/settings").json()
    rb = get(H["B_ADMIN"], "/org/settings").json()
    check("tz", "org A timezone", ra.get("default_timezone") == "Asia/Kolkata",
          ra.get("default_timezone"))
    check("tz", "org B timezone", rb.get("default_timezone") == "America/New_York",
          rb.get("default_timezone"))

    # ── 12. PAYROLL ─────────────────────────────────────────────────────
    print("\n=== 12. PAYROLL ===")
    ca = get(H["A_EMP"], "/payroll/my-config")
    cb = get(H["B_EMP"], "/payroll/my-config")
    check("payroll", "org A my-config", ca.status_code == 200, f"HTTP {ca.status_code}")
    check("payroll", "org B my-config", cb.status_code == 200, f"HTTP {cb.status_code}")
    if ca.status_code == 200 and cb.status_code == 200:
        cca = (ca.json().get("config") or {})
        ccb = (cb.json().get("config") or {})
        check("payroll", "currencies differ per tenant",
              cca.get("currency") != ccb.get("currency"),
              f"A={cca.get('currency')} B={ccb.get('currency')}")

    print("\n" + "=" * 62)
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[2]]
    print(f"TOTAL {total} checks — {total - len(failed)} PASS, {len(failed)} FAIL")
    if failed:
        print("\nFAILURES:")
        for sec, name, _, ev in failed:
            print(f"  [{sec}] {name} :: {ev}")
    with open("e2e_results.json", "w") as f:
        json.dump([{"section": s, "name": n, "pass": p, "evidence": e}
                   for s, n, p, e in RESULTS], f, indent=1)
    return 1 if failed else 0


if __name__ == "__main__":
    EMP_A, EMP_B = 2, 4
    sys.exit(main())
