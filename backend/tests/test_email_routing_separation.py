"""Leave routing is separable from attendance/risk routing, per organization.

``notifications.leave_recipients`` is consulted first for ``leave.*`` purposes
only; when it is empty, leave alerts fall back to the existing resolution
(``notifications.alert_recipients`` -> HR/support profile email -> the org's own
admins). Attendance and risk purposes never read it. Customer support mail
still goes to the platform support inbox, which is never a workforce-alert
recipient produced by this resolver.
"""
from __future__ import annotations

import itertools
import pathlib
import re

import pytest

from database import models
from database.database import Base
from app.notifications.recipients import (
    SETTING_ALERT_RECIPIENTS,
    SETTING_DEFAULTS,
    SETTING_LEAVE_RECIPIENTS,
    alert_recipients,
    recipients_for_user,
)

_org_seq = itertools.count(9100)

PLATFORM_SUPPORT = "support@temveris.example"

# Purposes exactly as the live callers pass them.
LEAVE_PURPOSES = ["leave.request"]
ATTENDANCE_PURPOSES = [
    "attendance.requires_attention",   # app/attendance_alerts.py
    "attendance.absent",               # app/employees/router.py (/team)
    "attendance.early_clockout",       # app/employees/router.py (clock-out)
    "break.overrun",                   # app/breaks/monitor.py
]
RISK_PURPOSES = ["risk.alert"]         # app/risk/router.py


@pytest.fixture(scope="module", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _new_org() -> int:
    return next(_org_seq)


def _set(db, org_id, key, value):
    db.add(models.AppSettings(organization_id=org_id, key=key, value=value))
    db.commit()


def _profile(db, org_id, *, hr=None, support=None):
    db.add(models.OrgSettings(organization_id=org_id, hr_email=hr,
                              support_email=support))
    db.commit()


# ── 1. Leave uses notifications.leave_recipients when configured ────────────

def test_leave_uses_leave_recipients_when_configured(db):
    org = _new_org()
    _profile(db, org, hr="profile-hr@acme.test")
    _set(db, org, SETTING_ALERT_RECIPIENTS, "attendance@acme.test")
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "hr@acme.test, people@acme.test")
    for purpose in LEAVE_PURPOSES:
        assert alert_recipients(db, org, purpose=purpose) == [
            "hr@acme.test", "people@acme.test"]


def test_leave_recipients_apply_through_recipients_for_user(db):
    """The leave router resolves via recipients_for_user(db, user, ...)."""
    org = _new_org()
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "hr@acme.test")
    _set(db, org, SETTING_ALERT_RECIPIENTS, "attendance@acme.test")
    user = models.User(name="Worker", email=f"routing-worker-{org}@acme.test",
                       role="employee",
                       is_active=True, organization_id=org)
    db.add(user)
    db.commit()
    assert recipients_for_user(db, user, purpose="leave.request") == ["hr@acme.test"]
    assert recipients_for_user(db, user, purpose="risk.alert") == ["attendance@acme.test"]


# ── 2. Leave falls back to the existing lookup when the setting is empty ────

def test_leave_falls_back_to_alert_recipients_when_leave_setting_empty(db):
    org = _new_org()
    _set(db, org, SETTING_ALERT_RECIPIENTS, "ops@acme.test")
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "")
    assert alert_recipients(db, org, purpose="leave.request") == ["ops@acme.test"]


def test_leave_falls_back_to_profile_then_admins_when_nothing_configured(db):
    org = _new_org()
    _profile(db, org, hr="profile-hr@acme.test", support="helpdesk@acme.test")
    assert alert_recipients(db, org, purpose="leave.request") == [
        "profile-hr@acme.test", "helpdesk@acme.test"]

    org2 = _new_org()
    boss = f"routing-boss-{org2}@acme.test"   # users.email is globally unique
    db.add(models.User(name="Boss", email=boss, role="admin",
                       is_active=True, organization_id=org2))
    db.commit()
    assert alert_recipients(db, org2, purpose="leave.request") == [boss]


def test_leave_with_nothing_configured_still_sends_nothing(db):
    assert alert_recipients(db, _new_org(), purpose="leave.request") == []


def test_unset_leave_setting_is_identical_to_previous_behaviour(db):
    """A tenant that never touches the new key resolves leave exactly as it
    resolves every other alert — i.e. no behaviour change for existing tenants."""
    org = _new_org()
    _profile(db, org, hr="hr-only@acme.test")
    assert (alert_recipients(db, org, purpose="leave.request")
            == alert_recipients(db, org, purpose="attendance.absent")
            == ["hr-only@acme.test"])


# ── 3 / 4. Attendance and risk ignore notifications.leave_recipients ────────

@pytest.mark.parametrize("purpose", ATTENDANCE_PURPOSES + RISK_PURPOSES)
def test_non_leave_purposes_ignore_leave_recipients(db, purpose):
    org = _new_org()
    _set(db, org, SETTING_ALERT_RECIPIENTS, "attendance@acme.test")
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "hr@acme.test")
    assert alert_recipients(db, org, purpose=purpose) == ["attendance@acme.test"]


@pytest.mark.parametrize("purpose", ATTENDANCE_PURPOSES + RISK_PURPOSES)
def test_non_leave_purposes_never_fall_to_leave_recipients(db, purpose):
    """Only the leave key is set: attendance/risk must use the existing
    fallback (profile inbox), never the leave inbox."""
    org = _new_org()
    _profile(db, org, hr="profile-hr@acme.test")
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "hr@acme.test")
    assert alert_recipients(db, org, purpose=purpose) == ["profile-hr@acme.test"]


# ── 5. Tenant A's leave setting cannot affect tenant B ──────────────────────

def test_leave_recipients_are_per_organization(db):
    org_a, org_b, org_c = _new_org(), _new_org(), _new_org()
    _set(db, org_a, SETTING_LEAVE_RECIPIENTS, "hr@a.test")
    _profile(db, org_b, hr="hr@b.test")

    assert alert_recipients(db, org_a, purpose="leave.request") == ["hr@a.test"]
    assert alert_recipients(db, org_b, purpose="leave.request") == ["hr@b.test"]
    assert alert_recipients(db, org_c, purpose="leave.request") == []
    for org in (org_b, org_c):
        for purpose in LEAVE_PURPOSES + ATTENDANCE_PURPOSES + RISK_PURPOSES:
            assert "hr@a.test" not in alert_recipients(db, org, purpose=purpose)


def test_leave_setting_is_registered_as_a_tenant_scoped_setting():
    from app.settings import router as settings_router
    from app.settings import scope as settings_scope
    assert settings_router.DEFAULTS[SETTING_LEAVE_RECIPIENTS] == ""
    meta = settings_scope.KEY_META[SETTING_LEAVE_RECIPIENTS]
    assert meta["group"] == "Notifications"
    assert not meta.get("org_only")  # same cascade as alert_recipients


def test_no_platform_wide_default_recipient():
    assert SETTING_DEFAULTS == {SETTING_ALERT_RECIPIENTS: "",
                                SETTING_LEAVE_RECIPIENTS: ""}


# ── 6. Customer "Need Help" mail still goes to the platform support inbox ───

def test_need_help_still_goes_to_platform_support(monkeypatch):
    from app import email_service
    sent = []

    def _capture(to, subject, html, **kw):
        sent.append((to, kw))
        return True

    monkeypatch.setattr(email_service, "_send", _capture)
    assert email_service.send_support_ticket_email(
        name="Pat", work_email="pat@customer.test",
        issue="Tracker will not start after update",
    ) is True
    assert len(sent) == 1
    to, kw = sent[0]
    assert to == PLATFORM_SUPPORT
    assert kw.get("reply_to") == "pat@customer.test"


# ── 7. Workforce routing never introduces the platform support inbox ────────

@pytest.mark.parametrize("purpose",
                         LEAVE_PURPOSES + ATTENDANCE_PURPOSES + RISK_PURPOSES)
def test_configured_routing_excludes_profile_support_inbox(db, purpose):
    """An org whose Workspace Profile Support Email is the platform support
    inbox stops sending workforce alerts there once its routing is configured."""
    org = _new_org()
    _profile(db, org, hr="hr@acme.test", support=PLATFORM_SUPPORT)
    _set(db, org, SETTING_ALERT_RECIPIENTS, "attendance@acme.test")
    _set(db, org, SETTING_LEAVE_RECIPIENTS, "hr@acme.test")
    assert PLATFORM_SUPPORT not in alert_recipients(db, org, purpose=purpose)


def test_routing_code_hard_codes_no_mailbox():
    """The resolver and settings registration contain no real mailbox literal —
    recipients come from each organization's own configuration."""
    backend = pathlib.Path(__file__).resolve().parents[1]
    addr = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    src = (backend / "app" / "notifications" / "recipients.py").read_text(encoding="utf-8")
    assert addr.findall(src) == []

    sources = {p.relative_to(backend).as_posix():
               p.read_text(encoding="utf-8", errors="ignore")
               for p in (backend / "app").rglob("*.py")}
    for literal in ("attendance@temveris.example", "hr@temveris.example"):
        offenders = [path for path, text in sources.items() if literal in text]
        assert offenders == [], f"{literal} hard-coded in {offenders}"

    # The platform support inbox appears as a string literal only in
    # email_service (FROM_EMAIL/SMTP_USER defaults + the Need Help recipient).
    users = [path for path, text in sources.items()
             if f'"{PLATFORM_SUPPORT}"' in text]
    assert users == ["app/email_service.py"]
