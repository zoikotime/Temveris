"""
PHASE 5 — THE CENTRAL TEST.

Two organizations, the same code, completely different leave catalogues:

    ORG A (shipped names)          ORG B (renamed, nothing in common)
    ─────────────────────          ─────────────────────────────────
    Annual Leave  = pool master    PTO            = pool master
    Unpaid Leave  = unpaid         LWP            = unpaid
    Sick Leave    = sick           Medical Leave  = sick

Org B has DISABLED every shipped type, so if any rule still keys off a display
name it resolves to a type that does not exist in B's catalogue — and the
balance, the pool, the payroll deduction or the HR-review flag silently goes
wrong. That is the failure this file exists to catch.

Also pinned here: the migration bridge. An organization that has not been
backfilled keeps its exact current behaviour.
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app.leave import identity as ident

_org_seq = itertools.count(980000)


@pytest.fixture(scope="session", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"P5Org{oid}"))
    db.add(models.OrgSettings(organization_id=oid, default_timezone="Asia/Kolkata"))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="P5", email=f"p5_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _set_json(db, org_id, key, value):
    full = f"{key}:{org_id}"
    row = (db.query(models.AppSettings)
           .filter(models.AppSettings.organization_id == org_id,
                   models.AppSettings.key == full).first())
    payload = json.dumps(value)
    if row:
        row.value = payload
    else:
        db.add(models.AppSettings(organization_id=org_id, key=full, value=payload))
    db.commit()


def _policies(db, org_id):
    from app.leave.router import _get_effective_policies
    return _get_effective_policies(db, org_id)


# ── the two tenants ─────────────────────────────────────────────────────────

@pytest.fixture
def org_a(db):
    """Shipped catalogue, roles stamped by the backfill."""
    oid = _mk_org(db)
    _set_json(db, oid, "leave_policies", {
        "Annual Leave": {"limit": 15, "shared_pool": False, "pool_master": True},
        "Unpaid Leave": {"limit": 0, "unlimited": True, "is_unpaid": True},
        "Sick Leave":   {"limit": 12, "shared_pool": True, "is_sick": True},
    })
    return oid


@pytest.fixture
def org_b(db):
    """A tenant with NOTHING in common with the shipped catalogue."""
    oid = _mk_org(db)
    _set_json(db, oid, "leave_policies", {
        "PTO":           {"limit": 25, "shared_pool": False, "pool_master": True},
        "LWP":           {"limit": 0, "unlimited": True, "is_unpaid": True},
        "Medical Leave": {"limit": 8, "shared_pool": True, "is_sick": True},
    })
    # Every shipped type switched off — the names must not be able to win.
    _set_json(db, oid, "disabled_leave_types",
              ["Annual Leave", "Unpaid Leave", "Sick Leave", "Casual Leave",
               "Compensatory Leave", "Company Leave", "Half Day Leave"])
    return oid


# ══════════════════════════════════════════════════════════════════════════
# R1 · POOL MASTER
# ══════════════════════════════════════════════════════════════════════════

def test_each_tenant_resolves_its_own_pool_master(db, org_a, org_b):
    pa, pb = _policies(db, org_a), _policies(db, org_b)
    assert ident.pool_master_name(pa) == "Annual Leave"
    assert ident.pool_master_name(pb) == "PTO"

    # And the shipped name must NOT claim the role in org B.
    assert ident.is_pool_master("PTO", pb.get("PTO"), pb) is True
    assert ident.is_pool_master("Annual Leave", pb.get("Annual Leave"), pb) is False, (
        "the shipped name still claimed the pool master in a tenant that "
        "assigned the role elsewhere")


def test_pool_membership_and_sizing_follow_the_configured_master(db, org_a, org_b):
    """The pool is SIZED by the master's entitlement. Org B's is 25, not 15."""
    from app.leave.router import _shared_pool_used
    pa, pb = _policies(db, org_a), _policies(db, org_b)
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)

    assert float(pa[ident.pool_master_name(pa)]["limit"]) == 15.0
    assert float(pb[ident.pool_master_name(pb)]["limit"]) == 25.0

    # _shared_pool_used unions the pool members with the master; for org B that
    # must be {Medical Leave, PTO} and must NOT include "Annual Leave".
    assert _shared_pool_used(ub.id, pb, db) == 0.0
    assert _shared_pool_used(ua.id, pa, db) == 0.0


# ══════════════════════════════════════════════════════════════════════════
# R2 · UNPAID  (payroll is where this one costs money)
# ══════════════════════════════════════════════════════════════════════════

def test_each_tenant_deducts_its_own_unpaid_type(db, org_a, org_b):
    """The whole point: same code, same salary, each tenant's own unpaid type
    produces the deduction — and the other tenant's does not."""
    from app.payroll.service import get_lwp_days
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    end = datetime(2026, 8, 5, tzinfo=timezone.utc)

    for user, unpaid_type in ((ua, "Unpaid Leave"), (ub, "LWP")):
        db.add(models.LeaveRequest(
            user_id=user.id, organization_id=user.organization_id,
            leave_type=unpaid_type, from_date=start, to_date=end,
            status="approved", reason="x", is_lwp=False))
    db.commit()

    assert get_lwp_days(db, ua.id, start, end) == 3
    assert get_lwp_days(db, ub.id, start, end) == 3, (
        "org B's own unpaid type was not deducted — payroll is still looking "
        "for a type named 'Unpaid Leave'")


def test_a_paid_type_is_never_deducted_in_either_tenant(db, org_a, org_b):
    from app.payroll.service import get_lwp_days
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 3, tzinfo=timezone.utc)
    for user, paid_type in ((ua, "Annual Leave"), (ub, "PTO")):
        db.add(models.LeaveRequest(
            user_id=user.id, organization_id=user.organization_id,
            leave_type=paid_type, from_date=start, to_date=end,
            status="approved", reason="x", is_lwp=False))
    db.commit()
    assert get_lwp_days(db, ua.id, start, end) == 0
    assert get_lwp_days(db, ub.id, start, end) == 0


def test_org_b_is_not_deducted_for_the_shipped_unpaid_name(db, org_b):
    """A row carrying the SHIPPED name in a tenant that renamed the role must
    not be silently deducted — that type is not theirs."""
    from app.payroll.service import get_lwp_days
    ub = _mk_user(db, org_b)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 2, tzinfo=timezone.utc)
    db.add(models.LeaveRequest(
        user_id=ub.id, organization_id=org_b, leave_type="Unpaid Leave",
        from_date=start, to_date=end, status="approved", reason="x", is_lwp=False))
    db.commit()
    assert get_lwp_days(db, ub.id, start, end) == 0


def test_auto_lwp_writes_each_tenants_own_unpaid_type(db, org_a, org_b):
    pa, pb = _policies(db, org_a), _policies(db, org_b)
    assert ident.unpaid_name(pa) == "Unpaid Leave"
    assert ident.unpaid_name(pb) == "LWP", (
        "auto-LWP would write a type that does not exist in org B's catalogue, "
        "so the request would appear in no balance view")


# ══════════════════════════════════════════════════════════════════════════
# R3 · SICK
# ══════════════════════════════════════════════════════════════════════════

def test_sick_review_watches_each_tenants_own_type(db, org_a, org_b):
    pa, pb = _policies(db, org_a), _policies(db, org_b)
    assert ident.sick_name(pa) == "Sick Leave"
    assert ident.sick_name(pb) == "Medical Leave"
    assert ident.is_sick("Medical Leave", pb.get("Medical Leave"), pb) is True
    assert ident.is_sick("Sick Leave", pb.get("Sick Leave"), pb) is False
    # ...and org A is unaffected by org B's choice.
    assert ident.is_sick("Sick Leave", pa.get("Sick Leave"), pa) is True
    assert ident.is_sick("Medical Leave", pa.get("Medical Leave"), pa) is False


def test_sick_threshold_setting_and_identity_combine_per_tenant(db, org_a, org_b):
    """The threshold was already a cascaded setting; before Phase 5 it could
    only ever evaluate a type named "Sick Leave", so a tenant that tuned it
    tuned something that never fired."""
    from app.leave import policy as leave_policy
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    db.add(models.AppSettings(organization_id=org_b,
                              key=leave_policy.SETTING_SICK_REVIEW_PER_MONTH,
                              value="4"))
    db.commit()
    assert leave_policy.sick_review_threshold(db, ua) == 2
    assert leave_policy.sick_review_threshold(db, ub) == 4
    assert ident.sick_name(_policies(db, org_b)) == "Medical Leave"


# ══════════════════════════════════════════════════════════════════════════
# MIGRATION BRIDGE — an unmigrated tenant must not change behaviour
# ══════════════════════════════════════════════════════════════════════════

def test_an_unmigrated_tenant_behaves_exactly_as_before(db):
    """No flags anywhere: the shipped names still resolve every role, so the
    day the flags shipped nothing moved for existing customers."""
    oid = _mk_org(db)
    pol = _policies(db, oid)
    assert ident._role_holder(pol, "pool_master") is None
    assert ident.pool_master_name(pol) == "Annual Leave"
    assert ident.unpaid_name(pol) == "Unpaid Leave"
    assert ident.sick_name(pol) == "Sick Leave"
    assert ident.is_pool_master("Annual Leave", pol.get("Annual Leave"), pol) is True
    assert ident.is_unpaid("Unpaid Leave", pol.get("Unpaid Leave"), pol) is True
    assert ident.is_sick("Sick Leave", pol.get("Sick Leave"), pol) is True


def test_backfill_is_idempotent_and_reports_unconfigurable_tenants(db):
    from scripts.backfill_leave_roles import plan

    shipped = _mk_org(db)                      # has the legacy types
    assignments, already, missing = plan(db, shipped)
    assert assignments == {"pool_master": "Annual Leave",
                           "is_unpaid": "Unpaid Leave",
                           "is_sick": "Sick Leave"}
    assert missing == []

    # After stamping, a second run proposes nothing.
    _set_json(db, shipped, "leave_policies", {
        "Annual Leave": {"pool_master": True},
        "Unpaid Leave": {"is_unpaid": True},
        "Sick Leave":   {"is_sick": True},
    })
    assignments, already, missing = plan(db, shipped)
    assert assignments == {}
    assert set(already) == {"pool_master", "is_unpaid", "is_sick"}

    # A tenant with no matching legacy type is REPORTED, never given one.
    custom = _mk_org(db)
    _set_json(db, custom, "leave_policies", {"PTO": {"limit": 20}})
    _set_json(db, custom, "disabled_leave_types",
              ["Annual Leave", "Unpaid Leave", "Sick Leave"])
    assignments, already, missing = plan(db, custom)
    assert assignments == {}
    assert set(missing) == {"pool_master", "is_unpaid", "is_sick"}


# ══════════════════════════════════════════════════════════════════════════
# AMBIGUITY IS REJECTED
# ══════════════════════════════════════════════════════════════════════════

def test_two_claimants_for_one_role_are_rejected(db):
    from fastapi import HTTPException
    from app.leave.router import _assert_roles_unambiguous

    ok = {"PTO": {"pool_master": True}, "Annual Leave": {}}
    _assert_roles_unambiguous(ok, set())          # no raise

    clash = {"PTO": {"pool_master": True}, "Annual Leave": {"pool_master": True}}
    with pytest.raises(HTTPException) as e:
        _assert_roles_unambiguous(clash, set())
    assert e.value.status_code == 400
    assert "pool_master" in str(e.value.detail)
    assert "Annual Leave" in str(e.value.detail) and "PTO" in str(e.value.detail)

    # A DISABLED claimant is not a conflict — that is the rename case.
    _assert_roles_unambiguous(clash, {"Annual Leave"})


def test_role_flags_survive_a_policy_save(db):
    """PUT /leave/policies rebuilt the whole blob from seven fields, which is
    why the flags could never have a working write path: the next save wiped
    them. The writer now merges."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    oid = _mk_org(db)
    admin = models.User(name="P5ADM", email=f"p5a_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=oid)
    db.add(admin); db.commit(); db.refresh(admin)
    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    r = c.post("/leave/admin/types", headers=H, json={
        "leave_type": "PTO", "annual_limit": 25, "notice_days": 7,
        "doc_required": False, "monthly_accrual": 0.0, "shared_pool": False,
        "pool_master": True})
    assert r.status_code == 200, r.text[:300]
    assert ident.pool_master_name(_policies(db, oid)) == "PTO"

    # An OLD client saves policies without the new fields — the role must hold.
    r = c.put("/leave/policies", headers=H, json=[
        {"leave_type": "PTO", "annual_limit": 30, "notice_days": 7,
         "doc_required": False, "monthly_accrual": 0.0, "shared_pool": False}])
    assert r.status_code == 200, r.text[:300]
    pol = _policies(db, oid)
    assert pol["PTO"]["limit"] == 30, "the ordinary field did not save"
    assert ident.pool_master_name(pol) == "PTO", (
        "a policy save wiped the role flag — the write path is still unusable")


def test_api_rejects_a_second_claimant(db):
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    oid = _mk_org(db)
    admin = models.User(name="P5ADM2", email=f"p5b_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=oid)
    db.add(admin); db.commit(); db.refresh(admin)
    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    assert c.post("/leave/admin/types", headers=H, json={
        "leave_type": "PTO", "annual_limit": 25, "notice_days": 0,
        "doc_required": False, "monthly_accrual": 0.0, "shared_pool": False,
        "pool_master": True}).status_code == 200

    r = c.post("/leave/admin/types", headers=H, json={
        "leave_type": "Vacation", "annual_limit": 20, "notice_days": 0,
        "doc_required": False, "monthly_accrual": 0.0, "shared_pool": False,
        "pool_master": True})
    assert r.status_code == 400, f"a second pool master was accepted: {r.text[:200]}"
    assert "Only one active leave type" in r.text


# ══════════════════════════════════════════════════════════════════════════
# EVIDENCE POLICY (Step 6) — evidence_gate BLOCKS, doc_required INFORMS
# ══════════════════════════════════════════════════════════════════════════

def _validate(db, user, leave_type, has_attachment):
    from app.leave.router import _run_validation
    from datetime import datetime as _dt
    return _run_validation(
        user.id, leave_type, _dt(2027, 3, 1), _dt(2027, 3, 2),
        has_attachment, db, org_id=user.organization_id,
    )


def _doc_gate_blocked(result):
    """Did the evidence gate BLOCK this submission?

    Asserted on the flag plus a blocking error, not on the wording. These
    checks used to match the substring "mandatory" in the message, so changing
    the employee-facing copy failed three tests that are not about copy.
    """
    errors, flags, _ = result
    return "EVIDENCE_GATE" in flags and len(errors) > 0


def test_evidence_gate_blocks_and_doc_required_does_not(db):
    """The two flags mean different things and now behave differently. Before
    Phase 5 BOTH were dead — the condition named a type no catalogue has."""
    oid = _mk_org(db)
    _set_json(db, oid, "leave_policies", {
        # Blocking: the org flagged it as an evidence gate.
        "Sick Leave":  {"limit": 12, "doc_required": True, "evidence_gate": True,
                        "notice_days": 0},
        # Informational only: a document is expected but does not stop submit.
        "Child Care Leave": {"limit": 10, "doc_required": True,
                             "evidence_gate": False, "notice_days": 0},
    })
    u = _mk_user(db, oid)

    blocked = _validate(db, u, "Sick Leave", has_attachment=False)
    assert _doc_gate_blocked(blocked), (
        "evidence_gate did not block a submission with no document")
    # The employee-facing wording the Apply Leave form mirrors client-side.
    assert "Supporting document is required for this leave type." in blocked[0]

    assert not _doc_gate_blocked(_validate(db, u, "Sick Leave", has_attachment=True))

    informational = _validate(db, u, "Child Care Leave", has_attachment=False)
    assert not _doc_gate_blocked(informational), (
        "doc_required alone must not block — it is informational")
    assert "DOC_REQUIRED" in informational[1], "the reviewer must still be told"


def test_an_org_can_turn_the_evidence_gate_off(db):
    """It is a control, not an imposition: clearing the flag removes the block
    while keeping the informational badge."""
    oid = _mk_org(db)
    _set_json(db, oid, "leave_policies", {
        "Sick Leave": {"limit": 12, "doc_required": True, "evidence_gate": False,
                       "notice_days": 0},
    })
    u = _mk_user(db, oid)
    errors, flags, _ = _validate(db, u, "Sick Leave", has_attachment=False)
    assert not any("mandatory" in e.lower() for e in errors)
    assert "DOC_REQUIRED" in flags


def test_two_tenants_can_disagree_about_the_evidence_gate(db):
    oid_strict, oid_lenient = _mk_org(db), _mk_org(db)
    for oid, gate in ((oid_strict, True), (oid_lenient, False)):
        _set_json(db, oid, "leave_policies", {
            "Sick Leave": {"limit": 12, "doc_required": True,
                           "evidence_gate": gate, "notice_days": 0}})
    strict, lenient = _mk_user(db, oid_strict), _mk_user(db, oid_lenient)
    assert _doc_gate_blocked(_validate(db, strict, "Sick Leave", has_attachment=False))
    assert not _doc_gate_blocked(_validate(db, lenient, "Sick Leave", has_attachment=False))


# ══════════════════════════════════════════════════════════════════════════
# STEP 18 — THE WHOLE PRODUCT, TWO TENANTS, ONE PROCESS
# ══════════════════════════════════════════════════════════════════════════

def test_two_complete_tenants_operate_independently(db):
    """The Phase-5 acceptance scenario. Every axis this programme touched,
    configured differently, resolved in the same process at the same instant.

        ORG A  Asia/Kolkata      Annual/Unpaid/Sick   anchor 2024-01-01
        ORG B  America/New_York  PTO/LWP/Medical      anchor 2024-01-08
    """
    from datetime import date as _date
    from app import _tz
    from app.schedule_policy import keys as K
    from app.schedule_policy.resolver import resolve_work_schedule, week_start_date
    from app.payroll import policy as pp
    from app.payroll.service import get_lwp_days
    from app.notifications.recipients import alert_recipients
    from app.leave import policy as leave_policy

    a, b = _mk_org(db), _mk_org(db)
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == a).update(
            {"default_timezone": "Asia/Kolkata", "hr_email": "hr-a@a.test"})
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == b).update(
            {"default_timezone": "America/New_York", "hr_email": "hr-b@b.test"})
    db.commit()
    _tz.invalidate_tz_cache(a); _tz.invalidate_tz_cache(b)

    def _set(org, key, value):
        db.add(models.AppSettings(organization_id=org, key=key, value=value))
    _set(a, K.SETTING_WORKING_DAYS, json.dumps(
        ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]))
    _set(b, K.SETTING_WORKING_DAYS, json.dumps(
        ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]))
    _set(a, K.SETTING_GRACE_MINUTES, "15")
    _set(b, K.SETTING_GRACE_MINUTES, "30")
    _set(a, K.SETTING_WEEK_START_DAY, "MONDAY")
    _set(b, K.SETTING_WEEK_START_DAY, "SUNDAY")
    _set(a, pp.SETTING_BIWEEKLY_ANCHOR, "2024-01-01")
    _set(b, pp.SETTING_BIWEEKLY_ANCHOR, "2024-01-08")
    _set(a, pp.SETTING_TIME_ADJ_MAX_PAST_DAYS, "60")
    _set(b, pp.SETTING_TIME_ADJ_MAX_PAST_DAYS, "120")
    _set(a, pp.SETTING_LWP_DIVISOR_DAYS, "30")
    _set(b, pp.SETTING_LWP_DIVISOR_DAYS, "22")
    _set(a, leave_policy.SETTING_YEAR_START_MONTH, "1")
    _set(b, leave_policy.SETTING_YEAR_START_MONTH, "4")
    db.commit()

    _set_json(db, a, "leave_policies", {
        "Annual Leave": {"limit": 15, "pool_master": True},
        "Unpaid Leave": {"limit": 0, "unlimited": True, "is_unpaid": True},
        "Sick Leave":   {"limit": 12, "is_sick": True, "evidence_gate": True},
    })
    _set_json(db, b, "leave_policies", {
        "PTO":           {"limit": 25, "pool_master": True},
        "LWP":           {"limit": 0, "unlimited": True, "is_unpaid": True},
        "Medical Leave": {"limit": 8, "is_sick": True, "evidence_gate": False},
    })
    _set_json(db, b, "disabled_leave_types",
              ["Annual Leave", "Unpaid Leave", "Sick Leave"])

    ua, ub = _mk_user(db, a), _mk_user(db, b)
    pa, pb = _policies(db, a), _policies(db, b)
    monday = _date(2026, 8, 3)
    saturday = _date(2026, 8, 8)
    wednesday = _date(2026, 8, 12)

    # ── leave identity ──────────────────────────────────────────────────
    assert (ident.pool_master_name(pa), ident.pool_master_name(pb)) == ("Annual Leave", "PTO")
    assert (ident.unpaid_name(pa), ident.unpaid_name(pb)) == ("Unpaid Leave", "LWP")
    assert (ident.sick_name(pa), ident.sick_name(pb)) == ("Sick Leave", "Medical Leave")

    # ── leave year ──────────────────────────────────────────────────────
    assert leave_policy.leave_year_start(db, a).month == 1
    assert leave_policy.leave_year_start(db, b).month == 4

    # ── evidence gate ───────────────────────────────────────────────────
    assert _doc_gate_blocked(_validate(db, ua, "Sick Leave", has_attachment=False)), \
        "org A gates sick leave"
    assert not _doc_gate_blocked(_validate(db, ub, "Medical Leave", has_attachment=False)), \
        "org B does not"

    # ── payroll ─────────────────────────────────────────────────────────
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    end = datetime(2026, 8, 4, tzinfo=timezone.utc)
    for u, t in ((ua, "Unpaid Leave"), (ub, "LWP")):
        db.add(models.LeaveRequest(
            user_id=u.id, organization_id=u.organization_id, leave_type=t,
            from_date=start, to_date=end, status="approved", reason="x"))
    db.commit()
    assert get_lwp_days(db, ua.id, start, end) == 2
    assert get_lwp_days(db, ub.id, start, end) == 2
    assert (pp.divisor_days(db, ua), pp.divisor_days(db, ub)) == (30.0, 22.0)
    assert pp.biweekly_anchor(db, ua) != pp.biweekly_anchor(db, ub)
    assert (pp.time_adjustment_max_past_days(db, ua),
            pp.time_adjustment_max_past_days(db, ub)) == (60, 120)

    # ── attendance / schedule ───────────────────────────────────────────
    sa = resolve_work_schedule(db, ua, saturday)
    sb = resolve_work_schedule(db, ub, saturday)
    assert (sa.is_working_day, sb.is_working_day) == (False, True)
    assert (resolve_work_schedule(db, ua, monday).grace_minutes,
            resolve_work_schedule(db, ub, monday).grace_minutes) == (15, 30)
    assert week_start_date(db, a, wednesday) == _date(2026, 8, 10)
    assert week_start_date(db, b, wednesday) == _date(2026, 8, 9)

    # ── timezone: one instant, two local days ───────────────────────────
    instant = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)
    assert _tz.to_local(db, a, instant).date() == _date(2026, 8, 4)
    assert _tz.to_local(db, b, instant).date() == _date(2026, 8, 3)

    # ── notifications ───────────────────────────────────────────────────
    ra = alert_recipients(db, a, purpose="test")
    rb = alert_recipients(db, b, purpose="test")
    assert ra == ["hr-a@a.test"] and rb == ["hr-b@b.test"]
    assert not set(ra) & set(rb)


# ══════════════════════════════════════════════════════════════════════════
# PHASE 6 — MIGRATION SAFETY, END TO END
# ══════════════════════════════════════════════════════════════════════════

def test_backfill_apply_is_idempotent_and_reversible(db):
    """The production rehearsal proposes stamping 4 organizations. This proves
    the full apply -> re-apply -> revert cycle on the same shape, so the
    operator can run --apply knowing what it does and that it can be undone."""
    from scripts import backfill_leave_roles as bf

    org = _mk_org(db)                       # shipped catalogue, no flags yet
    assert ident.unconfigured_roles(_policies(db, org)) == \
        ["pool_master", "is_unpaid", "is_sick"]

    def _apply_once():
        assignments, _, _ = bf.plan(db, org)
        data = bf._load_overrides(db, org)
        for role, name in assignments.items():
            entry = dict(data.get(name) or {})
            entry[role] = True
            data[name] = entry
        if assignments:
            bf._save_overrides(db, org, data)
            db.commit()
        return assignments

    # 1. First apply stamps all three.
    assert set(_apply_once()) == {"pool_master", "is_unpaid", "is_sick"}
    pol = _policies(db, org)
    assert ident.pool_master_name(pol) == "Annual Leave"
    assert ident.unpaid_name(pol) == "Unpaid Leave"
    assert ident.sick_name(pol) == "Sick Leave"
    assert ident.unconfigured_roles(pol) == []

    # 2. IDEMPOTENT — a second run proposes nothing.
    assert _apply_once() == {}

    # 3. REVERSIBLE — revert removes exactly what it set.
    data = bf._load_overrides(db, org)
    for lt, p in list(data.items()):
        for role, legacy in bf.LEGACY_FOR_ROLE.items():
            if p.get(role) and lt == legacy:
                p.pop(role, None)
    bf._save_overrides(db, org, data); db.commit()
    assert ident.unconfigured_roles(_policies(db, org)) == \
        ["pool_master", "is_unpaid", "is_sick"]
    # ...and behaviour returns to the legacy bridge, unchanged.
    assert ident.pool_master_name(_policies(db, org)) == "Annual Leave"


def test_revert_never_undoes_an_admins_own_choice(db):
    """--revert removes only flags sitting on the LEGACY name. A role an admin
    moved to their own type must survive."""
    from scripts import backfill_leave_roles as bf
    org = _mk_org(db)
    _set_json(db, org, "leave_policies", {
        "Annual Leave": {"pool_master": True},     # would be reverted
        "LWP":          {"is_unpaid": True},       # admin's own choice — keep
    })
    data = bf._load_overrides(db, org)
    for lt, p in list(data.items()):
        for role, legacy in bf.LEGACY_FOR_ROLE.items():
            if p.get(role) and lt == legacy:
                p.pop(role, None)
    bf._save_overrides(db, org, data); db.commit()

    pol = _policies(db, org)
    assert ident.unpaid_name(pol) == "LWP", "revert destroyed an admin's choice"
    assert ident.pool_master_name(pol) == "Annual Leave"   # back to the bridge


def test_backfill_is_tenant_scoped(db):
    """Stamping one organization must not touch another."""
    from scripts import backfill_leave_roles as bf
    a, b = _mk_org(db), _mk_org(db)
    assignments, _, _ = bf.plan(db, a)
    data = bf._load_overrides(db, a)
    for role, name in assignments.items():
        data.setdefault(name, {})[role] = True
    bf._save_overrides(db, a, data); db.commit()

    assert ident.unconfigured_roles(_policies(db, a)) == []
    assert ident.unconfigured_roles(_policies(db, b)) == \
        ["pool_master", "is_unpaid", "is_sick"], "org B was modified"


def test_waiting_period_exemption_follows_the_resolved_unpaid_type(db, org_b):
    """Phase 6. `waiting_exempt` and `is_paid` still keyed off the literal
    "Unpaid Leave", so a tenant whose unpaid bucket is "LWP" had its new
    joiners blocked from leave the backend was otherwise happy to grant."""
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    admin = models.User(name="P6ADM", email=f"p6a_{uuid.uuid4().hex}@x.com",
                        password="x", role="admin", is_active=True,
                        organization_id=org_b)
    db.add(admin); db.commit(); db.refresh(admin)
    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    c = TestClient(main.app, raise_server_exceptions=False)

    r = c.get("/leave/types", headers=H)
    assert r.status_code == 200, r.text[:200]
    by_type = {t["leave_type"]: t for t in r.json()}

    assert "LWP" in by_type, "org B's own catalogue was not returned"
    assert by_type["LWP"]["waiting_exempt"] is True, (
        "org B's unpaid type is not waiting-exempt — a new joiner would be "
        "blocked from unpaid leave")
    assert by_type["LWP"]["is_paid"] is False, (
        "org B's unpaid type was reported as PAID")
    # And a normal type is neither.
    assert by_type["PTO"]["is_paid"] is True
    assert by_type["PTO"]["waiting_exempt"] is False
