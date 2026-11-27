"""
ALERT — the durable ECO notification record/render layer.
=========================================================

The property under test: **a notification is recorded once per audience, from
a frozen source fact, and can never be rewritten.**

Everything runs the real pipeline. Events come from EVAL-EVENT through a real
week built by EVAL-DAY and EVAL-WEEK against a real stored threshold; no test
hand-stubs an event row where the service would read one, because a layer that
only works against hand-made facts has not been tested.

Literals are transcribed rather than imported wherever a wrong production
constant could otherwise agree with itself: the audience, the notification
types, the eight identity columns and the locked copy are all written out here.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.recipient_authority import EcoRecipientDesignation
from app.config_versioning import (
    ConfigValue,
    ConfigVersion,
    InMemoryConfigVersionProvider,
)
from app.eval_day import EcoDayRecord, evaluate_day, record_evaluation
from app.eval_event import (
    EcoThresholdEvent,
    EventMode,
    EventOutcome,
    evaluate_threshold_event,
    read_events,
)
from app.alert import (
    ALERT_ACTOR,
    AUTHORIZED_AUDIENCES,
    EcoNotification,
    EcoNotificationDeliveryAttempt,
    NotificationAudience,
    NotificationContentConflict,
    NotificationImmutable,
    NotificationOutcome,
    NotificationType,
    ProhibitedTerminology,
    RefusalReason,
    assert_clean,
    read_notifications,
    record_threshold_notification,
    render_body,
)
from app.requirement_resolution import RequirementCandidate
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

ORG = 141
USER = 8900
OTHER_USER = 8901

WEEK = "2026-08-31"          # a Monday; the canonical key IS this date (W4-3)
OTHER_WEEK = "2026-09-07"
DAYS_IN_WEEK = 7

REQUIRED_MINUTES = 525
THRESHOLD = 30

# ── Independently transcribed ────────────────────────────────────────────────
WORKER = "WORKER"
REACHED = "THRESHOLD_REACHED"
WITHDRAWN = "THRESHOLD_WITHDRAWN"
#: EVAL-EVENT's own event_type vocabulary — deliberately NOT the same strings
#: as the notification purposes above (owner ruling R3).
REACHED_EVENT = "ECO_THRESHOLD_REACHED"
WITHDRAWN_EVENT = "ECO_THRESHOLD_WITHDRAWN"
PRIMARY = "PRIMARY"
OPERATIONAL, SHADOW = "OPERATIONAL", "SHADOW"

#: The eight-column physical identity, transcribed. R3's six-field logical
#: episode identity is the first six; the last two are the separate axes.
IDENTITY_COLUMNS = [
    "organization_id", "user_id", "workweek_key", "tier", "mode", "sequence",
    "notification_type", "audience",
]
EPISODE_COLUMNS = IDENTITY_COLUMNS[:6]

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "alert"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "a8f4c2e91b07_eco_notification.py")
EXPECTED_REVISION = "a8f4c2e91b07"
EXPECTED_PARENT_REVISION = "f3d8a1c65b29"


# ═════════════════════════════════════════════════════════════════════════════
# Schema and seeding
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot
    from app.rollout import RolloutControl

    for model in (Organization, User, WorkDayLedger, WorkStateInterval,
                  EcoDayRecord, LeaveInputSnapshot, LeaveInputContribution,
                  ConfigVersion, ConfigValue, EcoThresholdEvent, EcoRecipientDesignation,
                  RolloutControl, EcoNotification,
                  EcoNotificationDeliveryAttempt):
        model.__table__.create(engine, checkfirst=True)


def _provider():
    p = InMemoryConfigVersionProvider()
    p.capture_version(
        organization_id=ORG, config_version_id="v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="SHIFT", rank=2,
                                         minutes=REQUIRED_MINUTES)])
    return p


def _store_threshold(db, minutes=THRESHOLD, version_key="t1"):
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )

    row = ConfigVersion(
        organization_id=ORG, version_key=version_key,
        effective_from=date(2026, 1, 1),
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="0" * 64)
    db.add(row)
    db.flush()
    db.add(ConfigValue(
        config_version_id=row.id, organization_id=ORG, scope_user_id=None,
        tier=ECO_THRESHOLD_TIER, rank=ECO_THRESHOLD_RANK,
        key=ECO_THRESHOLD_KEY, value_minutes=minutes))
    db.flush()


def _set_rollout(db, state, *, organization_id=ORG):
    """Write a rollout row directly.

    Deliberately not through ``transition``: that enforces an observation
    window and a transition graph this test has no business exercising, and
    ALERT only ever READS the state. Nothing here changes the repository's
    rollout default, which remains OFF — these rows live in a test database.
    """
    from app.rollout import RolloutControl

    existing = db.execute(
        sa.select(RolloutControl).where(
            RolloutControl.organization_id == organization_id,
            RolloutControl.control_key == CONTROL_EARLY_CLOCK_OUT_V2)
    ).scalars().first()
    if existing is not None:
        db.execute(sa.text(
            "UPDATE rollout_control SET state = :s WHERE id = :i"),
            {"s": state.value, "i": existing.id})
        db.flush()
        db.expire_all()
        return
    db.add(RolloutControl(
        organization_id=organization_id,
        control_key=CONTROL_EARLY_CLOCK_OUT_V2, state=state.value,
        state_version=1, last_transition_at=NOW, last_transition_by="TEST"))
    db.flush()


def _day(db, on, gross, *, week=WEEK, user_id=USER, version=1,
         predecessor=None):
    led = WorkDayLedger(
        organization_id=ORG, user_id=user_id,
        work_date=datetime(on.year, on.month, on.day), workweek_key=week,
        ledger_version=version,
        state=(LedgerState.SEALED.value if gross is not None
               else LedgerState.NOT_A_WORKING_DAY.value),
        timezone_iana="Asia/Kolkata", is_working_day=gross is not None,
        working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST")
    if predecessor is not None:
        led.supersedes = predecessor.id
    db.add(led)
    db.flush()
    if predecessor is not None:
        predecessor.superseded_by = led.id
        db.flush()

    intervals = []
    if gross is not None:
        secs = (REQUIRED_MINUTES - gross) * 60
        begin = datetime(on.year, on.month, on.day, 9, 0, tzinfo=UTC)
        iv = WorkStateInterval(
            ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
            start_at_utc=begin, end_at_utc=begin + timedelta(seconds=secs),
            duration_seconds=secs, provenance="WORKER", created_at=NOW)
        db.add(iv)
        db.flush()
        intervals = [iv]

    from app.eval_day import get_or_create_leave_snapshot

    ev = evaluate_day(ledger=led, intervals=intervals, provider=_provider())
    snap = get_or_create_leave_snapshot(
        db, organization_id=ORG, user_id=user_id, work_date=on,
        overlap_seconds=0, contributions=())
    record_evaluation(db, ev, leave_input_snapshot_key=snap.snapshot_key)
    db.flush()
    return led


def _week_dates(key=WEEK):
    start = date.fromisoformat(key)
    return [start + timedelta(days=i) for i in range(DAYS_IN_WEEK)]


def _build_week(db, grosses, *, week=WEEK, user_id=USER):
    for index, on in enumerate(_week_dates(week)):
        _day(db, on, grosses[index] if index < len(grosses) else None,
             week=week, user_id=user_id)


def _correct_first_day(db, gross, *, version, week=WEEK):
    start = date.fromisoformat(week)
    previous = db.execute(sa.select(WorkDayLedger).where(
        WorkDayLedger.organization_id == ORG,
        WorkDayLedger.work_date == datetime(start.year, start.month, start.day),
        WorkDayLedger.superseded_by.is_(None))).scalars().one()
    return _day(db, start, gross, week=week, version=version,
                predecessor=previous)


def _cross(db, *, week=WEEK, mode=EventMode.OPERATIONAL, user_id=USER):
    """A real crossing event for a real week. Returns the event row."""
    result = evaluate_threshold_event(
        db, organization_id=ORG, user_id=user_id, workweek_key=week, mode=mode)
    assert result.outcome is EventOutcome.CROSSED, result
    return db.get(EcoThresholdEvent, result.event_id)


def _rows(db, *, week=WEEK, user_id=USER):
    return list(db.execute(
        sa.select(EcoNotification)
        .where(EcoNotification.organization_id == ORG)
        .where(EcoNotification.user_id == user_id)
        .where(EcoNotification.workweek_key == week)
        .order_by(EcoNotification.id)
    ).scalars().all())


@pytest.fixture
def enabled(db):
    """An ENABLED tenant with a crossed week. Returns the crossing event."""
    _set_rollout(db, RolloutState.ENABLED)
    _store_threshold(db)
    _build_week(db, [20, 10, 0, 0, 0])
    return _cross(db)


def _tokens(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docs.add(doc)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docs:
                out.add(node.value)
    return out


def _package_files():
    return sorted(PACKAGE.glob("*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# A — recording, idempotency and conflict
# ═════════════════════════════════════════════════════════════════════════════

class TestRecording:

    def test_a_crossing_records_exactly_one_worker_notification(self, db,
                                                                enabled):
        result = record_threshold_notification(db, event_id=enabled.id)
        assert result.outcome is NotificationOutcome.RECORDED
        assert result.notification_type is NotificationType.THRESHOLD_REACHED
        assert result.audience is NotificationAudience.WORKER

        rows = _rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.notification_type == REACHED
        assert row.audience == WORKER
        assert row.mode == OPERATIONAL
        assert row.tier == PRIMARY
        assert row.sequence == 1
        assert row.originating_event_id == enabled.id
        assert row.created_by == ALERT_ACTOR

    def test_repeated_processing_is_idempotent(self, db, enabled):
        first = record_threshold_notification(db, event_id=enabled.id)
        for _ in range(5):
            again = record_threshold_notification(db, event_id=enabled.id)
            assert again.outcome is NotificationOutcome.ALREADY_RECORDED
            assert again.notification_id == first.notification_id
        assert len(_rows(db)) == 1

    def test_same_identity_same_content_returns_the_existing_row(self, db,
                                                                 enabled):
        first = record_threshold_notification(db, event_id=enabled.id)
        stored = _rows(db)[0]
        frozen = stored.content_hash
        again = record_threshold_notification(db, event_id=enabled.id)
        assert again.outcome is NotificationOutcome.ALREADY_RECORDED
        assert again.notification_id == first.notification_id
        db.expire_all()
        assert _rows(db)[0].content_hash == frozen

    def test_same_identity_different_content_raises_a_typed_conflict(self, db,
                                                                     enabled):
        """Neither returning nor overwriting would be truthful.

        The divergent row is INSERTED rather than produced by mutating a
        recorded one — the table is append-only, so even a test cannot rewrite
        history to set this up. That constraint is the point.
        """
        db.add(EcoNotification(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL, sequence=1,
            notification_type=REACHED, audience=WORKER,
            originating_event_id=enabled.id,
            originating_event_type=enabled.event_type,
            event_provenance={}, recipient_kind=WORKER,
            recipient_user_id=USER, recipient_snapshot={},
            rendered_body="a materially different body",
            rendered_copy_keys=[],
            eligibility_outcome="ELIGIBLE_TO_RECORD",
            rollout_state="ENABLED", content_hash="b" * 64,
            created_by="TEST"))
        db.flush()

        with pytest.raises(NotificationContentConflict):
            record_threshold_notification(db, event_id=enabled.id)

    def test_an_unknown_event_refuses(self, db):
        result = record_threshold_notification(db, event_id=999_999)
        assert result.outcome is NotificationOutcome.REFUSED
        assert result.refusal is RefusalReason.EVENT_NOT_FOUND
        assert result.notification_id is None

    def test_alert_never_creates_a_threshold_event(self, db, enabled):
        before = db.execute(sa.text(
            "SELECT count(*) FROM eco_threshold_event")).scalar()
        record_threshold_notification(db, event_id=enabled.id)
        after = db.execute(sa.text(
            "SELECT count(*) FROM eco_threshold_event")).scalar()
        assert after == before


# ═════════════════════════════════════════════════════════════════════════════
# B — identity: eight physical columns, six logical ones
# ═════════════════════════════════════════════════════════════════════════════

class TestIdentity:

    def test_the_physical_identity_is_the_eight_documented_columns(self):
        constraint = next(
            c for c in EcoNotification.__table__.constraints
            if getattr(c, "name", None) == "uq_eco_notification_identity")
        assert [c.name for c in constraint.columns] == IDENTITY_COLUMNS

    def test_the_logical_episode_identity_is_unchanged(self):
        """R3's six fields, and audience is NOT among them."""
        assert EPISODE_COLUMNS == [
            "organization_id", "user_id", "workweek_key", "tier", "mode",
            "sequence"]
        assert "audience" not in EPISODE_COLUMNS
        assert "notification_type" not in EPISODE_COLUMNS

    def test_type_and_audience_are_separate_columns(self):
        names = {c.name for c in EcoNotification.__table__.columns}
        assert "notification_type" in names
        assert "audience" in names

    def test_the_eight_column_uniqueness_is_database_enforced(self, db,
                                                              enabled):
        record_threshold_notification(db, event_id=enabled.id)
        stored = _rows(db)[0]
        duplicate = EcoNotification(
            organization_id=stored.organization_id, user_id=stored.user_id,
            workweek_key=stored.workweek_key, tier=stored.tier,
            mode=stored.mode, sequence=stored.sequence,
            notification_type=stored.notification_type,
            audience=stored.audience,
            originating_event_id=stored.originating_event_id,
            originating_event_type=stored.originating_event_type,
            event_provenance={}, recipient_kind=WORKER,
            recipient_user_id=USER, recipient_snapshot={},
            rendered_body="x", rendered_copy_keys=[],
            eligibility_outcome="ELIGIBLE_TO_RECORD",
            rollout_state="ENABLED", content_hash="c" * 64,
            created_by="TEST")
        db.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_second_audience_is_permitted_by_the_schema(self, db, enabled):
        """The model must support audiences independently — even though the
        SERVICE refuses to create them. Database permits; service declines."""
        record_threshold_notification(db, event_id=enabled.id)
        stored = _rows(db)[0]
        db.add(EcoNotification(
            organization_id=stored.organization_id, user_id=stored.user_id,
            workweek_key=stored.workweek_key, tier=stored.tier,
            mode=stored.mode, sequence=stored.sequence,
            notification_type=stored.notification_type,
            audience="ADMINISTRATOR",
            originating_event_id=stored.originating_event_id,
            originating_event_type=stored.originating_event_type,
            event_provenance={}, recipient_kind="ADMINISTRATOR",
            recipient_user_id=1, recipient_snapshot={},
            rendered_body="future", rendered_copy_keys=[],
            eligibility_outcome="ELIGIBLE_TO_RECORD",
            rollout_state="ENABLED", content_hash="d" * 64,
            created_by="TEST"))
        db.flush()

        rows = _rows(db)
        assert len(rows) == 2
        # The episode is unchanged by the extra audience.
        assert {tuple(getattr(r, c) for c in EPISODE_COLUMNS)
                for r in rows} == {
            (ORG, USER, WEEK, PRIMARY, OPERATIONAL, 1)}

    def test_worker_suppression_cannot_reach_an_administrator_row(self, db,
                                                                  enabled):
        """Audience rows are independently durable (R1).

        Deleting a Worker row is impossible at all — the table is append-only —
        so an administrator row cannot be collaterally affected by anything
        that happens to the Worker's.
        """
        record_threshold_notification(db, event_id=enabled.id)
        worker_row = _rows(db)[0]
        db.add(EcoNotification(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL, sequence=1,
            notification_type=REACHED, audience="ADMINISTRATOR",
            originating_event_id=worker_row.originating_event_id,
            originating_event_type=worker_row.originating_event_type,
            event_provenance={}, recipient_kind="ADMINISTRATOR",
            recipient_user_id=1, recipient_snapshot={},
            rendered_body="future", rendered_copy_keys=[],
            eligibility_outcome="ELIGIBLE_TO_RECORD",
            rollout_state="ENABLED", content_hash="e" * 64,
            created_by="TEST"))
        db.flush()

        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.text("DELETE FROM eco_notification WHERE id = :i"),
                       {"i": worker_row.id})
        db.rollback()

    def test_a_re_crossing_is_a_new_episode_and_a_new_notification(self, db):
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        crossing = _cross(db)
        record_threshold_notification(db, event_id=crossing.id)

        _correct_first_day(db, 10, version=2)          # -10 -> weekly 20
        withdrawal = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        assert withdrawal.outcome is EventOutcome.WITHDRAWN
        record_threshold_notification(db, event_id=withdrawal.event_id)

        _correct_first_day(db, 22, version=3)          # +12 -> weekly 32
        recross = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        assert recross.outcome is EventOutcome.RE_CROSSED
        assert recross.sequence == 2
        record_threshold_notification(db, event_id=recross.event_id)

        rows = _rows(db)
        assert [(r.notification_type, r.sequence, r.audience) for r in rows] \
            == [(REACHED, 1, WORKER), (WITHDRAWN, 1, WORKER),
                (REACHED, 2, WORKER)]


# ═════════════════════════════════════════════════════════════════════════════
# C — withdrawal (GV-07 shape)
# ═════════════════════════════════════════════════════════════════════════════

class TestWithdrawal:

    @pytest.fixture
    def withdrawn(self, db):
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        crossing = _cross(db)
        record_threshold_notification(db, event_id=crossing.id)
        _correct_first_day(db, 10, version=2)
        result = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        assert result.outcome is EventOutcome.WITHDRAWN
        return crossing, db.get(EcoThresholdEvent, result.event_id)

    def test_a_withdrawal_records_the_withdrawn_notification_type(self, db,
                                                                  withdrawn):
        _crossing, withdrawal_event = withdrawn
        result = record_threshold_notification(db,
                                               event_id=withdrawal_event.id)
        assert result.outcome is NotificationOutcome.RECORDED
        assert result.notification_type is NotificationType.THRESHOLD_WITHDRAWN

    def test_the_withdrawal_stays_in_the_original_logical_episode(self, db,
                                                                  withdrawn):
        crossing, withdrawal_event = withdrawn
        record_threshold_notification(db, event_id=withdrawal_event.id)
        rows = _rows(db)
        assert len(rows) == 2
        reached, withdrawal = rows
        for column in EPISODE_COLUMNS:
            assert getattr(withdrawal, column) == getattr(reached, column), (
                f"{column} differs; the withdrawal left the episode")

    def test_the_withdrawal_references_the_withdrawal_event(self, db,
                                                            withdrawn):
        _crossing, withdrawal_event = withdrawn
        record_threshold_notification(db, event_id=withdrawal_event.id)
        withdrawal_row = _rows(db)[1]
        assert withdrawal_row.originating_event_id == withdrawal_event.id
        assert withdrawal_row.originating_event_type == \
            "ECO_THRESHOLD_WITHDRAWN"

    def test_the_withdrawal_uses_the_frozen_original_recipient(self, db,
                                                               withdrawn):
        """GV-07's "all original recipients" — never re-resolved."""
        crossing, withdrawal_event = withdrawn
        record_threshold_notification(db, event_id=withdrawal_event.id)
        reached, withdrawal = _rows(db)
        assert withdrawal.recipient_snapshot == reached.recipient_snapshot
        assert withdrawal.recipient_user_id == reached.recipient_user_id
        assert withdrawal.recipient_user_id == USER

    def test_the_recipient_is_not_re_resolved_from_mutable_data(self, db,
                                                                withdrawn):
        """Changing the user's current address must not move the recipient."""
        crossing, withdrawal_event = withdrawn
        db.execute(sa.text("UPDATE users SET email = :e WHERE id = :i"),
                   {"e": "moved@example.test", "i": USER})
        db.flush()
        record_threshold_notification(db, event_id=withdrawal_event.id)
        withdrawal = _rows(db)[1]
        assert withdrawal.recipient_user_id == USER
        assert "moved@example.test" not in json.dumps(
            withdrawal.recipient_snapshot)


# ═════════════════════════════════════════════════════════════════════════════
# D — worker only; no administrator or reviewer inference
# ═════════════════════════════════════════════════════════════════════════════

class TestWorkerOnly:

    def test_only_worker_is_authorized(self):
        assert AUTHORIZED_AUDIENCES == {WORKER}
        assert [a.value for a in NotificationAudience] == [WORKER]

    def test_no_administrator_row_is_created_by_the_service(self, db, enabled):
        record_threshold_notification(db, event_id=enabled.id)
        assert {r.audience for r in _rows(db)} == {WORKER}

    def test_an_unauthorized_audience_is_refused(self, db, enabled):
        """Constructed below the enum, because the enum has no other member —
        which is itself the point: the refusal is not the only guard."""
        class _Fake(str):
            value = "ADMINISTRATOR"

        result = record_threshold_notification(
            db, event_id=enabled.id, audience=_Fake("ADMINISTRATOR"))
        assert result.outcome is NotificationOutcome.REFUSED
        assert result.refusal is RefusalReason.AUDIENCE_NOT_AUTHORIZED
        assert _rows(db) == []

    def test_rendering_refuses_a_non_worker_audience(self):
        class _Fake(str):
            value = "ADMINISTRATOR"

        with pytest.raises(ValueError):
            render_body(
                audience=_Fake("ADMINISTRATOR"),
                notification_type=NotificationType.THRESHOLD_REACHED,
                workweek_key=WEEK, weekly_total_minutes=30,
                threshold_minutes=30, is_retroactive=False, caused_by={})

    def test_no_reviewer_or_admin_resolver_is_introduced(self):
        for path in _package_files():
            tokens = {t.lower() for t in _tokens(path)}
            for banned in ("reviewer", "designated_reviewer", "approver",
                           "hr_email", "admin_email", "alert_recipients",
                           "recipients_for_user", "super_admin"):
                assert banned not in tokens, f"{path.name} names {banned!r}"

    def test_the_recipient_is_never_an_address_alone(self, db, enabled):
        record_threshold_notification(db, event_id=enabled.id)
        row = _rows(db)[0]
        assert row.recipient_user_id == USER
        assert row.recipient_kind == WORKER
        assert "@" not in json.dumps(row.recipient_snapshot)

    def test_an_unresolvable_worker_fails_closed(self, db, enabled):
        """No substitute recipient is ever chosen.

        The snapshot-less event is INSERTED, not produced by editing a
        recorded one: ``eco_threshold_event`` is append-only too, so the only
        way to present this input is to append it. EVAL-EVENT never writes a
        row like this — which is why the guard is defence in depth rather than
        a reachable path.
        """
        orphan = EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=OTHER_WEEK,
            tier=PRIMARY, sequence=1, mode=OPERATIONAL,
            event_type="ECO_THRESHOLD_REACHED",
            weekly_total_minutes=50, threshold_minutes=THRESHOLD,
            is_retroactive=False, caused_by={}, week_provenance={},
            recipient_snapshot={},          # names no Worker
            worker_recipient=USER, created_by="TEST")
        db.add(orphan)
        db.flush()

        result = record_threshold_notification(db, event_id=orphan.id)
        assert result.outcome is NotificationOutcome.REFUSED
        assert result.refusal is RefusalReason.RECIPIENT_UNRESOLVED
        assert _rows(db, week=OTHER_WEEK) == []


# ═════════════════════════════════════════════════════════════════════════════
# E — rendering and the §31.1 gate
# ═════════════════════════════════════════════════════════════════════════════

class TestRenderingAndTerminology:

    def test_the_locked_status_string_is_rendered_verbatim(self, db, enabled):
        from app.early_clock_out import LOCKED_COPY

        record_threshold_notification(db, event_id=enabled.id)
        body = _rows(db)[0].rendered_body
        assert LOCKED_COPY["eco.status.threshold"] in body
        assert _rows(db)[0].rendered_copy_keys == ["eco.status.threshold"]

    def test_only_notification_surface_copy_is_used(self, db, enabled):
        """§31's WHERE column assigns eco.advisory.* to WF-ECO-02, a different
        surface. Quoting one here would place it where the spec did not."""
        from app.early_clock_out import LOCKED_COPY

        record_threshold_notification(db, event_id=enabled.id)
        body = _rows(db)[0].rendered_body
        for key in ("eco.advisory.title", "eco.advisory.provisional",
                    "eco.advisory.threshold"):
            assert LOCKED_COPY[key] not in body

    def test_the_final_body_passes_the_terminology_gate(self, db, enabled):
        from app.early_clock_out import find_prohibited_terms

        record_threshold_notification(db, event_id=enabled.id)
        assert find_prohibited_terms(_rows(db)[0].rendered_body) == []

    @pytest.mark.parametrize("term", [
        "violation", "breach", "infraction", "offence", "misconduct",
        "unauthorised", "non-compliant", "failure", "offender", "owed",
        "debt", "deficit", "penalty", "warning", "strike",
    ])
    def test_prohibited_terminology_is_rejected(self, term):
        with pytest.raises(ProhibitedTerminology):
            assert_clean(f"Threshold reached. This is a {term}.")

    def test_the_disclaimer_exemption_covers_only_the_exact_string(self):
        """It must not launder the body merely by appearing in it."""
        from app.early_clock_out import LOCKED_COPY

        disclaimer = LOCKED_COPY["eco.admin.disclaimer"]
        # The disclaimer alone passes when exempted.
        assert_clean(disclaimer, exempt=(disclaimer,))
        # The same body plus an independent prohibited term still fails.
        with pytest.raises(ProhibitedTerminology):
            assert_clean(disclaimer + "\n\nThis is a breach.",
                         exempt=(disclaimer,))

    def test_only_a_registered_locked_string_may_be_exempted(self):
        with pytest.raises(ValueError):
            assert_clean("anything", exempt=("not a registered string",))

    def test_the_disclaimer_is_dormant_for_worker_rows(self, db, enabled):
        from app.early_clock_out import LOCKED_COPY

        record_threshold_notification(db, event_id=enabled.id)
        assert LOCKED_COPY["eco.admin.disclaimer"] not in \
            _rows(db)[0].rendered_body

    def test_locked_copy_is_never_rewritten_in_the_package(self):
        """The package must quote the register, not restate it."""
        from app.early_clock_out import LOCKED_COPY

        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            for key, value in LOCKED_COPY.items():
                assert value not in source, (
                    f"{path.name} inlines the locked string {key!r} instead "
                    f"of reading it from the register")


# ═════════════════════════════════════════════════════════════════════════════
# F — rollout (D8)
# ═════════════════════════════════════════════════════════════════════════════

class TestRollout:

    def test_off_records_nothing(self, db):
        _set_rollout(db, RolloutState.OFF)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        event = _cross(db)

        result = record_threshold_notification(db, event_id=event.id)
        assert result.outcome is NotificationOutcome.REFUSED
        assert result.refusal is RefusalReason.ROLLOUT_OFF
        assert _rows(db) == []

    def test_a_missing_control_row_is_off(self, db):
        """OFF is the answer to every uncertainty — no row means OFF."""
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        event = _cross(db)
        result = record_threshold_notification(db, event_id=event.id)
        assert result.refusal is RefusalReason.ROLLOUT_OFF

    def test_shadow_records_a_retained_shadow_row(self, db):
        _set_rollout(db, RolloutState.SHADOW)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        event = _cross(db, mode=EventMode.SHADOW)

        result = record_threshold_notification(db, event_id=event.id)
        assert result.outcome is NotificationOutcome.RECORDED
        row = _rows(db)[0]
        assert row.mode == SHADOW
        assert row.rollout_state == "SHADOW"

    def test_shadow_refuses_an_operational_event(self, db):
        """A SHADOW rollout may have no operational effect."""
        _set_rollout(db, RolloutState.SHADOW)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        event = _cross(db, mode=EventMode.OPERATIONAL)

        result = record_threshold_notification(db, event_id=event.id)
        assert result.outcome is NotificationOutcome.REFUSED
        assert result.refusal is \
            RefusalReason.OPERATIONAL_UNDER_SHADOW_ROLLOUT
        assert _rows(db) == []

    def test_shadow_rows_are_excluded_from_an_operational_read(self, db):
        _set_rollout(db, RolloutState.SHADOW)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        event = _cross(db, mode=EventMode.SHADOW)
        record_threshold_notification(db, event_id=event.id)

        operational = read_notifications(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            mode=OPERATIONAL)
        shadow = read_notifications(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            mode=SHADOW)
        assert operational == ()
        assert len(shadow) == 1

    def test_no_delivery_attempt_is_ever_created(self, db, enabled):
        """SHADOW and ENABLED alike: this increment delivers nothing."""
        record_threshold_notification(db, event_id=enabled.id)
        assert db.execute(sa.text(
            "SELECT count(*) FROM eco_notification_delivery_attempt"
        )).scalar() == 0

    def test_alert_reads_the_existing_control_key(self):
        assert CONTROL_EARLY_CLOCK_OUT_V2 == "early_clock_out_v2"
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "CONTROL_EARLY_CLOCK_OUT_V2" in source
        assert "early_clock_out_v2" not in source, (
            "the control key must be imported, not restated")

    def test_alert_never_writes_rollout_state(self):
        for path in _package_files():
            tokens = _tokens(path)
            for banned in ("transition", "set_canary", "set_observation_days"):
                assert banned not in tokens, f"{path.name} writes rollout"


# ═════════════════════════════════════════════════════════════════════════════
# G — immutability and concurrency
# ═════════════════════════════════════════════════════════════════════════════

class TestImmutabilityAndConcurrency:

    def test_core_update_is_refused_below_the_orm(self, db, enabled):
        record_threshold_notification(db, event_id=enabled.id)
        row = _rows(db)[0]
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            db.execute(sa.text(
                "UPDATE eco_notification SET rendered_body = 'x' WHERE id=:i"),
                {"i": row.id})
        assert "append-only" in str(excinfo.value)
        db.rollback()

    def test_core_delete_is_refused_below_the_orm(self, db, enabled):
        record_threshold_notification(db, event_id=enabled.id)
        row = _rows(db)[0]
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.text("DELETE FROM eco_notification WHERE id=:i"),
                       {"i": row.id})
        db.rollback()

    def test_orm_update_and_delete_are_refused(self, db, enabled):
        record_threshold_notification(db, event_id=enabled.id)
        row = db.get(EcoNotification, _rows(db)[0].id)
        row.rendered_body = "rewritten"
        with pytest.raises(NotificationImmutable):
            db.flush()
        db.rollback()

    def test_delivery_attempts_are_append_only(self, db, enabled):
        """Restated for the notification-platform foundation, which gave this
        table a vocabulary it did not have.

        The placeholder row used ``outcome="NOT_ATTEMPTED"`` and updated it to
        ``'X'``, both chosen when the table was inert and nothing constrained
        the column. D-2 now pins the vocabulary to ``dispatched | failed``, so
        the old UPDATE would raise on the CHECK rather than on the append-only
        trigger — passing for the wrong reason.

        Both values are therefore VALID now, which makes the claim stronger:
        the row is refused because it is append-only, and for no other reason.
        """
        record_threshold_notification(db, event_id=enabled.id)
        notification = _rows(db)[0]
        attempt = EcoNotificationDeliveryAttempt(
            notification_id=notification.id, attempt_seq=1,
            recipient_kind=WORKER, recipient_user_id=USER,
            channel="email", destination=None, mode=OPERATIONAL,
            outcome="failed")
        db.add(attempt)
        db.flush()
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.text(
                "UPDATE eco_notification_delivery_attempt "
                "SET outcome='dispatched' WHERE id=:i"), {"i": attempt.id})
        db.rollback()

    def test_an_attempt_belongs_to_exactly_one_notification(self):
        fks = [fk for c in EcoNotificationDeliveryAttempt.__table__.columns
               for fk in c.foreign_keys]
        targets = {fk.column.table.name for fk in fks}
        assert targets == {"eco_notification"}

    def test_there_is_no_updated_at_column(self):
        names = {c.name for c in EcoNotification.__table__.columns}
        assert "updated_at" not in names and "updated_at_utc" not in names

    def test_a_lost_insert_race_returns_the_winner(self, db, enabled,
                                                  monkeypatch):
        """The competitor committed between this run's read and its insert."""
        from app.alert import service as svc

        winner = record_threshold_notification(db, event_id=enabled.id)
        real = svc._existing
        calls = {"n": 0}

        def blind_once(*args, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*args, **kwargs)

        monkeypatch.setattr(svc, "_existing", blind_once)
        loser = record_threshold_notification(db, event_id=enabled.id)

        assert loser.outcome is NotificationOutcome.ALREADY_RECORDED
        assert loser.notification_id == winner.notification_id
        assert calls["n"] >= 2, "the recovery path must re-read the winner"
        assert len(_rows(db)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# H — boundary guards (§17)
# ═════════════════════════════════════════════════════════════════════════════

class TestBoundaries:

    def test_no_legacy_notification_infrastructure_is_imported(self):
        banned = ("app.notifications", "app.events", "app.email_service",
                  "app.attendance_alerts", "app.commercial")
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for name in names:
                    assert not any(name.startswith(b) for b in banned), \
                        f"{path.name} imports {name}"

    def test_no_delivery_or_retry_vocabulary(self):
        banned = ("send", "send_email", "smtp", "resend", "websocket",
                  "broadcast", "push", "outbox", "dead_letter", "max_retries",
                  "retry_count", "backoff", "jitter", "celery", "apscheduler",
                  "scheduler", "APIRouter", "router", "endpoint")
        for path in _package_files():
            tokens = _tokens(path)
            for name in banned:
                assert name not in tokens, f"{path.name} names {name!r}"

    def test_no_retry_policy_numbers_are_invented(self):
        """R6 leaves limits, windows and backoff unspecified on purpose."""
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        name = getattr(target, "id", "") or ""
                        assert not any(
                            k in name.upper()
                            for k in ("RETRY", "ATTEMPT_LIMIT", "BACKOFF",
                                      "MAX_ATTEMPTS", "RETRY_WINDOW")), name

    def test_no_horizon_logic_is_introduced(self):
        for path in _package_files():
            tokens = {t.lower() for t in _tokens(path)}
            for banned in ("horizon", "within_horizon", "horizon_days",
                           "retroactive_horizon"):
                assert banned not in tokens, f"{path.name} names {banned!r}"

    def test_no_auditlog_or_legacy_table_is_used(self):
        for path in _package_files():
            tokens = _tokens(path)
            for banned in ("AuditLog", "risk_alerts", "RiskAlert",
                           "EmailSuppression"):
                assert banned not in tokens, f"{path.name} names {banned!r}"

    def test_no_eco_v1_path_is_reactivated(self):
        for path in _package_files():
            tokens = {t.lower() for t in _tokens(path)}
            for banned in ("early_clock_out_alert", "eco_v1", "ecov1",
                           "attendance.early_clockout"):
                assert banned not in tokens

    def test_the_service_never_commits(self):
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "commit"

    def test_alert_does_not_evaluate_anything(self):
        """EVAL-EVENT remains the source fact; ALERT consumes an id."""
        tokens = _tokens(PACKAGE / "service.py")
        for banned in ("evaluate_threshold_event", "evaluate_workweek",
                       "evaluate_day", "resolve_threshold"):
            assert banned not in tokens
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "event_id: int" in source

    def test_the_ddl_module_imports_nothing(self):
        tree = ast.parse(
            (PACKAGE / "append_only_ddl.py").read_text(encoding="utf-8"))
        modules = [n.module for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom)]
        assert modules == ["__future__"]

    def test_the_notification_type_mapping_is_explicit(self):
        """R3: EVAL-EVENT's event_type is not substituted for the purpose."""
        from app.alert import NOTIFICATION_TYPE_FOR_EVENT
        from app.eval_event import EventType

        assert NOTIFICATION_TYPE_FOR_EVENT == {
            EventType.ECO_THRESHOLD_REACHED.value:
                NotificationType.THRESHOLD_REACHED,
            EventType.ECO_THRESHOLD_WITHDRAWN.value:
                NotificationType.THRESHOLD_WITHDRAWN,
        }
        # The vocabularies are related but NOT equal.
        assert {t.value for t in NotificationType} != \
            {t.value for t in EventType}


# ═════════════════════════════════════════════════════════════════════════════
# I — migration
# ═════════════════════════════════════════════════════════════════════════════

class TestMigration:

    def test_the_revision_and_parent_are_pinned(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        found = {}
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                              ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            if target in ("revision", "down_revision") \
                    and isinstance(node.value, ast.Constant):
                found[target] = node.value.value
        assert found["revision"] == EXPECTED_REVISION
        assert found["down_revision"] == EXPECTED_PARENT_REVISION

    def test_the_graph_has_one_head_and_one_base(self):
        """One head, one base — and ALERT's revision still on the only chain.

        The head is no longer ALERT's own: MODEL-WORKWEEK's b2e6d9f04a17
        chained onto it for the R-12.3.4 / §35 attribution column. So the
        claim is stated as it was always meant — the graph is linear and
        ALERT's revision is an ancestor of the tip — rather than by pinning a
        head that every later increment moves.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        heads = list(sd.get_heads())
        assert len(heads) == 1, heads
        assert list(sd.get_bases()) == ["81c63dee733a"]
        ancestry = {r.revision for r in sd.walk_revisions(base="base",
                                                          head=heads[0])}
        assert EXPECTED_REVISION in ancestry, (
            "the ALERT migration is no longer on the migration chain")

    def test_the_migration_is_purely_additive(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        for banned in ("drop_table", "drop_column", "alter_column",
                       "rename_table"):
            assert banned not in called

    def test_the_migration_emits_the_shared_append_only_ddl(self):
        source = MIGRATION.read_text(encoding="utf-8")
        assert "from app.alert.append_only_ddl import" in source
        assert "append_only_statements(dialect)" in source
        assert "attempt_append_only_statements(dialect)" in source

    def test_migration_and_model_agree_and_round_trip(self):
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        probe = sa.create_engine("sqlite://")
        Organization.__table__.create(probe, checkfirst=True)
        User.__table__.create(probe, checkfirst=True)
        EcoThresholdEvent.__table__.create(probe, checkfirst=True)

        spec = importlib.util.spec_from_file_location("_alert_rev", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with probe.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()
            conn.commit()

            migrated = {c["name"] for c in
                        sa.inspect(conn).get_columns("eco_notification")}
            triggers = set(conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )).scalars().all())
            assert migrated == {c.name for c in
                                EcoNotification.__table__.columns}
            assert {"eco_notification_no_update", "eco_notification_no_delete",
                    "eco_notification_attempt_no_update",
                    "eco_notification_attempt_no_delete"} <= triggers

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.downgrade()
                names = set(sa.inspect(conn).get_table_names())
                assert "eco_notification" not in names
                assert "eco_notification_delivery_attempt" not in names
                module.upgrade()
                assert "eco_notification" in set(
                    sa.inspect(conn).get_table_names())
            conn.rollback()

    def test_the_tables_compile_for_postgresql(self):
        from sqlalchemy.dialects import postgresql

        for model in (EcoNotification, EcoNotificationDeliveryAttempt):
            ddl = sa.schema.CreateTable(model.__table__).compile(
                dialect=postgresql.dialect()).string
            assert "ON DELETE RESTRICT" in ddl

    def test_only_the_two_supported_dialects_are_protected(self):
        from app.alert import SUPPORTED_DIALECTS

        assert SUPPORTED_DIALECTS == {"sqlite", "postgresql"}


# ═════════════════════════════════════════════════════════════════════════════
# J — golden-vector shapes (NOT promoted; see the promotion audit)
# ═════════════════════════════════════════════════════════════════════════════

class TestGoldenVectorShapes:
    """The behaviour GV-06, GV-07 and GV-22 describe, exercised here.

    These are NOT the promotion harnesses: the vectors remain blocked and
    their metadata is untouched. Promotion is a separate, audited increment.
    """

    def test_gv06_shape_one_logical_episode_one_worker_notification(self, db):
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])
        crossing = _cross(db)
        record_threshold_notification(db, event_id=crossing.id)

        # Further shortfalls to 36 and 44: EVAL-EVENT writes no new event.
        _correct_first_day(db, 26, version=2)
        assert evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER,
            workweek_key=WEEK).outcome is EventOutcome.ALREADY_CROSSED
        _correct_first_day(db, 34, version=3)
        assert evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER,
            workweek_key=WEEK).outcome is EventOutcome.ALREADY_CROSSED

        record_threshold_notification(db, event_id=crossing.id)

        rows = _rows(db)
        reached = [r for r in rows if r.notification_type == REACHED]
        # ONE logical episode, ONE audience-specific durable record.
        assert len({tuple(getattr(r, c) for c in EPISODE_COLUMNS)
                    for r in reached}) == 1
        assert len([r for r in reached if r.audience == WORKER]) == 1

    def test_gv22_shape_renders_workweek_and_cause_without_a_horizon(self, db):
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [2, 2, 0, 0, 0])            # weekly 4, below T
        assert evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER,
            workweek_key=WEEK).outcome is EventOutcome.NOT_CROSSED

        _correct_first_day(db, 23, version=2)       # -> weekly 25... then more
        _correct_first_day(db, 31, version=3)       # -> weekly 33
        result = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        assert result.outcome is EventOutcome.CROSSED
        assert result.weekly_total_minutes == 33

        event = db.get(EcoThresholdEvent, result.event_id)
        assert event.is_retroactive is True

        record_threshold_notification(db, event_id=event.id)
        body = _rows(db)[0].rendered_body
        assert WEEK in body, "the notification must name the workweek"
        assert "re-evaluated" in body.lower(), "it must name the cause"


# ═════════════════════════════════════════════════════════════════════════════
# K — scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_golden_vector_artifact_is_untouched(self):
        import hashlib

        path = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = path.read_bytes()
        digest = hashlib.sha1(
            f"blob {len(data)}\0".encode() + data).hexdigest()
        # Re-pinned by the PROMOTION of GV-18. Its ALERT citation was STALE
        # rather than satisfied: a requirement-unresolved day emits no
        # threshold event, so this package's entry point could never have
        # fired for it — the same error ruling B3 corrected for GV-17. Ruling
        # 2: B authorized a separate day-scoped primitive instead, and ALERT
        # itself is untouched by the promotion. Harness-only: exactly one
        # vector changed, the count moved 26/2 -> 27/1, and both normative
        # projections are byte-for-byte unchanged.
        # Re-pinned by the PROMOTION of GV-26. Its STATE-CONTRACT citation
        # was satisfied by the existing D-8.1 contract — LedgerState owns
        # ledger facts and EcoDayState stays the seven-member evaluation
        # vocabulary, so no evaluation state was added — and its ALERT
        # citation was STALE: §24.15 asks for a platform INCIDENT, which has
        # no audience, no recipient, no channel and no delivery, so ALERT's
        # entry point could never have fired for it either. Owner ruling
        # D-9.1 authorized app.eco_ledger_incident. Harness-only: exactly
        # one vector changed, the count moved 27/1 -> 28/0, and the
        # normative projection is byte-for-byte unchanged.
        assert digest == "2a7a0fb34145fe7fd210db585cc44f198974a364"

    def test_the_promoted_vectors_crossed_while_gv26_did_not(self):
        """The promotion boundary, pinned from this side too.

        GV-06 and GV-22 needed only ALERT's record/render half. GV-23 needed
        MODEL-HORIZON. GV-07 needed ALERT-DELIVERY, which the notification
        platform, the persist-first processing step and the operational
        command together supplied. Each crossed when ITS blocker shipped —
        which is what makes the remaining one meaningful: GV-18 waits on the
        administrator audience, and no amount of delivery machinery moves it.
        """
        data = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))
        by_id = {v["id"]: v for v in data["vectors"]}

        for gid in ("GV-06", "GV-07", "GV-18", "GV-22", "GV-23"):
            harness = by_id[gid]["harness"]
            assert harness["execution"] == "executable", gid
            assert harness["blocking_dependencies"] == [], gid

        # GV-18 has since crossed too, and NOT through this package: its ALERT
        # citation was stale, because a requirement-unresolved day emits no
        # threshold event and this package's entry point could never have
        # fired for it. GV-26 then crossed on the same kind of reading —
        # §24.15 asks for a platform INCIDENT, which has no audience, no
        # recipient and no delivery — under owner ruling D-9.1. ALERT's own
        # record/render half is untouched by any of the three promotions, and
        # the blocked set is now EMPTY.
        assert by_id["GV-18"]["harness"]["execution"] == "executable"
        harness = by_id["GV-26"]["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []

    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_no_rollout_seed_row_is_shipped(self):
        """The package must not turn ECO on for anyone."""
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            assert "RolloutControl(" not in source
            assert "ENABLED" not in _tokens(path)


# ═════════════════════════════════════════════════════════════════════════════
# GOLDEN VECTORS — GV-06 and GV-22, executed
# ═════════════════════════════════════════════════════════════════════════════
# Executed HERE for the same reason GV-12 lives in the week-fold suite and
# GV-28 in the classifier suite: the behaviour these vectors pin is ALERT's,
# and the fixtures that build a real week, a real threshold, a real event and a
# real notification already exist in this file.
#
# EVERY expected value is READ FROM THE ARTIFACT at test time and never
# restated, and each test asserts exhaustiveness, so a field added to a vector
# fails here instead of being ignored into a green build.
#
# NOTHING IS RECOMPUTED. The week comes from EVAL-DAY/EVAL-WEEK, the event from
# EVAL-EVENT, the notification from ALERT. There is no arithmetic and no
# parallel implementation in these tests.
#
# THESE VECTORS ARE STILL BLOCKED. A harness is a precondition of promotion,
# not promotion itself: the artifact is untouched, and
# ``TestScopeHeld::test_gv06_gv07_gv22_remain_blocked`` pins that.

GOLDEN_PATH = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"


def _vector(gid):
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return next(v for v in data["vectors"] if v["id"] == gid)


def _exhaustive(gid, expected, handled):
    unhandled = set(expected) - set(handled)
    assert not unhandled, (
        f"{gid} carries expected_fields this harness never compared: "
        f"{sorted(unhandled)}")


def _episode_of(row):
    """The six-field LOGICAL episode identity of a notification row.

    Built from the row's own columns so the test fails if ``audience`` or
    ``notification_type`` ever leaks into the episode: those are read
    separately, below, and must never appear in this tuple.
    """
    return tuple(getattr(row, column) for column in EPISODE_COLUMNS)


class TestGV06:
    """GV-06, executed: "Deduplication".

    §21.2: the crossing is created by a NATURAL KEY, not by a "has notified"
    boolean, so a replay cannot create a second. This harness proves that at
    both layers — EVAL-EVENT writes no second event, and ALERT records no
    second notification — and keeps the three counts the vector depends on
    distinct: logical episodes, notification purposes, and audience-specific
    durable rows.
    """

    def _scenario(self, db):
        """GV-04's crossing, then the further shortfalls to 36 and 44."""
        expected = _vector("GV-06")["expected_fields"]
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [20, 10, 0, 0, 0])          # weekly 30 == T
        crossing = _cross(db)
        first = record_threshold_notification(db, event_id=crossing.id)
        assert first.outcome is NotificationOutcome.RECORDED

        _correct_first_day(db, 26, version=2)       # weekly 36
        second = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        _correct_first_day(db, 34, version=3)       # weekly 44
        third = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        return expected, crossing, first, second, third

    def test_gv06_deduplication(self, db):
        expected, crossing, first, second, third = self._scenario(db)

        # Replay the SAME event through ALERT after the further shortfalls.
        replay = record_threshold_notification(db, event_id=crossing.id)

        events = read_events(db, organization_id=ORG, user_id=USER,
                             workweek_key=WEEK)
        notifications = read_notifications(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            mode=OPERATIONAL)
        primaries = [e for e in events if e.event_type == REACHED_EVENT]
        reached = [n for n in notifications
                   if n.notification_type == REACHED]
        handled = []

        # "Exactly one primary event" — the further shortfalls produced no new
        # event at all, which is why no second notification can exist.
        assert len(primaries) == expected["primary_event_count"]
        assert (second.outcome, third.outcome) == (
            EventOutcome.ALREADY_CROSSED, EventOutcome.ALREADY_CROSSED)
        handled.append("primary_event_count")

        # "No further notifications" — counted beyond the first, over a REAL
        # notification table that already holds one row. This is a positive
        # measurement, not the absence of a notification concept.
        assert len(reached) - 1 == expected["further_notifications"]
        handled.append("further_notifications")

        _exhaustive("GV-06", expected, handled)

        # ── the three counts the vector depends on, kept distinct ──────────
        episodes = {_episode_of(n) for n in notifications}
        assert len(episodes) == 1, f"more than one logical episode: {episodes}"
        assert {n.notification_type for n in notifications} == {REACHED}
        assert len(notifications) == 1
        assert replay.outcome is NotificationOutcome.ALREADY_RECORDED
        assert replay.notification_id == first.notification_id

    def test_gv06_audience_is_not_part_of_the_logical_episode(self, db):
        """A second audience must NOT read as a second episode.

        Inserted directly, because the service refuses to create a non-WORKER
        row (R2) — the point is that the identity model keeps them distinct
        even when two audiences exist.
        """
        self._scenario(db)
        worker_row = _rows(db)[0]
        db.add(EcoNotification(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=worker_row.tier, mode=OPERATIONAL, sequence=1,
            notification_type=REACHED, audience="ADMINISTRATOR",
            originating_event_id=worker_row.originating_event_id,
            originating_event_type=worker_row.originating_event_type,
            event_provenance={}, recipient_kind="ADMINISTRATOR",
            recipient_user_id=1, recipient_snapshot={},
            rendered_body="a future audience", rendered_copy_keys=[],
            eligibility_outcome="ELIGIBLE_TO_RECORD", rollout_state="ENABLED",
            content_hash="f" * 64, created_by="TEST"))
        db.flush()

        rows = _rows(db)
        assert len(rows) == 2
        # Two audience-specific records, ONE logical episode.
        assert len({_episode_of(r) for r in rows}) == 1
        assert {r.audience for r in rows} == {WORKER, "ADMINISTRATOR"}
        assert "audience" not in EPISODE_COLUMNS
        assert "notification_type" not in EPISODE_COLUMNS

    def test_gv06_dedup_is_durable_not_in_memory(self, db, engine):
        """A different Session reaches the same answer.

        In-memory suppression would forget across sessions; the constraint and
        the re-read do not.
        """
        from sqlalchemy.orm import Session

        expected, crossing, first, _second, _third = self._scenario(db)
        db.flush()
        with Session(engine) as fresh:
            again = record_threshold_notification(fresh, event_id=crossing.id)
            assert again.outcome is NotificationOutcome.ALREADY_RECORDED
            assert again.notification_id == first.notification_id
            # Counted INSIDE the fresh session. The shared in-memory engine
            # hands both sessions one connection, so closing this one would
            # roll back the outer session's uncommitted rows — an artifact of
            # the fixture, not of the service.
            assert len(read_notifications(
                fresh, organization_id=ORG, user_id=USER, workweek_key=WEEK,
                mode=OPERATIONAL)) == 1

    def test_gv06_physical_identity_carries_type_and_audience(self, db):
        """Losing either column would let a duplicate through."""
        constraint = next(
            c for c in EcoNotification.__table__.constraints
            if getattr(c, "name", None) == "uq_eco_notification_identity")
        columns = [c.name for c in constraint.columns]
        assert columns == IDENTITY_COLUMNS
        assert columns[:6] == EPISODE_COLUMNS
        assert columns[6:] == ["notification_type", "audience"]

    def test_gv06_audience_is_worker(self, db):
        self._scenario(db)
        assert {r.audience for r in _rows(db)} == {WORKER}
        assert AUTHORIZED_AUDIENCES == {WORKER}


class TestGV22:
    """GV-22, executed: "Retroactive crossing (§21.5)".

    A correction two weeks later pushes week k over the threshold. The
    notification must name the workweek and the cause, and §21.5 requires it
    not to read as though the condition arose today.

    Executable WITHOUT MODEL-HORIZON: the horizon governs whether anyone may
    be told (D3), which is a DELIVERY decision. Rendering happens at record
    creation, before any such decision, so every field this vector asserts is
    a property of the immutable record.
    """

    def _scenario(self, db):
        expected = _vector("GV-22")["expected_fields"]
        _set_rollout(db, RolloutState.ENABLED)
        _store_threshold(db)
        _build_week(db, [2, 2, 0, 0, 0])            # weekly 4 — below T
        assert evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER,
            workweek_key=WEEK).outcome is EventOutcome.NOT_CROSSED

        # The correction, arriving later, that pushes the week to 33.
        _correct_first_day(db, 23, version=2)
        _correct_first_day(db, 31, version=3)
        result = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        assert result.outcome is EventOutcome.CROSSED
        event = db.get(EcoThresholdEvent, result.event_id)
        recorded = record_threshold_notification(db, event_id=event.id)
        assert recorded.outcome is NotificationOutcome.RECORDED
        return expected, result, event, _rows(db)[0]

    def test_gv22_retroactive_crossing(self, db):
        expected, result, event, note = self._scenario(db)
        body = note.rendered_body
        handled = []

        assert result.weekly_total_minutes == expected["weekly_total_minutes"]
        handled.append("weekly_total_minutes")

        assert event.is_retroactive is expected["is_retroactive"]
        handled.append("is_retroactive")

        # The workweek comes from the event's own identity, not from a clock.
        assert (event.workweek_key in body) is \
            expected["notification_names_workweek"]
        handled.append("notification_names_workweek")

        # The cause comes from the lineage EVAL-EVENT froze into caused_by.
        lineage = event.caused_by or {}
        assert lineage.get("corrected_day_record_ids"), \
            "the event carries no correction lineage to name"
        names_cause = ("re-evaluated" in body.lower()
                       and "corrected" in body.lower())
        assert names_cause is expected["notification_names_cause"]
        handled.append("notification_names_cause")

        _exhaustive("GV-22", expected, handled)

        # §21.5: it must not read as though the condition arose today.
        for today_ish in ("today", "just now", "this morning"):
            assert today_ish not in body.lower()

    def test_gv22_provenance_is_frozen_on_the_notification(self, db):
        """The body is not the only record of why — the lineage travels."""
        _expected, _result, event, note = self._scenario(db)
        assert note.event_provenance["is_retroactive"] is True
        assert note.event_provenance["caused_by"] == dict(event.caused_by)
        assert note.originating_event_id == event.id

    def test_gv22_needs_no_horizon(self, db):
        """MODEL-HORIZON independence, asserted on the package itself.

        The scenario above already renders with no horizon configured
        anywhere; this pins that no horizon lookup, calculation, import or
        identifier can be introduced without failing here.
        """
        for path in _package_files():
            tokens = {t.lower() for t in _tokens(path)}
            for banned in ("horizon", "within_horizon", "horizon_days",
                           "retroactive_horizon", "days_elapsed"):
                assert banned not in tokens, f"{path.name} names {banned!r}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                for module in modules:
                    assert "horizon" not in module.lower(), module

    def test_gv22_renders_before_any_delivery_and_delivers_nothing(self, db):
        """Rendering is a property of record creation, not of delivery.

        If rendering were deferred into a delivery path it would sit behind a
        notification-eligibility decision that D3 makes the horizon's — and the
        horizon does not exist. The body is present with zero attempts made.
        """
        _expected, _result, _event, note = self._scenario(db)
        assert note.rendered_body, "the body exists at record creation"
        assert note.rendered_copy_keys == ["eco.status.threshold"]
        attempts = db.execute(sa.text(
            "SELECT count(*) FROM eco_notification_delivery_attempt")).scalar()
        assert attempts == 0, "this increment delivers nothing"
