"""Break Governance & Workday Provenance — regression tests.

Covers the spec's six acceptance tests plus the entitlement matrix, the
overrun monitor, HR-email throttling and the immutable provenance stream.

Like test_attendance_lifecycle, these call the router/service functions
directly (no auth/HTTP plumbing) against the in-memory SQLite DB from
conftest. The HR-escalation email is monkeypatched so no real mail is sent.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import BackgroundTasks

from database import models
from database.database import Base

from app.employees.router import (
    clock_in,
    clock_out,
    start_break as emp_start_break,
    resume_work,
    _StartBreakBody,
)
from app.breaks import governance as gov
from app.breaks import monitor as break_monitor
from app.breaks.router import get_active_overruns, get_entitlements
import app.email_service as email_service
from app.tracking import router as tracking_router

# Monotonic id source for unique test-user names/emails (see _mk_user).
_user_seq = itertools.count(1)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    """The governance flow needs the full schema (break_sessions, work_events,
    work_evidence_chain, app_settings, clock_sessions, users). create_all is
    idempotent via checkfirst."""
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, employment_type="full_time", role="employee"):
    # Unique suffix per user. Timestamp alone collides on Windows (coarse clock
    # resolution) when two users are created back-to-back, tripping the
    # users.email UNIQUE constraint — use a monotonic counter to guarantee
    # uniqueness regardless of clock granularity or test ordering.
    uid = next(_user_seq)
    u = models.User(
        name=f"Worker {uid}",
        email=f"w_{uid}@example.com",
        role=role,
        is_active=True,
        employment_type=employment_type,
        department="Engineering",
        team="Platform",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    tracking_router._cache_invalidate(u.id)
    return u


@pytest.fixture
def full_time(db):
    return _mk_user(db, "full_time")


@pytest.fixture
def part_time(db):
    return _mk_user(db, "part_time")


@pytest.fixture
def contractor(db):
    return _mk_user(db, "contractor")


def _active_break(db, user):
    return (
        db.query(models.BreakSession)
        .filter(models.BreakSession.user_id == user.id, models.BreakSession.break_end.is_(None))
        .order_by(models.BreakSession.break_start.desc())
        .first()
    )


def _events(db, user, event_type):
    return (
        db.query(models.WorkEvent)
        .filter(models.WorkEvent.user_id == user.id, models.WorkEvent.event_type == event_type)
        .all()
    )


@pytest.fixture
def capture_hr_email(monkeypatch):
    calls = []
    monkeypatch.setattr(
        email_service, "send_break_overrun_alert_to_hr",
        lambda **kw: (calls.append(kw) or True),
    )
    return calls


@pytest.fixture(autouse=True)
def capture_employee_email(monkeypatch):
    """Capture the worker-facing overrun reminder. autouse so the real sender is
    never invoked during tests (a dev .env could carry live credentials); tests
    that assert on it just request this fixture by name."""
    calls = []
    monkeypatch.setattr(
        email_service, "send_break_overrun_alert_to_employee",
        lambda **kw: (calls.append(kw) or True),
    )
    return calls


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — WORKER TYPE + ENTITLEMENT EVALUATION (pure)
# ══════════════════════════════════════════════════════════════════════════════
def test_worker_type_normalization():
    assert gov.normalize_worker_type("Full Time") == "full_time"
    assert gov.normalize_worker_type("PART-TIME") == "part_time"
    assert gov.normalize_worker_type("part time") == "part_time"
    assert gov.normalize_worker_type("Contractor") == "contractor"
    assert gov.normalize_worker_type("freelance") == "contractor"
    assert gov.normalize_worker_type(None) == "full_time"


def test_allowances_match_spec(db):
    assert gov.allowance_minutes(db, "break") == 15   # BR-02 short
    assert gov.allowance_minutes(db, "lunch") == 60   # BR-02 lunch


def test_entitlement_matrix(db):
    assert gov.entitlement_for(db, "full_time", "break") == 2
    assert gov.entitlement_for(db, "full_time", "lunch") == 1
    assert gov.entitlement_for(db, "part_time", "break") == 1
    assert gov.entitlement_for(db, "part_time", "lunch") == 0   # BR-03
    assert gov.entitlement_for(db, "contractor", "break") == 1
    assert gov.entitlement_for(db, "contractor", "lunch") == 0   # default: short only


def test_governance_indicator_state():
    assert gov.governance_state(0, 15) == "green"
    assert gov.governance_state(13 * 60, 15) == "yellow"   # >= 80% of 15m
    assert gov.governance_state(15 * 60, 15) == "red"      # exceeded
    assert gov.governance_state(40 * 60, 15) == "red"


def test_status_chip_and_banner():
    assert gov.status_chip("break", False) == "On Short Break"
    assert gov.status_chip("break", True) == "On Short Break (Exceeded)"
    assert gov.status_chip("lunch", True) == "On Lunch Break (Exceeded)"
    assert gov.overrun_banner("break") == "Your Short Break allowance has ended. Please resume work."
    assert gov.overrun_banner("lunch") == "Your Lunch Break allowance has ended. Please resume work."


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — ENFORCEMENT (the spec's acceptance tests 3, 4, 5 + BR-01 + stop-work)
# ══════════════════════════════════════════════════════════════════════════════
def test_3_third_short_break_blocked(db, full_time):
    """Test 3: full-time workers get TWO Short Breaks per workday — the second is
    allowed, the third is blocked."""
    clock_in(db=db, user=full_time)
    first = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    assert first["success"] is True and first["allowed"] is True

    resume_work(db=db, user=full_time)

    second = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    assert second["success"] is True and second["allowed"] is True

    resume_work(db=db, user=full_time)

    third = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    assert third["allowed"] is False
    assert third["code"] == "DAILY_SHORT_BREAK_ALREADY_USED"
    assert third["message"] == "You have already used all 2 of your Short Breaks for today."


def test_part_time_second_short_break_still_blocked(db, part_time):
    """The extra Short Break is full-time only — part-time keeps its single one."""
    clock_in(db=db, user=part_time)
    first = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=part_time)
    assert first["allowed"] is True

    resume_work(db=db, user=part_time)

    second = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=part_time)
    assert second["allowed"] is False
    assert second["code"] == "DAILY_SHORT_BREAK_ALREADY_USED"
    assert second["message"] == "You have already used your Short Break for today."


def test_4_second_lunch_break_blocked(db, full_time):
    """Test 4: worker tries a second Lunch Break the same day → blocked."""
    clock_in(db=db, user=full_time)
    first = emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=full_time)
    assert first["allowed"] is True

    resume_work(db=db, user=full_time)

    second = emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=full_time)
    assert second["allowed"] is False
    assert second["code"] == "DAILY_LUNCH_BREAK_ALREADY_USED"
    assert second["message"] == "You have already used your Lunch Break for today."


def test_5_part_time_lunch_blocked_by_api(db, part_time):
    """Test 5: part-time worker tries Lunch → blocked by API (BR-03)."""
    clock_in(db=db, user=part_time)
    res = emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=part_time)
    assert res["allowed"] is False
    assert res["code"] == "LUNCH_BREAK_NOT_ALLOWED"
    # No break row was created.
    assert _active_break(db, part_time) is None


def test_contractor_lunch_blocked_default_policy(db, contractor):
    clock_in(db=db, user=contractor)
    res = emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=contractor)
    assert res["allowed"] is False and res["code"] == "LUNCH_BREAK_NOT_ALLOWED"
    # But a short break is allowed.
    ok = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=contractor)
    assert ok["allowed"] is True


def test_br01_only_one_active_break(db, full_time):
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    second = emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=full_time)
    assert second["allowed"] is False
    assert second["code"] == "ACTIVE_BREAK_IN_PROGRESS"


def test_no_break_after_stop_working(db, full_time):
    clock_in(db=db, user=full_time)
    # Backdate so required hours are met — otherwise Controlled Clock-Out would
    # ask for an early-clock-out reason and leave the session open on a weekday.
    s = db.query(models.ClockSession).filter(
        models.ClockSession.user_id == full_time.id,
        models.ClockSession.clock_out_time.is_(None),
    ).first()
    s.clock_in_time = datetime.now(timezone.utc) - timedelta(hours=10)
    db.commit()
    clock_out(background_tasks=BackgroundTasks(), db=db, user=full_time)
    res = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    assert res["allowed"] is False
    assert res["code"] == "NOT_CLOCKED_IN"


def test_part_time_entitlements_endpoint_hides_lunch(db, part_time):
    out = get_entitlements(db=db, user=part_time)
    assert out["worker_type"] == "part_time"
    assert out["lunch_allowed"] is False
    assert out["short_allowed"] is True


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — IMMUTABLE PROVENANCE STREAM
# ══════════════════════════════════════════════════════════════════════════════
def test_provenance_events_on_start_and_deny(db, full_time):
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    # BREAK_START_REQUESTED + BREAK_STARTED on a successful start.
    assert len(_events(db, full_time, "BREAK_START_REQUESTED")) == 1
    assert len(_events(db, full_time, "BREAK_STARTED")) == 1
    # A second concurrent attempt is denied → BREAK_DENIED appended.
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    denied = _events(db, full_time, "BREAK_DENIED")
    assert len(denied) == 1
    assert denied[0].payload["denialReason"] == "ACTIVE_BREAK_IN_PROGRESS"
    # The chain entry captures the required provenance fields.
    started = _events(db, full_time, "BREAK_STARTED")[0]
    for key in ("tenantId", "workerId", "workerType", "policyVersionId",
                "scheduleVersionId", "timestamp", "sessionId", "breakType"):
        assert key in started.payload


def test_break_ended_records_overrun_summary(db, full_time):
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=22)
    db.commit()
    resume_work(db=db, user=full_time)
    ended = _events(db, full_time, "BREAK_ENDED")
    assert len(ended) == 1
    assert ended[0].payload["overrunFlag"] is True
    assert ended[0].payload["overrunMinutes"] >= 6


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — OVERRUN MONITOR + HR ESCALATION
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _no_stray_open_breaks(db):
    """process_break_overruns is a WORKER: it scans every open break in the
    database, which is correct for a background monitor and is why these tests
    can count its output at all. The in-memory DB is shared across modules
    though, so an open break another module left behind is counted here and the
    global assertions below fail — in a full run only. Close strays first; this
    changes no product behaviour.
    """
    from datetime import datetime as _dt, timezone as _tzc
    stray = (db.query(models.BreakSession)
             .filter(models.BreakSession.break_end.is_(None)).all())
    for b in stray:
        b.break_end = _dt.now(_tzc.utc)
    if stray:
        db.commit()
    yield


def test_1_short_break_exceeds_marks_overdue(db, full_time, capture_hr_email, capture_employee_email):
    """Test 1: short break reaches 15m → OVERDUE + provenance + employee alert
    (HR/admin overrun email is disabled — employee-only)."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    base = datetime.now(timezone.utc)
    brk.break_start = base - timedelta(minutes=16)
    db.commit()

    summary = break_monitor.process_break_overruns(db, now=base)
    assert summary["overdue"] == 1
    assert summary["alerts"] == 0

    db.refresh(brk)
    assert brk.overrun_flag is True
    assert brk.status == "overdue"
    assert brk.overrun_notification_sent is False
    assert (brk.alert_count or 0) == 0
    # Timer is NOT auto-stopped — the break is still active.
    assert brk.break_end is None

    # Provenance: allowance reached + overrun + notification sent.
    assert len(_events(db, full_time, "BREAK_ALLOWANCE_REACHED")) == 1
    assert len(_events(db, full_time, "BREAK_OVERRUN")) == 1
    assert len(_events(db, full_time, "BREAK_OVERRUN_NOTIFICATION_SENT")) == 0
    assert len(capture_hr_email) == 0

    # Still inside the 10m grace at 16m → no alert yet.
    summary = break_monitor.process_break_overruns(db, now=base + timedelta(minutes=4, seconds=1))
    assert summary["alerts"] == 0
    assert len(capture_employee_email) == 0

    # 25m01s = allowance 15 + grace 10 reached → first alert fires.
    summary = break_monitor.process_break_overruns(db, now=base + timedelta(minutes=9, seconds=1))
    assert summary["overdue"] == 1
    assert summary["alerts"] == 1

    db.refresh(brk)
    assert brk.overrun_notification_sent is True
    assert brk.alert_count == 1
    assert len(_events(db, full_time, "BREAK_OVERRUN_NOTIFICATION_SENT")) == 1

    # HR/admin email is disabled; the EMPLOYEE gets exactly one (non-reminder) alert.
    assert len(capture_hr_email) == 0
    assert len(capture_employee_email) == 1
    kw = capture_employee_email[0]
    assert kw["break_type"] == "break"
    assert kw["allowed_minutes"] == 15
    assert kw["is_reminder"] is False


def test_2_overrun_email_throttled_to_every_30_min(db, full_time, capture_hr_email, capture_employee_email):
    """Test 2 + spam guard: first alert after grace, reminder every 30 min.
    Alerts go to the EMPLOYEE (HR/admin disabled)."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    # 30m: past the 15 + 10 = 25m effective limit → due.
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()

    # First pass → first alert (employee only; HR never notified).
    break_monitor.process_break_overruns(db, now=datetime.now(timezone.utc))
    assert len(capture_employee_email) == 1
    assert len(capture_hr_email) == 0

    # Immediate second pass → NO new email (within the 30-min window).
    summary = break_monitor.process_break_overruns(db, now=datetime.now(timezone.utc))
    assert summary["alerts"] == 0
    assert len(capture_employee_email) == 1

    # Backdate the last alert by 31 min → reminder fires.
    db.refresh(brk)
    brk.last_alert_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db.commit()
    break_monitor.process_break_overruns(db, now=datetime.now(timezone.utc))
    assert len(capture_employee_email) == 2
    assert capture_employee_email[1]["is_reminder"] is True
    assert len(capture_hr_email) == 0
    db.refresh(brk)
    assert brk.alert_count == 2
    # Notification-sent provenance appended once per alert (idempotent per count).
    assert len(_events(db, full_time, "BREAK_OVERRUN_NOTIFICATION_SENT")) == 2


def test_worker_emailed_at_own_address_on_overrun(db, full_time, capture_hr_email, capture_employee_email):
    """The worker is reminded at their OWN email — first alert only after
    allowance + grace, then a reminder every 30 min. HR/admin are not notified."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    base = datetime.now(timezone.utc)

    # 16 min — over allowance but inside the 10m grace → no worker email yet.
    brk.break_start = base - timedelta(minutes=16)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    mine = [c for c in capture_employee_email if c.get("employee_email") == full_time.email]
    assert mine == []

    # 25 min — effective limit (15 + 10) reached → worker gets the first (non-reminder) email.
    brk.break_start = base - timedelta(minutes=25)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    mine = [c for c in capture_employee_email if c.get("employee_email") == full_time.email]
    assert len(mine) == 1
    assert mine[0]["employee_name"] == full_time.name
    assert mine[0]["break_type"] == "break"
    assert mine[0]["allowed_minutes"] == 15
    assert mine[0]["is_reminder"] is False
    # HR/admin overrun email is disabled — the worker is the only recipient.
    assert len(_emails_for(capture_hr_email, full_time)) == 0

    # Reminder cadence mirrors HR — backdate the last alert by 31 min.
    db.refresh(brk)
    brk.last_alert_at = base - timedelta(minutes=31)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    mine = [c for c in capture_employee_email if c.get("employee_email") == full_time.email]
    assert len(mine) == 2
    assert mine[1]["is_reminder"] is True

    # Provenance records the worker as the recipient (HR/admin disabled).
    notify = _events(db, full_time, "BREAK_OVERRUN_NOTIFICATION_SENT")[0]
    assert full_time.email in notify.payload["recipients"]
    assert notify.payload["employeeEmailDispatched"] is True


def test_monitor_idempotent_no_duplicate_events(db, full_time, capture_hr_email):
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=65)
    db.commit()

    for _ in range(3):
        break_monitor.process_break_overruns(db, now=datetime.now(timezone.utc))

    # Allowance-reached + overrun emitted exactly once despite 3 passes.
    assert len(_events(db, full_time, "BREAK_ALLOWANCE_REACHED")) == 1
    assert len(_events(db, full_time, "BREAK_OVERRUN")) == 1


def test_alert_claim_is_single_winner(db, full_time):
    """Duplicate-notification guard: the atomic claim lets exactly ONE caller
    send each alert, so running the monitor in 2 gunicorn workers / Cloud Run
    instances can no longer double-send. Simulate the race by invoking the
    claim helpers twice for the same break (a second concurrent worker)."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()

    now = datetime.now(timezone.utc)
    # First alert: worker A wins the claim, worker B loses → only one email.
    assert break_monitor._claim_first_alert(db, brk.id, now) is True
    assert break_monitor._claim_first_alert(db, brk.id, now) is False

    # Reminder slot is not due yet (last_alert_at just set) → nobody sends.
    assert break_monitor._claim_reminder(db, brk.id, now, is_first_alert=False) is False

    # After the reminder window elapses, exactly one worker wins the next slot.
    db.refresh(brk)
    brk.last_alert_at = now - timedelta(minutes=gov.OVERRUN_REMINDER_MINUTES + 1)
    db.commit()
    later = now + timedelta(seconds=1)
    assert break_monitor._claim_reminder(db, brk.id, later, is_first_alert=False) is True
    assert break_monitor._claim_reminder(db, brk.id, later, is_first_alert=False) is False


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — CONFIGURABLE GRACE PERIOD (alert only after allowance + grace)
# ══════════════════════════════════════════════════════════════════════════════
def test_grace_defaults_and_effective_limit(db):
    """Default grace is 10 min per break type → effective limits 25 / 70 min."""
    assert gov.grace_minutes(db, "break") == 10
    assert gov.grace_minutes(db, "lunch") == 10
    assert gov.effective_limit_minutes(db, "break") == 25   # 15 + 10
    assert gov.effective_limit_minutes(db, "lunch") == 70   # 60 + 10


def test_grace_period_is_configurable(db):
    """app_settings overrides the per-break-type grace; effective limit tracks it."""
    db.add(models.AppSettings(key=gov.SHORT_GRACE_KEY, value="10"))
    db.add(models.AppSettings(key=gov.LUNCH_GRACE_KEY, value="3"))
    db.commit()
    try:
        assert gov.grace_minutes(db, "break") == 10
        assert gov.grace_minutes(db, "lunch") == 3
        assert gov.effective_limit_minutes(db, "break") == 25   # 15 + 10
        assert gov.effective_limit_minutes(db, "lunch") == 63   # 60 + 3
    finally:
        # Committed rows persist across tests on the shared in-memory DB — drop
        # them so default-grace tests keep seeing 5.
        for k in (gov.SHORT_GRACE_KEY, gov.LUNCH_GRACE_KEY):
            row = db.query(models.AppSettings).filter(models.AppSettings.key == k).first()
            if row:
                db.delete(row)
        db.commit()


def _emails_for(capture, user):
    """Emails captured for THIS worker — the monitor scans every active break on
    the shared in-memory DB, so filter by worker_id to ignore prior tests' rows."""
    return [c for c in capture if c.get("worker_id") == user.id]


def test_short_break_no_alert_until_grace_passed(db, full_time, capture_hr_email, capture_employee_email):
    """Short break: 15m allowance + 10m grace. NO alert at 24m; first alert at 25m.
    Alert goes to the employee; HR/admin are never notified."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    base = datetime.now(timezone.utc)

    def _emp_mine():
        return [c for c in capture_employee_email if c.get("employee_email") == full_time.email]

    # 24 min — over the 15m allowance but still inside the 10m grace → NO email.
    brk.break_start = base - timedelta(minutes=24)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    db.refresh(brk)
    assert brk.overrun_flag is True            # crossing detected (timer keeps counting)
    assert brk.overrun_notification_sent is False
    assert _emp_mine() == []
    assert len(capture_hr_email) == 0

    # 25 min — effective_limit (15 + 10) reached → first alert fires, not a reminder.
    brk.break_start = base - timedelta(minutes=25)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    mine = _emp_mine()
    assert len(mine) == 1
    assert mine[0]["is_reminder"] is False
    assert mine[0]["allowed_minutes"] == 15
    assert len(capture_hr_email) == 0


def test_lunch_break_no_alert_until_grace_passed(db, full_time, capture_hr_email, capture_employee_email):
    """Lunch break: 60m allowance + 10m grace. NO alert at 69m; first alert at 70m.
    Alert goes to the employee; HR/admin are never notified."""
    clock_in(db=db, user=full_time)
    emp_start_break(body=_StartBreakBody(break_type="lunch"), db=db, user=full_time)
    brk = _active_break(db, full_time)
    base = datetime.now(timezone.utc)

    def _emp_mine():
        return [c for c in capture_employee_email if c.get("employee_email") == full_time.email]

    brk.break_start = base - timedelta(minutes=69)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    db.refresh(brk)
    assert brk.overrun_flag is True
    assert brk.overrun_notification_sent is False
    assert _emp_mine() == []
    assert len(capture_hr_email) == 0

    brk.break_start = base - timedelta(minutes=70)
    db.commit()
    break_monitor.process_break_overruns(db, now=base)
    mine = _emp_mine()
    assert len(mine) == 1
    assert mine[0]["is_reminder"] is False
    assert mine[0]["allowed_minutes"] == 60
    assert len(capture_hr_email) == 0


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — SUPERVISOR VIEW (Test 6)
# ══════════════════════════════════════════════════════════════════════════════
def test_6_supervisor_sees_active_overrun(db, capture_hr_email):
    admin = _mk_user(db, "full_time", role="admin")
    worker = _mk_user(db, "full_time")
    clock_in(db=db, user=worker)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=worker)
    brk = _active_break(db, worker)
    # Past the new 15 + 10 = 25m effective limit.
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()
    break_monitor.process_break_overruns(db, now=datetime.now(timezone.utc))

    view = get_active_overruns(db=db, user=admin)
    assert view["count"] >= 1
    row = next(r for r in view["overruns"] if r["user_id"] == worker.id)
    assert row["indicator"] == "red"
    assert row["status_chip"] == "On Short Break (Exceeded)"
    assert row["exceeded_seconds"] > 0
