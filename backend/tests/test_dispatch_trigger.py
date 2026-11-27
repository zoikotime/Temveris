"""
The authorized D-C trigger.
===========================

Owner ruling D-C: B, trigger authorized separately. Proves the rollout gate in
all three states, the on-demand cadence, tenant scope, and — from several
directions — that the trigger adds no capability beyond gating: it selects
nothing, resolves nothing, chooses no channel, suppresses nothing, retries
nothing and records nothing.
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
    EcoNotification,
    EcoNotificationDeliveryAttempt,
    run_eco_dispatch_pass,
)
from app.alert import dispatch_trigger
from app.eval_event import EcoThresholdEvent
from app.notification_platform import NotificationDispatchRequest
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutControl, RolloutState

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MODULE = BACKEND / "app" / "alert" / "dispatch_trigger.py"

ORG = 8300
OTHER_ORG = 8400
WORKER = 83001
OTHER_WORKER = 84001
WEEK = "2026-W36"

_SEQ = {"n": 0}
_H = {"n": 0}


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import AuditLog, Organization, User

    for model in (Organization, User, AuditLog, EcoThresholdEvent,
                  EcoNotification, EcoNotificationDeliveryAttempt,
                  NotificationDispatchRequest, EcoDayAdministratorAlert,
                  EcoDayAlertDeliveryAttempt, RolloutControl):
        model.__table__.create(engine, checkfirst=True)


@pytest.fixture
def seeded(db):
    from database.models import Organization, User

    # The append-only tables (R5) are deliberately NOT cleaned: a DELETE is
    # refused by design, and the D-F command suite commits rows that would
    # otherwise collide here. Isolation comes from this suite's own
    # organization instead, which is the scoping the processor already has.
    db.execute(sa.delete(NotificationDispatchRequest))
    db.execute(sa.delete(RolloutControl))
    for org_id in (ORG, OTHER_ORG):
        if db.get(Organization, org_id) is None:
            db.add(Organization(id=org_id, name=f"org-{org_id}",
                                billing_type="standard"))
    db.flush()
    for uid, org_id in ((WORKER, ORG), (OTHER_WORKER, OTHER_ORG)):
        if db.get(User, uid) is None:
            db.add(User(id=uid, name=f"w{uid}", email=f"w{uid}.tr@example.test",
                        organization_id=org_id))
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


def _set_rollout(db, state, *, organization_id=ORG):
    db.execute(sa.delete(RolloutControl).where(
        RolloutControl.organization_id == organization_id))
    if state is not None:
        db.add(RolloutControl(organization_id=organization_id,
                              control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                              state=state.value,
                              last_transition_by="test"))
    db.flush()


def _work(db, *, organization_id=ORG, user_id=WORKER, mode="OPERATIONAL"):
    """One committed event plus the notification ALERT would have recorded."""
    _SEQ["n"] += 1
    _H["n"] += 1
    event = EcoThresholdEvent(
        organization_id=organization_id, user_id=user_id, workweek_key=WEEK,
        tier="PRIMARY", sequence=_SEQ["n"], mode=mode,
        event_type="ECO_THRESHOLD_REACHED", weekly_total_minutes=34,
        threshold_minutes=30, is_retroactive=False, caused_by={},
        week_provenance={},
        recipient_snapshot={"schema_version": 2, "worker_user_id": user_id,
                            "roles": ["WORKER"]},
        worker_recipient=user_id, created_by="test")
    db.add(event)
    db.flush()
    notification = EcoNotification(
        organization_id=organization_id, user_id=user_id, workweek_key=WEEK,
        tier="PRIMARY", mode=mode, sequence=int(event.sequence),
        notification_type="THRESHOLD_REACHED", audience="WORKER",
        originating_event_id=int(event.id),
        originating_event_type=event.event_type, event_provenance={},
        recipient_kind="WORKER", recipient_user_id=user_id,
        recipient_snapshot=dict(event.recipient_snapshot),
        rendered_body="Your working time reached the weekly review threshold.",
        rendered_copy_keys=["eco.advisory.threshold"],
        eligibility_outcome="ELIGIBLE_TO_RECORD", rollout_state="ENABLED",
        content_hash=f"{_H['n']:064x}", created_by="test")
    db.add(notification)
    db.flush()
    return event, notification


# ═════════════════════════════════════════════════════════════════════════════
# The rollout gate — the trigger's entire content
# ═════════════════════════════════════════════════════════════════════════════

class TestRolloutGate:

    def test_off_processes_nothing(self, seeded, sends):
        _work(seeded)
        _set_rollout(seeded, RolloutState.OFF)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.processed is False
        assert result.rollout_state == "OFF"
        assert result.result is None
        assert result.handoffs == 0
        assert sends == []

    def test_off_creates_no_delivery_record(self, seeded, sends):
        _work(seeded)
        _set_rollout(seeded, RolloutState.OFF)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert seeded.query(EcoNotificationDeliveryAttempt).count() == 0
        assert seeded.query(NotificationDispatchRequest).count() == 0

    def test_a_missing_control_row_is_off(self, seeded, sends):
        """OFF is the answer to every uncertainty, including no row at all."""
        _work(seeded)
        _set_rollout(seeded, None)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.rollout_state == "OFF"
        assert result.processed is False
        assert sends == []

    def test_shadow_never_reaches_the_platform(self, seeded, sends):
        _work(seeded, mode="SHADOW")
        _set_rollout(seeded, RolloutState.SHADOW)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.processed is False
        assert result.rollout_state == "SHADOW"
        assert sends == []
        assert seeded.query(NotificationDispatchRequest).count() == 0

    def test_shadow_creates_no_delivery_record(self, seeded, sends):
        _work(seeded, mode="SHADOW")
        _set_rollout(seeded, RolloutState.SHADOW)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert seeded.query(EcoNotificationDeliveryAttempt).count() == 0

    def test_shadow_reports_the_skip_rather_than_a_silent_zero(self, seeded,
                                                               sends):
        """"Declined" must stay distinguishable from "ran and found nothing"."""
        _work(seeded, mode="SHADOW")
        _set_rollout(seeded, RolloutState.SHADOW)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.result is None
        assert "SHADOW" in (result.detail or "")

    def test_shadow_reports_nobody_notified(self, seeded, sends):
        _work(seeded, mode="SHADOW")
        _set_rollout(seeded, RolloutState.SHADOW)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.handoffs == 0

    def test_enabled_runs_one_pass(self, seeded, sends):
        _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.processed is True
        assert result.rollout_state == "ENABLED"
        assert result.result is not None
        assert result.handoffs == 1
        assert len(sends) == 1

    def test_enabled_records_the_delivery(self, seeded, sends):
        _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert seeded.query(EcoNotificationDeliveryAttempt).count() == 1

    def test_the_trigger_does_not_change_rollout(self, seeded, sends):
        _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        row = seeded.query(RolloutControl).filter_by(
            organization_id=ORG).one()
        assert row.state == "ENABLED"

    def test_it_is_not_a_second_rollout_authority(self):
        """It READS the one control. It must not define states of its own."""
        source = MODULE.read_text(encoding="utf-8")
        assert "rollout_state(" in source
        for banned in ("class RolloutState", "ENABLED = ", "OFF = ",
                       "RolloutControl("):
            assert banned not in source, banned

    def test_the_processor_still_holds_no_rollout_authority(self):
        """The gate lives HERE, exactly once. D-C forbids a second one.

        An AST import scan, not a substring search: the processor's comments
        explain WHY the gate is not theirs, and prose that disclaims authority
        is the opposite of holding it. An import is what "consults the control"
        actually means, so the narrower test is the stronger one.
        """
        tree = ast.parse((BACKEND / "app" / "alert"
                          / "dispatch_processing.py").read_text(
                              encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "rollout" not in name, name


# ═════════════════════════════════════════════════════════════════════════════
# Cadence — on demand only
# ═════════════════════════════════════════════════════════════════════════════

class TestCadence:

    def test_one_invocation_is_one_pass(self, seeded, sends):
        for _ in range(55):
            _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.handoffs == 50
        assert result.result.truncated is True
        assert len(sends) == 50, "the trigger must not drain the remainder"

    def test_the_remainder_needs_another_invocation(self, seeded, sends):
        for _ in range(55):
            _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert run_eco_dispatch_pass(
            seeded, organization_id=ORG).handoffs == 5

    def test_the_trigger_contains_no_loop(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not isinstance(node, (ast.While, ast.For)), \
                "a loop here would be a drain or a scheduler"

    def test_the_processor_is_called_exactly_once_per_invocation(
            self, seeded, monkeypatch):
        calls = []
        monkeypatch.setattr(
            dispatch_trigger, "process_pending_notification_dispatches",
            lambda db, **kw: calls.append(kw) or _FakePass())
        _set_rollout(seeded, RolloutState.ENABLED)
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert len(calls) == 1

    def test_no_scheduler_or_async_machinery_is_imported(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for banned in ("asyncio", "sched", "apscheduler", "threading",
                               "celery", "redis"):
                    assert not name.startswith(banned), name


class _FakePass:
    handoffs = 0
    recorded = 0
    skipped_shadow = 0
    truncated = False
    outcomes = ()


# ═════════════════════════════════════════════════════════════════════════════
# Trigger input and tenant scope
# ═════════════════════════════════════════════════════════════════════════════

class TestTriggerInput:

    def test_the_caller_can_supply_nothing_but_the_tenant(self):
        import inspect

        params = list(inspect.signature(run_eco_dispatch_pass).parameters)
        assert params == ["db", "organization_id"]

    def test_no_forbidden_input_is_accepted(self):
        import inspect

        params = set(inspect.signature(run_eco_dispatch_pass).parameters)
        for banned in ("recipient_user_id", "channel", "provider",
                       "suppression", "preference", "correlation_id", "body",
                       "email", "phone", "rendered", "limit"):
            assert banned not in params, banned

    def test_the_tenant_is_required(self, seeded):
        with pytest.raises(ValueError):
            run_eco_dispatch_pass(seeded, organization_id=None)

    def test_tenant_isolation_is_preserved(self, seeded, sends):
        _work(seeded, organization_id=ORG, user_id=WORKER)
        _work(seeded, organization_id=OTHER_ORG, user_id=OTHER_WORKER)
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=ORG)
        _set_rollout(seeded, RolloutState.ENABLED,
                     organization_id=OTHER_ORG)

        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.handoffs == 1
        assert result.result.outcomes[0].recipient_user_id == WORKER
        assert len(sends) == 1

    def test_one_tenant_rollout_does_not_gate_another(self, seeded, sends):
        _work(seeded, organization_id=OTHER_ORG, user_id=OTHER_WORKER)
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=ORG)
        _set_rollout(seeded, RolloutState.OFF, organization_id=OTHER_ORG)
        assert run_eco_dispatch_pass(
            seeded, organization_id=OTHER_ORG).processed is False
        assert sends == []

    def test_it_introduces_no_second_tenant_resolution(self):
        """The organization is passed straight through to the processor."""
        source = MODULE.read_text(encoding="utf-8")
        assert "organization_id=organization_id" in source
        for banned in ("current_user", "Depends(", "get_current_user"):
            assert banned not in source


# ═════════════════════════════════════════════════════════════════════════════
# The trigger adds nothing but the gate
# ═════════════════════════════════════════════════════════════════════════════

class TestNoDuplicatedLogic:

    def test_it_duplicates_no_lower_layer_logic(self):
        """Scanned as CODE, not as text.

        The module's docstring names the things it refuses to do — recipients,
        channels, suppression, correlation ids — in order to refuse them, and a
        substring scan cannot tell a prohibition from an implementation. The
        identifiers a module actually uses can.
        """
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        identifiers |= {n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)}
        identifiers |= {n.name for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for banned in ("recipient_snapshot", "recipient_user_id",
                       "idempotency_key", "request_hash", "select_channel",
                       "Channel", "evaluate_send", "is_suppressed",
                       "correlation_id", "NotificationHandoff",
                       "EcoNotificationDeliveryAttempt", "EcoNotification"):
            assert banned not in identifiers, banned

    def test_it_never_reaches_the_platform_directly(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "notification_platform" not in name, name
                assert "email_service" not in name, name

    def test_it_declares_no_http_surface(self):
        source = MODULE.read_text(encoding="utf-8")
        for banned in ("APIRouter", "@router", "fastapi", "HTTPException"):
            assert banned not in source

    def test_the_architecture_chain_is_intact(self):
        """trigger -> processor -> handoff -> platform -> result -> record."""
        trigger = MODULE.read_text(encoding="utf-8")
        processor = (BACKEND / "app" / "alert"
                     / "dispatch_processing.py").read_text(encoding="utf-8")
        assert "process_pending_notification_dispatches" in trigger
        assert "from app.notification_platform import" in processor
        assert "email_service" not in trigger
        assert "email_service" not in processor

    def test_unexpected_exceptions_propagate(self, seeded, monkeypatch):
        """Swallowing one would hide a broken pass behind a report of zero."""
        def _boom(db, **kw):
            raise RuntimeError("processor exploded")

        monkeypatch.setattr(
            dispatch_trigger, "process_pending_notification_dispatches", _boom)
        _set_rollout(seeded, RolloutState.ENABLED)
        with pytest.raises(RuntimeError):
            run_eco_dispatch_pass(seeded, organization_id=ORG)


# ═════════════════════════════════════════════════════════════════════════════
# Event creation must never call the trigger
# ═════════════════════════════════════════════════════════════════════════════

class TestEventCreationIsIndependent:

    def test_eval_event_calls_neither_the_trigger_nor_the_platform(self):
        """The property D-C exists to guarantee."""
        for path in sorted((BACKEND / "app" / "eval_event").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for banned in ("run_eco_dispatch_pass", "dispatch_trigger",
                           "notification_platform",
                           "process_pending_notification_dispatches"):
                assert banned not in source, f"eval_event/{path.name}"

    def test_no_evaluation_or_ledger_path_calls_the_trigger(self):
        for package in ("eval_day", "eval_week", "work_ledger",
                        "work_ledger_producer", "clock_session_boundary"):
            root = BACKEND / "app" / package
            if not root.exists():
                continue
            for path in sorted(root.glob("*.py")):
                source = path.read_text(encoding="utf-8")
                for banned in ("run_eco_dispatch_pass", "dispatch_trigger"):
                    assert banned not in source, f"{package}/{path.name}"

    def test_a_threshold_event_is_unchanged_by_a_trigger_run(self, seeded,
                                                             sends):
        event, _ = _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        seeded.refresh(event)
        before = {c.name: getattr(event, c.name)
                  for c in EcoThresholdEvent.__table__.c}
        run_eco_dispatch_pass(seeded, organization_id=ORG)
        seeded.refresh(event)
        assert {c.name: getattr(event, c.name)
                for c in EcoThresholdEvent.__table__.c} == before

    def test_a_transport_failure_leaves_the_event_intact(self, seeded,
                                                          monkeypatch):
        import app.email_service as mailer

        monkeypatch.setattr(mailer, "_send", lambda **kw: False)
        event, _ = _work(seeded)
        _set_rollout(seeded, RolloutState.ENABLED)
        seeded.refresh(event)
        before = {c.name: getattr(event, c.name)
                  for c in EcoThresholdEvent.__table__.c}
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.result.outcomes[0].outcome == "failed"
        seeded.refresh(event)
        assert {c.name: getattr(event, c.name)
                for c in EcoThresholdEvent.__table__.c} == before


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_no_recurring_process_was_added(self):
        source = (BACKEND / "main.py").read_text(encoding="utf-8")
        assert source.count("run_forever") == 12
        for banned in ("run_eco_dispatch_pass", "dispatch_trigger",
                       "dispatch_processing"):
            assert banned not in source

    def test_no_route_exposes_the_trigger(self):
        """Nothing in the application CALLS the trigger; only the command does.

        Asserted by AST call/import scan rather than substring. The P0
        calculation driver NAMES this trigger in one comment, to state that
        delivery is a separate pass and that the driver never transports --
        which is the opposite of calling it. A text scan flagged that sentence,
        so the guard now looks at what a module actually does.
        """
        callers = []
        for path in (BACKEND / "app").rglob("*.py"):
            if path == MODULE or path.name == "__init__.py":
                continue
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            referenced = {getattr(n.func, "id", None)
                          or getattr(n.func, "attr", None)
                          for n in ast.walk(tree) if isinstance(n, ast.Call)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    referenced.update(a.name for a in node.names)
            if "run_eco_dispatch_pass" in referenced:
                callers.append(str(path.relative_to(BACKEND)))
        assert callers == [], f"unexpected caller: {callers}"

    def test_no_migration_was_added(self):
        versions = BACKEND / "alembic" / "versions"
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list(versions.glob("*.py"))) == 94

    def test_de_remains_untouched(self):
        register = (BACKEND / "app" / "early_clock_out"
                    / "copy_register.py").read_text(encoding="utf-8")
        assert "NOTIF-A-01" not in register

    def test_authorized_audiences_was_not_widened(self):
        from app.alert.outcomes import AUTHORIZED_AUDIENCES

        assert AUTHORIZED_AUDIENCES == frozenset({"WORKER"})

    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_no_rollout_is_enabled_by_default(self, seeded):
        """The trigger must not seed or flip a control."""
        assert seeded.query(RolloutControl).count() == 0

    def test_the_golden_vectors_are_untouched(self):
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
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
        # GV-07 was PROMOTED once ALERT-DELIVERY shipped. Asserted
        # positively rather than dropped: a vector leaving this list
        # must be accounted for, not merely stop being checked.
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []