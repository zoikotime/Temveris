"""
ALERT-DELIVERY — the D-C: B persist-first processing step.
==========================================================

Owner ruling D-C: B. Proves the ruled selection predicate, the hard batch
bound, the transaction boundary that keeps a transport failure away from the
threshold event, and — from several directions — that ECO still selects no
channel, suppresses nothing, retries nothing and sends nothing.

Also proves the negative the ruling is emphatic about: **no trigger exists.**
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa

from app.eco_day_alert import (
    EcoDayAdministratorAlert,
    EcoDayAlertDeliveryAttempt,
)
from app.alert import (
    MAX_HANDOFFS_PER_PASS,
    EcoNotification,
    EcoNotificationDeliveryAttempt,
    process_pending_notification_dispatches as run_pass,
)
from app.alert.dispatch_processing import COMPLETED_OUTCOME
from app.notification_platform import (
    Channel,
    DispatchOutcome,
    NotificationDispatchRequest,
    ReasonCode,
)
from app.eval_event import EcoThresholdEvent

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MODULE = BACKEND / "app" / "alert" / "dispatch_processing.py"

ORG = 8100
WORKER = 81001
ADMIN = 81002
WEEK = "2026-W36"


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import AuditLog, Organization, User

    for model in (Organization, User, AuditLog, EcoThresholdEvent,
                  EcoDayAdministratorAlert, EcoDayAlertDeliveryAttempt,
                  EcoNotification, EcoNotificationDeliveryAttempt,
                  NotificationDispatchRequest):
        model.__table__.create(engine, checkfirst=True)


@pytest.fixture
def seeded(db):
    from database.models import Organization, User

    # The append-only tables (R5) are deliberately NOT cleaned: a DELETE is
    # refused by design, and the D-F command suite commits rows that would
    # otherwise collide here. Isolation comes from this suite's own
    # organization instead, which is the scoping the processor already has.
    db.execute(sa.delete(NotificationDispatchRequest))
    if db.get(Organization, ORG) is None:
        db.add(Organization(id=ORG, name="org", billing_type="standard"))
    db.flush()
    for uid, name, email in ((WORKER, "Jordan Smith", "jordan.dc@example.test"),
                             (ADMIN, "Admin", "admin.dc@example.test")):
        if db.get(User, uid) is None:
            db.add(User(id=uid, name=name, email=email, organization_id=ORG))
    db.flush()
    return db


@pytest.fixture(autouse=True)
def _deterministic_quiet_hours(monkeypatch):
    """Pin quiet hours OFF for this suite.

    `DEFAULT_PREFS` enables quiet hours 20:00-07:00 UTC, so a WORKER-audience
    dispatch is suppressed for eleven hours a day and these tests would pass
    or fail by the clock. They are about dispatch MECHANICS; suppression has
    its own dedicated tests in `test_notification_platform.py`, which assert
    the quiet-hours path explicitly rather than depending on when they run.
    """
    import app.notifications.router as notif_router

    monkeypatch.setitem(notif_router.DEFAULT_PREFS, "quiet_hours_enabled",
                        False)


@pytest.fixture
def sends(monkeypatch):
    import app.email_service as mailer

    calls = []
    monkeypatch.setattr(mailer, "_send",
                        lambda **kw: (calls.append(kw.get("to")), True)[1])
    return calls


_SEQ = {"n": 0}


def _event(db, *, mode="OPERATIONAL", user_id=WORKER):
    _SEQ["n"] += 1
    row = EcoThresholdEvent(
        organization_id=ORG, user_id=user_id, workweek_key=WEEK,
        tier="PRIMARY", sequence=_SEQ["n"], mode=mode,
        event_type="ECO_THRESHOLD_REACHED", weekly_total_minutes=34,
        threshold_minutes=30, is_retroactive=False, caused_by={},
        week_provenance={},
        recipient_snapshot={"schema_version": 2, "worker_user_id": user_id,
                            "roles": ["WORKER"]},
        worker_recipient=user_id, created_by="test")
    db.add(row)
    db.flush()
    return row


def _notification(db, event, *, mode="OPERATIONAL", snapshot=None,
                  recipient=None, audience="WORKER"):
    recipient = recipient if recipient is not None else event.user_id
    row = EcoNotification(
        organization_id=ORG, user_id=event.user_id, workweek_key=WEEK,
        tier="PRIMARY", mode=mode, sequence=int(event.sequence),
        notification_type="THRESHOLD_REACHED", audience=audience,
        originating_event_id=int(event.id),
        originating_event_type=event.event_type, event_provenance={},
        recipient_kind="WORKER", recipient_user_id=recipient,
        recipient_snapshot=(snapshot if snapshot is not None
                            else dict(event.recipient_snapshot)),
        rendered_body="Your working time reached the weekly review threshold.",
        rendered_copy_keys=["eco.advisory.threshold"],
        eligibility_outcome="ELIGIBLE_TO_RECORD", rollout_state="ENABLED",
        content_hash=row_hash(), created_by="test")
    db.add(row)
    db.flush()
    return row


_H = {"n": 0}


def row_hash():
    """A unique, well-formed content hash: the model CHECKs length == 64."""
    _H["n"] += 1
    return f"{_H['n']:064x}"


def _attempts(db, notification_id=None):
    stmt = sa.select(EcoNotificationDeliveryAttempt)
    if notification_id is not None:
        stmt = stmt.where(EcoNotificationDeliveryAttempt.notification_id
                          == notification_id)
    return list(db.execute(stmt).scalars().all())


# ═════════════════════════════════════════════════════════════════════════════
# 1–6, 22. Selection predicate
# ═════════════════════════════════════════════════════════════════════════════

class TestSelection:

    def test_a_committed_notification_is_eligible(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event)
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 1
        assert result.recorded == 1
        assert len(sends) == 1

    def test_nothing_is_eligible_without_a_notification(self, seeded, sends):
        """ALERT decides WHETHER a notification exists; this step only
        delivers. An event alone is not work."""
        _event(seeded)
        assert run_pass(seeded, organization_id=ORG).handoffs == 0
        assert sends == []

    def test_a_missing_frozen_snapshot_is_not_dispatched(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event, snapshot={})
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 0
        assert sends == []
        assert _attempts(seeded) == []

    def test_a_snapshot_not_naming_the_recipient_is_not_dispatched(
            self, seeded, sends):
        """A snapshot that names somebody else is not authority for THIS
        recipient — and re-resolving to find one is exactly what D-3.7 bans."""
        event = _event(seeded)
        _notification(seeded, event, recipient=ADMIN,
                      snapshot={"schema_version": 2,
                                "worker_user_id": WORKER,
                                "roles": ["WORKER"]})
        assert run_pass(seeded, organization_id=ORG).handoffs == 0
        assert sends == []

    def test_an_audience_snapshot_names_the_recipient(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event, recipient=ADMIN, audience="PRIMARY_ADMIN",
                      snapshot={"schema_version": 2,
                                "worker_user_id": WORKER,
                                "audiences": [
                                    {"audience": "PRIMARY_ADMIN",
                                     "state": "RESOLVED",
                                     "user_ids": [ADMIN],
                                     "basis": "ECO_RECIPIENT_DESIGNATION"}]})
        assert run_pass(seeded, organization_id=ORG).handoffs == 1

    def test_a_completed_delivery_excludes_the_recipient(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event)
        run_pass(seeded, organization_id=ORG)
        assert len(sends) == 1
        second = run_pass(seeded, organization_id=ORG)
        assert second.handoffs == 0, "a dispatched recipient is not re-handed"
        assert len(sends) == 1

    def test_a_failed_recipient_can_be_handed_again(self, seeded,
                                                    monkeypatch):
        """D-C's replay example: recipient B may be handed to the platform
        again, and the PLATFORM decides what happens. ECO has no retry."""
        import app.email_service as mailer

        monkeypatch.setattr(mailer, "_send", lambda **kw: False)
        event = _event(seeded)
        _notification(seeded, event)
        first = run_pass(seeded, organization_id=ORG)
        assert first.outcomes[0].outcome == DispatchOutcome.FAILED.value

        second = run_pass(seeded, organization_id=ORG)
        assert second.handoffs == 1, "a failed recipient stays eligible"
        # D-4: the platform replays the original result, transporting nothing.
        assert second.outcomes[0].outcome == DispatchOutcome.FAILED.value
        assert second.outcomes[0].correlation_id == \
            first.outcomes[0].correlation_id

    def test_the_predicate_reads_only_persisted_facts(self):
        """No flag, no queue, no stored dispatch state — the predicate is a
        question asked of the event, the snapshot and the delivery records."""
        source = MODULE.read_text(encoding="utf-8")
        assert "EcoNotificationDeliveryAttempt.outcome == COMPLETED_OUTCOME" \
            in source
        assert COMPLETED_OUTCOME == "dispatched"

    def test_another_organization_can_be_excluded(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event)
        assert run_pass(seeded, organization_id=ORG + 1).handoffs == 0
        assert run_pass(seeded, organization_id=ORG).handoffs == 1


# ═════════════════════════════════════════════════════════════════════════════
# 5, 21. No recipient re-resolution
# ═════════════════════════════════════════════════════════════════════════════

class TestNoReResolution:

    def test_a_designation_change_does_not_affect_dispatch(self, seeded,
                                                            sends):
        from app.recipient_authority import EcoRecipientDesignation

        EcoRecipientDesignation.__table__.create(seeded.get_bind(),
                                                 checkfirst=True)
        event = _event(seeded)
        notification = _notification(seeded, event)
        seeded.add(EcoRecipientDesignation(
            organization_id=ORG, audience="PRIMARY_ADMIN", user_id=ADMIN))
        seeded.flush()

        result = run_pass(seeded, organization_id=ORG)
        assert result.outcomes[0].recipient_user_id == \
            notification.recipient_user_id
        assert result.outcomes[0].recipient_user_id != ADMIN

    def test_the_module_never_imports_recipient_authority(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "recipient_authority" not in name, name

    def test_the_recipient_comes_from_the_notification_row(self):
        source = MODULE.read_text(encoding="utf-8")
        assert "notification.recipient_user_id" in source
        assert "resolve_eco_recipients" not in source


# ═════════════════════════════════════════════════════════════════════════════
# 8, 9, 24. Batching
# ═════════════════════════════════════════════════════════════════════════════

class TestBatching:

    def test_the_bound_is_exactly_fifty(self):
        assert MAX_HANDOFFS_PER_PASS == 50

    def test_a_pass_never_exceeds_the_bound(self, seeded, sends):
        for _ in range(55):
            _notification(seeded, _event(seeded))
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 50
        assert result.truncated is True
        assert len(sends) == 50

    def test_the_remainder_is_left_for_a_later_invocation(self, seeded,
                                                          sends):
        for _ in range(55):
            _notification(seeded, _event(seeded))
        run_pass(seeded, organization_id=ORG)
        second = run_pass(seeded, organization_id=ORG)
        assert second.handoffs == 5
        assert second.truncated is False
        assert len(sends) == 55

    def test_exactly_fifty_is_not_reported_as_truncated(self, seeded, sends):
        for _ in range(50):
            _notification(seeded, _event(seeded))
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 50
        assert result.truncated is False

    def test_a_caller_cannot_raise_the_bound(self, seeded, sends):
        for _ in range(55):
            _notification(seeded, _event(seeded))
        assert run_pass(seeded, limit=1000).handoffs == 50

    def test_a_caller_may_narrow_the_bound(self, seeded, sends):
        for _ in range(5):
            _notification(seeded, _event(seeded))
        assert run_pass(seeded, limit=2).handoffs == 2

    def test_the_step_contains_no_loop_over_passes(self):
        """A pass processes one batch and RETURNS. Looping until empty would
        be the recurring behaviour D-C withheld."""
        source = MODULE.read_text(encoding="utf-8")
        assert "while " not in source


# ═════════════════════════════════════════════════════════════════════════════
# 10, 23. Outcomes and recording
# ═════════════════════════════════════════════════════════════════════════════

class TestRecording:

    def test_outcomes_are_independent_per_recipient(self, seeded,
                                                    monkeypatch):
        import app.email_service as mailer

        monkeypatch.setattr(
            mailer, "_send",
            lambda **kw: kw["to"] != "jordan.dc@example.test")
        _notification(seeded, _event(seeded, user_id=WORKER))
        admin_event = _event(seeded, user_id=ADMIN)
        _notification(seeded, admin_event)

        result = run_pass(seeded, organization_id=ORG)
        by_recipient = {o.recipient_user_id: o for o in result.outcomes}
        assert by_recipient[WORKER].outcome == DispatchOutcome.FAILED.value
        assert by_recipient[ADMIN].outcome == DispatchOutcome.DISPATCHED.value
        assert by_recipient[WORKER].correlation_id != \
            by_recipient[ADMIN].correlation_id

    def test_the_platform_result_is_recorded_verbatim(self, seeded, sends):
        event = _event(seeded)
        notification = _notification(seeded, event)
        result = run_pass(seeded, organization_id=ORG)

        attempt = _attempts(seeded, notification.id)[0]
        outcome = result.outcomes[0]
        assert attempt.outcome == outcome.outcome
        assert attempt.reason_code == outcome.reason_code
        assert attempt.channel == outcome.channel == Channel.EMAIL.value
        assert attempt.correlation_id == outcome.correlation_id
        assert attempt.mode == notification.mode
        assert attempt.recipient_user_id == notification.recipient_user_id
        assert attempt.attempted_at_utc is not None

    def test_the_destination_is_never_recorded(self, seeded, sends):
        """Under the handoff model ECO never learns the address; a value here
        could only come from a re-resolution."""
        _notification(seeded, _event(seeded))
        run_pass(seeded, organization_id=ORG)
        assert _attempts(seeded)[0].destination is None

    def test_a_no_channel_result_creates_no_delivery_record(self, seeded,
                                                             sends):
        """D-5.6: the ECO record identity includes a channel, so a result that
        never reached channel selection has no identity to be recorded under.
        Fabricating one would manufacture a delivery that never happened."""
        event = _event(seeded)
        _notification(seeded, event, audience="NOT_AN_AUDIENCE")
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 1
        assert result.recorded == 0
        assert result.outcomes[0].channel is None
        assert result.outcomes[0].reason_code == \
            ReasonCode.INVALID_HANDOFF.value
        assert _attempts(seeded) == []

    def test_attempts_are_appended_not_overwritten(self, seeded,
                                                   monkeypatch):
        import app.email_service as mailer

        monkeypatch.setattr(mailer, "_send", lambda **kw: False)
        notification = _notification(seeded, _event(seeded))
        run_pass(seeded, organization_id=ORG)
        run_pass(seeded, organization_id=ORG)
        rows = _attempts(seeded, notification.id)
        assert len(rows) == 2
        assert sorted(r.attempt_seq for r in rows) == [1, 2]


# ═════════════════════════════════════════════════════════════════════════════
# 16, 17. SHADOW
# ═════════════════════════════════════════════════════════════════════════════

class TestShadow:

    def test_shadow_produces_no_dispatch(self, seeded, sends):
        event = _event(seeded, mode="SHADOW")
        _notification(seeded, event, mode="SHADOW")
        result = run_pass(seeded, organization_id=ORG)
        assert result.handoffs == 0
        assert sends == []

    def test_shadow_produces_no_delivery_record(self, seeded, sends):
        _notification(seeded, _event(seeded, mode="SHADOW"), mode="SHADOW")
        run_pass(seeded, organization_id=ORG)
        assert _attempts(seeded) == []

    def test_shadow_creates_no_platform_idempotency_row(self, seeded, sends):
        _notification(seeded, _event(seeded, mode="SHADOW"), mode="SHADOW")
        run_pass(seeded, organization_id=ORG)
        assert seeded.query(NotificationDispatchRequest).count() == 0

    def test_shadow_is_excluded_before_the_boundary(self):
        """ECO owns the gate. The platform's SHADOW refusal is a backstop, not
        the thing that keeps a shadow row from dispatching."""
        source = MODULE.read_text(encoding="utf-8")
        assert "EcoNotification.mode != DispatchMode.SHADOW.value" in source

    def test_the_step_is_not_a_second_rollout_authority(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "rollout" not in name, name


# ═════════════════════════════════════════════════════════════════════════════
# 18, 19. The threshold event is untouched
# ═════════════════════════════════════════════════════════════════════════════

class TestEventIsUntouched:

    @staticmethod
    def _snapshot(db, event):
        """Read the row back from the database on BOTH sides of the pass.

        Refreshing first matters: SQLite drops tzinfo on read-back, so an
        in-memory value compared against a round-tripped one differs on
        ``created_at_utc`` alone and would fail for a reason that has nothing
        to do with mutation.
        """
        db.refresh(event)
        return {c.name: getattr(event, c.name)
                for c in EcoThresholdEvent.__table__.c}

    def test_the_threshold_event_is_unchanged_by_a_pass(self, seeded, sends):
        event = _event(seeded)
        _notification(seeded, event)
        before = self._snapshot(seeded, event)
        run_pass(seeded, organization_id=ORG)
        assert self._snapshot(seeded, event) == before

    def test_a_transport_failure_cannot_affect_the_event(self, seeded,
                                                          monkeypatch):
        import app.email_service as mailer

        def _explode(**kw):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(mailer, "_send", _explode)
        event = _event(seeded)
        _notification(seeded, event)
        before = self._snapshot(seeded, event)

        result = run_pass(seeded, organization_id=ORG)
        assert result.outcomes[0].outcome == DispatchOutcome.FAILED.value
        assert self._snapshot(seeded, event) == before
        assert seeded.query(EcoThresholdEvent).count() == 1

    def test_the_step_never_writes_to_the_event(self):
        source = MODULE.read_text(encoding="utf-8")
        for banned in ("EcoThresholdEvent(", "db.delete(",
                       "update(EcoThresholdEvent"):
            assert banned not in source


# ═════════════════════════════════════════════════════════════════════════════
# 11–15, 20. ECO owns nothing the platform owns
# ═════════════════════════════════════════════════════════════════════════════

class TestOwnershipHeld:

    def test_the_step_calls_the_platform_not_the_mailer(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        assert "app.notification_platform" in imported
        for banned in ("email_service", "smtplib", "resend", "app.notifications"):
            assert not any(banned in name for name in imported), banned

    def test_eco_selects_no_channel(self):
        source = MODULE.read_text(encoding="utf-8")
        assert "select_channel" not in source
        assert "Channel.EMAIL" not in source

    def test_eco_performs_no_suppression(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        identifiers |= {n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)}
        for banned in ("evaluate_send", "is_suppressed", "suppress",
                       "quiet_hours", "preferences"):
            assert banned not in identifiers, banned

    def test_eco_implements_no_retry(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        identifiers |= {n.name for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)}
        for banned in ("retry", "backoff", "attempts_remaining", "max_retries"):
            assert banned not in identifiers, banned

    def test_no_pending_or_queue_state_was_added(self):
        """The predicate is derived; there is nothing to fall out of step."""
        for model in (EcoNotification, EcoNotificationDeliveryAttempt,
                      EcoThresholdEvent):
            names = {c.name for c in model.__table__.c}
            for banned in ("dispatch_pending", "pending", "queued",
                           "queue_position", "dispatched_at", "sent",
                           "sent_at", "delivery_state", "dispatch_state"):
                assert banned not in names, f"{model.__tablename__}.{banned}"

    def test_no_second_idempotency_system_exists(self):
        source = MODULE.read_text(encoding="utf-8")
        assert "idempotency_key" not in source
        assert "request_hash" not in source

    def test_repeated_processing_is_safe(self, seeded, sends):
        for _ in range(3):
            _notification(seeded, _event(seeded))
        for _ in range(5):
            run_pass(seeded, organization_id=ORG)
        assert len(sends) == 3
        assert seeded.query(NotificationDispatchRequest).count() == 3

    def test_two_passes_cannot_create_conflicting_records(self, seeded,
                                                          monkeypatch):
        """Concurrency, on the existing constraints alone.

        No distributed lock is invented. Two records for one recipient cannot
        CONFLICT because D-4 guarantees both passes received the same platform
        result, and ``UNIQUE(notification_id, attempt_seq)`` stops two rows
        claiming the same attempt number.
        """
        import app.email_service as mailer

        monkeypatch.setattr(mailer, "_send", lambda **kw: False)
        notification = _notification(seeded, _event(seeded))
        run_pass(seeded, organization_id=ORG)
        run_pass(seeded, organization_id=ORG)
        rows = _attempts(seeded, notification.id)
        assert len({r.correlation_id for r in rows}) == 1
        assert len({r.outcome for r in rows}) == 1
        assert len({r.attempt_seq for r in rows}) == len(rows)


# ═════════════════════════════════════════════════════════════════════════════
# 25, 26. No trigger, no loop
# ═════════════════════════════════════════════════════════════════════════════

class TestNoTrigger:

    def test_exactly_one_authorized_trigger_invokes_the_step(self):
        """Restated: the trigger D-C withheld has since been authorized.

        This asserted that NOTHING invoked the step. One trigger —
        ``app/alert/dispatch_trigger.py`` — is now authorized, and it is the
        rollout gate the processor deliberately does not contain. The claim
        becomes the narrower one: that module is the only caller, so no
        second, ungated entry point can appear beside it.
        """
        callers = []
        for path in (BACKEND / "app").rglob("*.py"):
            if path == MODULE or path.name == "__init__.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8",
                                                errors="ignore"))
            except SyntaxError:  # pragma: no cover
                continue
            # CALLS, not text. `eco_day_alert/dispatch.py` names this function
            # in prose to explain why it does NOT widen it, and a substring
            # scan cannot tell an explanation from an invocation.
            called = {n.func.id for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)}
            if "process_pending_notification_dispatches" in called:
                callers.append(path.name)
        assert callers == ["dispatch_trigger.py"],             f"unexpected caller: {callers}"

    def test_main_does_not_invoke_it(self):
        source = (BACKEND / "main.py").read_text(encoding="utf-8")
        assert "process_pending_notification_dispatches" not in source
        assert "dispatch_processing" not in source

    def test_no_background_loop_was_added(self):
        source = (BACKEND / "main.py").read_text(encoding="utf-8")
        # The existing workers are untouched; none was added for this step.
        assert source.count("run_forever") == 12

    def test_the_step_declares_no_http_surface(self):
        source = MODULE.read_text(encoding="utf-8")
        for banned in ("APIRouter", "@router.", "fastapi", "Depends("):
            assert banned not in source

    def test_the_step_adds_no_scheduler(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for banned in ("asyncio", "sched", "apscheduler", "threading",
                               "celery"):
                    assert not name.startswith(banned), name


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_no_migration_was_added_by_this_step(self):
        versions = BACKEND / "alembic" / "versions"
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list(versions.glob("*.py"))) == 94

    def test_de_was_not_touched(self):
        register = (BACKEND / "app" / "early_clock_out"
                    / "copy_register.py").read_text(encoding="utf-8")
        assert "NOTIF-A-01" not in register

    def test_authorized_audiences_was_not_widened(self):
        from app.alert.outcomes import AUTHORIZED_AUDIENCES

        assert AUTHORIZED_AUDIENCES == frozenset({"WORKER"})

    def test_eco_v1_and_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_no_incident_work_leaked_in(self):
        source = MODULE.read_text(encoding="utf-8")
        for banned in ("eco_ledger_incident", "LedgerIncident", "GV-26"):
            assert banned not in source

    def test_the_golden_vectors_are_untouched(self):
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
        # GV-07 was PROMOTED once the whole chain — platform, this processing
        # step, and the operational command — satisfied ALERT-DELIVERY. Two
        # remain blocked, and naming them is stronger than looping over three:
        # it cannot keep passing silently if another vector regresses.
        # GV-26 was PROMOTED on §24.15's platform incident (ruling D-9.1);
        # its ALERT citation was stale, not satisfied — an incident is
        # operational evidence with no audience, recipient or delivery.
        # Asserted positively rather than dropped: the blocked set is now
        # EMPTY, and a vector re-entering it must be accounted for here.
        assert by_id["GV-26"]["harness"]["execution"] == "executable"
        assert by_id["GV-26"]["harness"]["blocking_dependencies"] == []
        # GV-18 was PROMOTED on the day-scoped primitive (ruling 2: B);
        # its ALERT citation was stale, not satisfied. Asserting it
        # positively means a vector leaving this list must be accounted for.
        assert by_id["GV-18"]["harness"]["execution"] == "executable"
        assert by_id["GV-18"]["harness"]["blocking_dependencies"] == []
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []
