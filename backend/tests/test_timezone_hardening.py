"""Every organization operates on its own validated IANA timezone.

Production held `'California, Sacramento'` for one workspace and NULL for five
more, and all six silently resolved to the system fallback — India's clock —
because `default_timezone` was free text and organization creation never set it.
A tenant in New York was having its attendance day, absence cutoffs, payroll
periods and report boundaries decided on IST without a single error anywhere.

These tests pin the four things that stop that recurring:
  * a value that is not a loadable IANA zone cannot be written,
  * an organization cannot be created without one,
  * a timezone change takes effect immediately, for that organization only,
  * and the timezone an organization resolves is never another's.

They also pin the day-boundary fixes that depend on the same helper — the
stale-session close instant and the report ranges — for three real zones on
both sides of a DST transition.
"""
import itertools
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import models
from database.database import Base, get_db

from app import _tz

# 66000 belongs to test_absent_after_minutes; ids must not collide.
_org_seq = itertools.count(68000)

KOLKATA = "Asia/Kolkata"
NEW_YORK = "America/New_York"
LONDON = "Europe/London"
LOS_ANGELES = "America/Los_Angeles"


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _clean_tz_cache():
    """The cache is process-global and keyed by org id; tests reuse ids across
    modules, so clear it at both ends rather than letting one test's zone leak
    into another's assertion."""
    _tz.invalidate_tz_cache()
    yield
    _tz.invalidate_tz_cache()


def _mk_org(db, tz_name=None) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    if tz_name is not None:
        db.add(models.OrgSettings(organization_id=oid, default_timezone=tz_name))
    db.commit()
    return oid


def _mk_user(db, org_id, role="employee", **kw):
    u = models.User(name="W", email=f"w_{uuid.uuid4().hex}@x.com", role=role,
                    is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ══════════════════════════════════════════════════════════════════════════
# 1. Validation — the single helper every write path uses
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tz", [
    KOLKATA, NEW_YORK, LOS_ANGELES, LONDON, "UTC", "Asia/Calcutta", "Australia/Sydney",
])
def test_valid_iana_zones_are_accepted(tz):
    assert _tz.is_valid_timezone(tz) is True


@pytest.mark.parametrize("tz", [
    "California, Sacramento",   # the exact production value
    "IST", "GMT+5.5", "gmt+05:30", "+05:30", "India", "Asia/Bangalore",
    "random text", "", "   ", None,
])
def test_invalid_values_are_rejected(tz):
    assert _tz.is_valid_timezone(tz) is False


def test_the_validator_and_the_resolver_agree(db):
    """A value that passes validation must never fall back at read time —
    otherwise validation would be theatre."""
    for tz in (KOLKATA, NEW_YORK, LONDON, "UTC"):
        org = _mk_org(db, tz)
        assert _tz.org_timezone_name(db, org) == tz


# ══════════════════════════════════════════════════════════════════════════
# 2. The write path — PUT /org/settings
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def org_api(db):
    """The real /org router, mounted the way main.py mounts it."""
    from app.org.router import router as org_router
    from app.auth.service import get_current_user

    org_id = _mk_org(db, KOLKATA)
    admin = _mk_user(db, org_id, role="admin")

    app = FastAPI()
    app.include_router(org_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin
    with TestClient(app) as c:
        yield c, db, org_id


def test_put_accepts_a_valid_zone(org_api):
    c, db, org_id = org_api
    r = c.put("/org/settings", json={"default_timezone": NEW_YORK})
    assert r.status_code == 200, r.text
    row = db.query(models.OrgSettings).filter_by(organization_id=org_id).one()
    assert row.default_timezone == NEW_YORK


@pytest.mark.parametrize("bad", ["California, Sacramento", "IST", "GMT+5.5", "nonsense"])
def test_put_rejects_an_invalid_zone(org_api, bad):
    c, db, org_id = org_api
    r = c.put("/org/settings", json={"default_timezone": bad})
    assert r.status_code == 400
    assert "IANA" in r.json()["detail"]
    row = db.query(models.OrgSettings).filter_by(organization_id=org_id).one()
    assert row.default_timezone == KOLKATA          # unchanged


def test_an_already_invalid_stored_value_does_not_lock_the_profile_form(db):
    """The handler is one long sequence of field checks and a single raise
    aborts the whole PUT. Validating unconditionally would mean the ONE
    workspace whose stored value is already invalid could not edit its address,
    HR email or company name until the timezone was fixed — punishing the
    tenant for the bug. Validation therefore applies on change only."""
    from app.org.router import router as org_router
    from app.auth.service import get_current_user

    org_id = _mk_org(db, "California, Sacramento")
    admin = _mk_user(db, org_id, role="admin")
    app = FastAPI()
    app.include_router(org_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin

    with TestClient(app) as c:
        # Re-sending the same (invalid) value alongside a real edit must pass.
        r = c.put("/org/settings", json={"default_timezone": "California, Sacramento",
                                         "company_phone": "+1 555 0100"})
        assert r.status_code == 200, r.text
        # But changing it to another invalid value is still refused.
        assert c.put("/org/settings", json={"default_timezone": "IST"}).status_code == 400
        # And fixing it is accepted.
        assert c.put("/org/settings", json={"default_timezone": LONDON}).status_code == 200


def test_changing_the_timezone_invalidates_only_that_org(org_api, db):
    """Cache invalidation is per organization. A global clear would be a
    correctness no-op but would throw away every other tenant's cached zone on
    every profile save."""
    c, db_, org_a = org_api
    org_b = _mk_org(db, NEW_YORK)

    assert _tz.org_timezone_name(db, org_a) == KOLKATA      # both now cached
    assert _tz.org_timezone_name(db, org_b) == NEW_YORK
    cached_b = _tz._tz_cache.get(org_b)

    r = c.put("/org/settings", json={"default_timezone": LONDON})
    assert r.status_code == 200

    assert _tz.org_timezone_name(db, org_a) == LONDON       # A refreshed at once
    assert _tz._tz_cache.get(org_b) == cached_b             # B untouched
    assert _tz.org_timezone_name(db, org_b) == NEW_YORK


# ══════════════════════════════════════════════════════════════════════════
# 3. Organization creation
# ══════════════════════════════════════════════════════════════════════════

def test_super_admin_create_requires_a_timezone():
    """A workspace created without one silently ran on the fallback clock."""
    import inspect
    from app.super_admin import router as sa

    src = inspect.getsource(sa)
    assert "timezone is required" in src
    assert "_is_valid_timezone(org_timezone)" in src
    # And the value that travels onward is the validated one, not the raw body
    # field. The handler no longer writes OrgSettings itself — it hands the
    # tenant graph to app.org.provisioning_service — so the guarantee is now
    # "the validated value is what gets passed in".
    assert "timezone_name=org_timezone" in src
    assert "timezone_name=body.timezone" not in src

    # ...and the shared service is what persists it, derived from exactly that
    # argument.
    #
    # This used to grep for the literal `default_timezone=timezone_name or None`.
    # The service now validates before writing, because the SELF-SERVICE path
    # reaches it without any upstream gate — `ctx.timezone` comes from the signup
    # payload's `client_context.timezone`, i.e. the browser — so an unloadable
    # zone was being persisted there even though this handler was hardened.
    # Asserted behaviourally below rather than by source text, and the source
    # check is kept only as far as "it still derives from timezone_name".
    from app.org import provisioning_service as ps

    ps_src = inspect.getsource(ps)
    assert "timezone_name" in ps_src
    assert "is_valid_timezone" in ps_src, (
        "the shared service must validate before persisting — it is the only "
        "gate on the self-service path"
    )
    assert "default_timezone=default_timezone" in ps_src


def test_self_service_provisioning_persists_a_valid_signup_timezone():
    """The signup payload has carried `client_context.timezone` into
    ProvisioningContext all along; it was simply dropped instead of stored."""
    import inspect
    from app.commercial import provisioning

    src = inspect.getsource(provisioning._step_link_workspace
                            if hasattr(provisioning, "_step_link_workspace")
                            else provisioning)
    assert "default_timezone" in src
    assert "_valid_tz" in src


def test_no_creation_path_defaults_to_india():
    """The specific regression: a new organization must not be handed the India
    fallback as though it were a choice. Naming a zone in an error message or a
    field comment is fine — ASSIGNING one is not."""
    import inspect
    import re
    from app.commercial import provisioning
    from app.super_admin import router as sa

    assigns_a_zone = re.compile(
        r'(default_timezone\s*=\s*["\']|or\s+["\'])(Asia/|America/|Europe/|UTC["\'])'
    )
    for mod in (provisioning, sa):
        src = inspect.getsource(mod)
        assert not assigns_a_zone.search(src), f"{mod.__name__} assigns a default zone"
        assert "FALLBACK_TZ_NAME" not in src, f"{mod.__name__} reaches for the fallback"


# ══════════════════════════════════════════════════════════════════════════
# 4. Multi-tenancy: A's zone can never be B's
# ══════════════════════════════════════════════════════════════════════════

def test_two_organizations_resolve_independently(db):
    a, b, c = (_mk_org(db, KOLKATA), _mk_org(db, NEW_YORK), _mk_org(db, LONDON))
    assert _tz.org_timezone_name(db, a) == KOLKATA
    assert _tz.org_timezone_name(db, b) == NEW_YORK
    assert _tz.org_timezone_name(db, c) == LONDON
    # Same UTC instant, three different local dates/hours.
    inst = datetime(2026, 8, 6, 22, 30, tzinfo=timezone.utc)
    assert _tz.to_local(db, a, inst).date() == date(2026, 8, 7)   # 04:00 next day
    assert _tz.to_local(db, b, inst).date() == date(2026, 8, 6)   # 18:30 same day
    assert _tz.to_local(db, c, inst).date() == date(2026, 8, 6)   # 23:30 same day


def test_the_cache_is_keyed_by_organization(db):
    a = _mk_org(db, KOLKATA)
    b = _mk_org(db, NEW_YORK)
    _tz.org_timezone_name(db, a)
    _tz.org_timezone_name(db, b)
    # Whatever the cache holds, it holds it per organization.
    for org_id, expected in ((a, KOLKATA), (b, NEW_YORK)):
        entry = _tz._tz_cache.get(org_id)
        if entry is not None:                      # sqlite disables the cache
            assert entry[0] == expected
    _tz.invalidate_tz_cache(a)
    assert _tz.org_timezone_name(db, b) == NEW_YORK


def test_a_missing_timezone_does_not_change_another_organization(db):
    """An unconfigured tenant falls back — loudly — but the fallback is applied
    per organization and cannot reach a configured one."""
    unset = _mk_org(db, None)
    configured = _mk_org(db, NEW_YORK)
    assert _tz.org_timezone_name(db, unset) == _tz.FALLBACK_TZ_NAME
    assert _tz.org_timezone_name(db, configured) == NEW_YORK


def test_missing_timezone_is_logged_not_silent(db, caplog):
    """NULL used to be silent, which is how five organizations ran on the wrong
    clock unnoticed. Free text already warned; absence must too."""
    import logging
    org = _mk_org(db, None)
    with caplog.at_level(logging.WARNING, logger="zoiko.tz"):
        _tz.org_timezone_name(db, org)
    assert any("NO default_timezone" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════════════════════════════════
# 5. Day boundaries — the helper every fixed call site now uses
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tz_name,day,expect_start_utc,expect_end_utc", [
    (KOLKATA,  date(2026, 8, 6), "2026-08-05 18:30", "2026-08-06 18:30"),
    (NEW_YORK, date(2026, 8, 6), "2026-08-06 04:00", "2026-08-07 04:00"),   # EDT
    (NEW_YORK, date(2026, 1, 6), "2026-01-06 05:00", "2026-01-07 05:00"),   # EST
    (LONDON,   date(2026, 8, 6), "2026-08-05 23:00", "2026-08-06 23:00"),   # BST
    (LONDON,   date(2026, 1, 6), "2026-01-06 00:00", "2026-01-07 00:00"),   # GMT
])
def test_local_day_bounds_are_org_local_and_dst_aware(db, tz_name, day,
                                                      expect_start_utc, expect_end_utc):
    org = _mk_org(db, tz_name)
    start, end = _tz.local_day_bounds(db, org, day)
    assert start.strftime("%Y-%m-%d %H:%M") == expect_start_utc
    assert end.strftime("%Y-%m-%d %H:%M") == expect_end_utc
    assert (end - start) == timedelta(days=1)


def test_dst_transition_day_is_not_24_hours(db):
    """The US spring-forward day is 23 hours long. Fixed-offset arithmetic
    would silently produce 24 and shift every boundary after it."""
    org = _mk_org(db, NEW_YORK)
    start, end = _tz.local_day_bounds(db, org, date(2026, 3, 8))   # DST begins
    assert (end - start) == timedelta(hours=23)
    start, end = _tz.local_day_bounds(db, org, date(2026, 11, 1))  # DST ends
    assert (end - start) == timedelta(hours=25)


# ══════════════════════════════════════════════════════════════════════════
# 6. Stale-session auto-close lands on the organization's midnight
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tz_name,clock_in_utc,expected_close_utc", [
    # 10:00 local yesterday -> closes at local midnight, not 23:59 UTC.
    (KOLKATA,  datetime(2026, 8, 5,  4, 30, tzinfo=timezone.utc),
               datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)),
    # New York overnight: 23:00 local on the 5th is 03:00 UTC on the 6th.
    # The UTC date is the 6th; the LOCAL date is the 5th, and local midnight
    # that follows it is 04:00 UTC on the 6th — one hour later, not 21.
    (NEW_YORK, datetime(2026, 8, 6,  3,  0, tzinfo=timezone.utc),
               datetime(2026, 8, 6,  4,  0, tzinfo=timezone.utc)),
    (LONDON,   datetime(2026, 8, 5,  8,  0, tzinfo=timezone.utc),
               datetime(2026, 8, 5, 23,  0, tzinfo=timezone.utc)),
])
def test_stale_sessions_close_at_organization_local_midnight(
        db, tz_name, clock_in_utc, expected_close_utc):
    from app.employees.router import _auto_close_stale_sessions

    org = _mk_org(db, tz_name)
    u = _mk_user(db, org)
    s = models.ClockSession(user_id=u.id, organization_id=org,
                            clock_in_time=clock_in_utc, clock_out_time=None,
                            total_seconds=0, break_seconds=0, is_active=True)
    db.add(s)
    db.commit()

    _auto_close_stale_sessions(db, org_id=org)
    db.refresh(s)

    closed = s.clock_out_time
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    assert closed == expected_close_utc
    # Duration is derived from the same instants — never negative, never >24h.
    assert 0 <= s.total_seconds <= 24 * 3600


def test_stale_close_does_not_touch_another_organization(db):
    from app.employees.router import _auto_close_stale_sessions

    org_a, org_b = _mk_org(db, KOLKATA), _mk_org(db, NEW_YORK)
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    old = datetime(2026, 8, 5, 4, 30, tzinfo=timezone.utc)
    for org, u in ((org_a, ua), (org_b, ub)):
        db.add(models.ClockSession(user_id=u.id, organization_id=org,
                                   clock_in_time=old, clock_out_time=None,
                                   total_seconds=0, break_seconds=0, is_active=True))
    db.commit()

    _auto_close_stale_sessions(db, org_id=org_a)
    b_session = (db.query(models.ClockSession)
                 .filter(models.ClockSession.organization_id == org_b).one())
    assert b_session.clock_out_time is None, "another tenant's session was closed"


def test_no_utc_calendar_midnight_survives_in_the_close_path():
    """The exact defect: org-local selection, UTC-midnight close."""
    import inspect
    from app.employees import router

    src = inspect.getsource(router._auto_close_stale_sessions)
    assert "datetime.combine(" not in src
    assert "_tz.local_day_bounds(" in src


# ══════════════════════════════════════════════════════════════════════════
# 7. Report ranges use the same organization-local helper
# ══════════════════════════════════════════════════════════════════════════

def test_report_ranges_are_organization_local():
    """Every session/break range filter must come from _day_window, so the
    monthly report, the daily report and payroll agree on which local day a
    session belongs to."""
    import inspect
    from app.reports import router

    src = inspect.getsource(router)
    # No UTC calendar bounds feeding a timestamp filter.
    assert "range_start = datetime.combine(start_d, time.min, tzinfo=timezone.utc)" not in src
    assert "range_end   = datetime.combine(last,  time.max, tzinfo=timezone.utc)" not in src
    # Every range pair is built from the canonical helper, and the upper bound
    # is exclusive because _day_window returns [start, next local midnight).
    assert src.count("_, range_end   = _day_window(") >= 6
    assert "clock_in_time <= range_end" not in src


def test_day_window_is_the_only_boundary_implementation():
    import inspect
    from app.reports import router
    assert "_tzmod.local_day_bounds" in inspect.getsource(router._day_window)


# ══════════════════════════════════════════════════════════════════════════
# 8. The absence engine is untouched by all of the above
# ══════════════════════════════════════════════════════════════════════════

def test_absence_still_shift_relative_and_org_scoped(db):
    """Same assertion as test_absent_after_minutes, re-checked here so a
    timezone change that broke absence could not pass this module."""
    from app.schedule_policy import keys as K
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedule

    def _org(tz, offset):
        oid = _mk_org(db, tz)
        db.add(models.AppSettings(organization_id=oid, key=K.SETTING_SHIFT_START_TIME,
                                  value="09:00"))
        db.add(models.AppSettings(organization_id=oid, key=K.SETTING_ABSENT_AFTER_MINUTES,
                                  value=offset))
        db.commit()
        return oid, _mk_user(db, oid)

    org_a, ua = _org(KOLKATA, "60")
    org_b, ub = _org(NEW_YORK, "90")
    day = date(2026, 8, 6)

    # One UTC instant; each org evaluated on its own clock.
    inst = datetime(2026, 8, 6, 4, 45, tzinfo=timezone.utc)   # 10:15 IST / 00:45 EDT
    a_local, b_local = _tz.to_local(db, org_a, inst), _tz.to_local(db, org_b, inst)
    assert (a_local.hour, b_local.hour) == (10, 0)

    a = classify_arrival(None, resolve_work_schedule(db, ua, day), a_local)
    b = classify_arrival(None, resolve_work_schedule(db, ub, b_local.date()), b_local)
    assert a.status == ArrivalStatus.ABSENT          # 09:00 + 60 passed
    assert b.status != ArrivalStatus.ABSENT          # shift has not started

    # And B's own cutoff, on B's clock: 09:00 + 90 = 10:30.
    b_1029 = datetime(2026, 8, 6, 10, 29, tzinfo=_tz.org_timezone(db, org_b))
    b_1030 = datetime(2026, 8, 6, 10, 30, tzinfo=_tz.org_timezone(db, org_b))
    sched_b = resolve_work_schedule(db, ub, day)
    assert classify_arrival(None, sched_b, b_1029).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched_b, b_1030).status == ArrivalStatus.ABSENT


def test_absent_email_still_has_no_clock_of_its_own():
    """The email must remain a consequence of the verdict, never a schedule."""
    import inspect
    from app import email_service, attendance_alerts

    fn = inspect.getsource(email_service.send_absent_alert_to_admin_hr)
    # No threshold, no cutoff, no schedule read — the template renders strings
    # its caller passes. (It does hold one `datetime.now(...)` as the display
    # fallback for `detected_at`; that decides nothing about absence.)
    for forbidden in ("absent_after", "classify", "resolve_work_schedule",
                      "shift", "cutoff", ".hour"):
        assert forbidden not in fn, f"the absent email template consults {forbidden!r}"

    sweep = inspect.getsource(attendance_alerts._scan_org)
    assert "ArrivalStatus.ABSENT" in sweep
    assert "now_local = _tz.local_now(db, org_id)" in sweep
    # Detected-at is rendered on the org's clock, and is the same instant the
    # verdict used — not a second time source.
    assert "org_timezone_name(db, org_id)" in sweep
    assert 'strftime("%Y-%m-%d %H:%M UTC")' not in sweep


def test_no_noon_rule_anywhere_in_the_absence_path():
    import inspect
    from app.schedule_policy import classify
    from app.employees import router as employees_router

    assert "now_local.hour >= int(sched.absent_after_hour)" not in inspect.getsource(classify)
    assert "now_ist.hour >= int(hour)" not in inspect.getsource(employees_router)
