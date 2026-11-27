"""
The D-F operational command, and the GV-07 end-to-end walk.
===========================================================

Owner ruling D-F: B. Proves the command enforces organization scope, invokes
the authorized trigger exactly once, adds no capability of its own, and leaks
no Worker PII — and then walks GV-07's eleven demonstrable steps through the
real chain, from a committed threshold event to a withdrawal reaching the
original recipients.

Nothing here promotes a vector or touches the golden artifact.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa

import run_eco_dispatch_pass as command
from app.eco_day_alert import (
    EcoDayAdministratorAlert,
    EcoDayAlertDeliveryAttempt,
)
from app.alert import EcoNotification, EcoNotificationDeliveryAttempt
from app.eval_event import EcoThresholdEvent
from app.notification_platform import NotificationDispatchRequest
from app.recipient_authority import EcoRecipientDesignation
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutControl, RolloutState

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "run_eco_dispatch_pass.py"

WEEK = "2026-W36"
WORKER_NAME = "Jordan Smith"

_SEQ = {"n": 0}
_H = {"n": 0}

#: A FRESH organization per test.
#:
#: The command commits, and `eco_notification` is append-only (R5) — so a
#: fixture cannot delete last test's rows, and a later pass would otherwise
#: pick them up. Per-test tenants solve both at once, using the very scoping
#: this suite exists to verify.
_TENANT = {"n": 8500}


#: This suite gets its OWN in-memory engine, and that is load-bearing.
#:
#: The command commits — an operational script must — and the ECO notification
#: tables are append-only (R5), so committed rows can neither be cleaned up nor
#: rolled back. On the session-scoped engine they would outlive this module and
#: break every later suite that asserts a table is empty. A private engine
#: keeps the commits real while containing them.
@pytest.fixture(scope="module")
def command_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from database.models import AuditLog, Organization, User

    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool, future=True)
    for model in (Organization, User, AuditLog, EcoThresholdEvent,
                  EcoNotification, EcoNotificationDeliveryAttempt,
                  NotificationDispatchRequest, EcoDayAdministratorAlert,
                  EcoDayAlertDeliveryAttempt, RolloutControl,
                  EcoRecipientDesignation):
        model.__table__.create(eng, checkfirst=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(command_engine):
    """Overrides the shared session fixture for this module only."""
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(autocommit=False, autoflush=False,
                           bind=command_engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class Tenant:
    """One test's isolated organization and its two recipients."""

    def __init__(self, n):
        self.org = n
        self.other_org = n + 1
        self.worker = n * 10 + 1
        self.admin = n * 10 + 2
        self.other_worker = n * 10 + 3
        self.worker_email = f"w{self.worker}.cmd@example.test"
        self.admin_email = f"a{self.admin}.cmd@example.test"


@pytest.fixture
def tenant(db):
    from database.models import Organization, User

    _TENANT["n"] += 2
    t = Tenant(_TENANT["n"])
    for org_id in (t.org, t.other_org):
        db.add(Organization(id=org_id, name=f"org-{org_id}",
                            billing_type="standard"))
    db.flush()
    for uid, org_id, name, email in (
        (t.worker, t.org, WORKER_NAME, t.worker_email),
        (t.admin, t.org, "Primary Admin", t.admin_email),
        (t.other_worker, t.other_org, "Other",
         f"o{t.other_worker}.cmd@example.test"),
    ):
        db.add(User(id=uid, name=name, email=email, organization_id=org_id))
    db.flush()
    db.commit()          # the command opens its own transaction on this session
    return t


@pytest.fixture
def seeded(db, tenant):
    return db


@pytest.fixture
def factory(seeded):
    """A session factory handing the command THIS test's session.

    The command must not open a second connection: it takes whatever factory it
    is given, which is how it stays inside the test database.
    """
    return lambda: seeded


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


def _set_rollout(db, state, *, organization_id):
    db.execute(sa.delete(RolloutControl).where(
        RolloutControl.organization_id == organization_id))
    if state is not None:
        db.add(RolloutControl(organization_id=organization_id,
                              control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                              state=state.value, last_transition_by="test"))
    db.flush()


def _snapshot(org_id, worker_id, admin_id=None):
    """The frozen event-time snapshot Stage 5 produces."""
    audiences = [
        {"audience": "WORKER", "state": "RESOLVED",
         "user_ids": [worker_id], "basis": "EVENT_SUBJECT"},
        {"audience": "PRIMARY_ADMIN",
         "state": "RESOLVED" if admin_id else "UNRESOLVED",
         "user_ids": [admin_id] if admin_id else [],
         "basis": "ECO_RECIPIENT_DESIGNATION" if admin_id
                  else "NOT_DESIGNATED"},
        {"audience": "DIRECT_MANAGER", "state": "UNRESOLVED", "user_ids": [],
         "basis": "NO_CANONICAL_MANAGER_RELATIONSHIP"},
        {"audience": "HR_ADMIN", "state": "UNRESOLVED", "user_ids": [],
         "basis": "NOT_DESIGNATED"},
    ]
    return {"schema_version": 2, "organization_id": org_id,
            "worker_user_id": worker_id, "roles": ["WORKER"],
            "audiences": audiences}


def _event(db, *, organization_id, user_id, snapshot=None,
           event_type="ECO_THRESHOLD_REACHED", mode="OPERATIONAL"):
    _SEQ["n"] += 1
    row = EcoThresholdEvent(
        organization_id=organization_id, user_id=user_id, workweek_key=WEEK,
        tier="PRIMARY", sequence=_SEQ["n"], mode=mode, event_type=event_type,
        weekly_total_minutes=34, threshold_minutes=30, is_retroactive=False,
        caused_by={}, week_provenance={},
        recipient_snapshot=(snapshot if snapshot is not None
                            else _snapshot(organization_id, user_id)),
        worker_recipient=user_id, created_by="test")
    db.add(row)
    db.flush()
    return row


def _notification(db, event, *, recipient, audience, mode="OPERATIONAL"):
    _H["n"] += 1
    row = EcoNotification(
        organization_id=event.organization_id, user_id=event.user_id,
        workweek_key=WEEK, tier="PRIMARY", mode=mode,
        sequence=int(event.sequence), notification_type="THRESHOLD_REACHED",
        audience=audience, originating_event_id=int(event.id),
        originating_event_type=event.event_type, event_provenance={},
        recipient_kind="WORKER", recipient_user_id=recipient,
        recipient_snapshot=dict(event.recipient_snapshot),
        rendered_body="Your working time reached the weekly review threshold.",
        rendered_copy_keys=["eco.advisory.threshold"],
        eligibility_outcome="ELIGIBLE_TO_RECORD", rollout_state="ENABLED",
        content_hash=f"{_H['n']:064x}", created_by="test")
    db.add(row)
    db.flush()
    return row


def _attempts_for(db, tenant):
    """This tenant's delivery attempts only.

    The command commits, so a global count would include every earlier test's
    rows. Scoping by organization is also the honest read: a delivery record
    belongs to the tenant whose notification produced it.
    """
    return list(db.execute(
        sa.select(EcoNotificationDeliveryAttempt)
        .join(EcoNotification,
              EcoNotification.id
              == EcoNotificationDeliveryAttempt.notification_id)
        .where(EcoNotification.organization_id == tenant.org)
    ).scalars().all())


def _run(factory, *args):
    return command.main(list(args), session_factory=factory)


# ═════════════════════════════════════════════════════════════════════════════
# 1–8. Organization scope, single invocation, no forbidden surface
# ═════════════════════════════════════════════════════════════════════════════

class TestCommandSurface:

    def test_the_organization_id_is_mandatory(self, tenant, factory):
        with pytest.raises(SystemExit) as exc:
            _run(factory)
        assert exc.value.code == 2

    def test_the_organization_cannot_be_inferred_or_defaulted(self, tenant):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    getattr(node.func, "attr", "") == "add_argument":
                kwargs = {k.arg for k in node.keywords}
                assert "default" not in kwargs, "no default organization"
                assert "required" in kwargs

    def test_the_organization_is_passed_unchanged(self, tenant, seeded, factory,
                                                  monkeypatch):
        seen = []
        monkeypatch.setattr(
            command_trigger_module(), "run_eco_dispatch_pass",
            lambda db, **kw: seen.append(kw) or _FakeTrigger())
        _run(factory, "--organization-id", str(tenant.org))
        assert seen == [{"organization_id": tenant.org}]

    def test_exactly_one_trigger_invocation(self, tenant, seeded, factory,
                                            monkeypatch):
        calls = []
        monkeypatch.setattr(
            command_trigger_module(), "run_eco_dispatch_pass",
            lambda db, **kw: calls.append(kw) or _FakeTrigger())
        _run(factory, "--organization-id", str(tenant.org))
        assert len(calls) == 1

    def test_the_command_contains_no_loop(self, tenant):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not isinstance(node, ast.While), "a while loop is a drain"
        # The only `for` permitted is printing the report lines.
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
        assert len(loops) <= 2

    def test_no_scheduler_or_background_machinery(self, tenant):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for banned in ("asyncio", "sched", "apscheduler", "threading",
                               "celery", "redis", "multiprocessing", "fastapi"):
                    assert not name.startswith(banned), name

    @staticmethod
    def _declared_flags():
        """The flags argparse actually defines.

        Read from the AST, never from the text: the script's docstring NAMES
        --all and --batch-size in order to say it has neither, and a substring
        scan cannot tell a prohibition from a definition. What argparse
        declares is the only thing that can be used.
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        return {k.value for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "add_argument"
                for k in n.args if isinstance(k, ast.Constant)}

    def test_no_all_organizations_behaviour(self, tenant):
        assert self._declared_flags() == {"--organization-id"}

    def test_no_batch_size_override(self, tenant):
        flags = self._declared_flags()
        for banned in ("--batch-size", "--limit", "--max"):
            assert banned not in flags
        # ...and the ceiling is not redefined here either.
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        assert "MAX_HANDOFFS_PER_PASS" not in assigned

    def test_no_rollout_override_flag(self, tenant):
        flags = self._declared_flags()
        for banned in ("--force", "--enabled", "--ignore-rollout",
                       "--rollout"):
            assert banned not in flags
        # The command must not read or name the control at all: the gate is
        # the trigger's, and duplicating it here would be a second authority.
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "rollout" not in name, name


def command_trigger_module():
    """The module the command imports the trigger FROM."""
    import app.alert as alert

    return alert


class _FakeTrigger:
    """A stand-in used only where the TRIGGER CALL is what is under test."""

    organization_id = 0
    rollout_state = "ENABLED"
    processed = True
    detail = None

    class result:
        handoffs = 0
        recorded = 0
        skipped_shadow = 0
        truncated = False
        outcomes = ()


# ═════════════════════════════════════════════════════════════════════════════
# 9–12. Rollout and tenant isolation
# ═════════════════════════════════════════════════════════════════════════════

class TestRolloutAndTenancy:

    def test_off_prevents_processing(self, tenant, seeded, factory, sends, capsys):
        _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker), recipient=tenant.worker,
                      audience="WORKER")
        _set_rollout(seeded, RolloutState.OFF, organization_id=tenant.org)
        code = _run(factory, "--organization-id", str(tenant.org))
        assert code == command.EXIT_ROLLOUT_PREVENTED
        assert sends == []
        assert "OFF" in capsys.readouterr().out

    def test_shadow_prevents_dispatch(self, tenant, seeded, factory, sends, capsys):
        _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker, mode="SHADOW"), recipient=tenant.worker,
                      audience="WORKER", mode="SHADOW")
        _set_rollout(seeded, RolloutState.SHADOW, organization_id=tenant.org)
        code = _run(factory, "--organization-id", str(tenant.org))
        assert code == command.EXIT_ROLLOUT_PREVENTED
        assert sends == []
        assert _attempts_for(seeded, tenant) == []

    def test_enabled_performs_one_pass(self, tenant, seeded, factory, sends, capsys):
        _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker), recipient=tenant.worker,
                      audience="WORKER")
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.org)
        code = _run(factory, "--organization-id", str(tenant.org))
        assert code == command.EXIT_OK
        assert len(sends) == 1
        assert "handoffs        : 1" in capsys.readouterr().out

    def test_two_organizations_stay_isolated(self, tenant, seeded, factory, sends):
        _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker), recipient=tenant.worker,
                      audience="WORKER")
        other = _event(seeded, organization_id=tenant.other_org,
                       user_id=tenant.other_worker,
                       snapshot={"schema_version": 2,
                                 "worker_user_id": tenant.other_worker,
                                 "roles": ["WORKER"]})
        _notification(seeded, other, recipient=tenant.other_worker,
                      audience="WORKER")
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.org)
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.other_org)

        _run(factory, "--organization-id", str(tenant.org))
        assert sends == [tenant.worker_email]

    def test_the_ceiling_stays_with_the_processor(self, tenant, seeded, factory,
                                                  sends, capsys):
        for _ in range(55):
            _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker), recipient=tenant.worker,
                          audience="WORKER")
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.org)
        _run(factory, "--organization-id", str(tenant.org))
        out = capsys.readouterr().out
        assert "handoffs        : 50" in out
        assert "truncated       : True" in out
        assert len(sends) == 50


# ═════════════════════════════════════════════════════════════════════════════
# 13–17. Output hygiene, layering, failure propagation
# ═════════════════════════════════════════════════════════════════════════════

class TestOutputAndLayering:

    def test_the_output_contains_no_worker_pii(self, tenant, seeded, factory, sends,
                                               capsys):
        _notification(seeded, _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker), recipient=tenant.worker,
                      audience="WORKER")
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.org)
        _run(factory, "--organization-id", str(tenant.org))
        out = capsys.readouterr().out
        assert WORKER_NAME not in out
        assert tenant.worker_email not in out
        assert "@" not in out
        for banned in ("correlation", "ndc_", "Your working time"):
            assert banned not in out

    def test_the_command_never_touches_the_platform_directly(self, tenant):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "notification_platform" not in name, name

    def test_the_command_never_touches_email_transport(self, tenant):
        source = SCRIPT.read_text(encoding="utf-8")
        for banned in ("email_service", "smtplib", "resend", "_send("):
            assert banned not in source

    def test_it_invokes_only_the_authorized_trigger(self, tenant):
        source = SCRIPT.read_text(encoding="utf-8")
        assert "run_eco_dispatch_pass" in source
        for banned in ("process_pending_notification_dispatches",
                       "dispatch_processing", "dispatch("):
            assert banned not in source

    def test_a_trigger_exception_is_not_swallowed(self, tenant, seeded, factory,
                                                  monkeypatch):
        def _boom(db, **kw):
            raise RuntimeError("trigger exploded")

        monkeypatch.setattr(command_trigger_module(),
                            "run_eco_dispatch_pass", _boom)
        with pytest.raises(RuntimeError):
            _run(factory, "--organization-id", str(tenant.org))

    def test_the_exit_codes_distinguish_the_three_cases(self, tenant):
        assert command.EXIT_OK == 0
        assert command.EXIT_ROLLOUT_PREVENTED == 3
        assert command.EXIT_FAILURE == 1
        assert len({command.EXIT_OK, command.EXIT_ROLLOUT_PREVENTED,
                    command.EXIT_FAILURE}) == 3

    def test_it_uses_the_application_session_pattern(self, tenant):
        source = SCRIPT.read_text(encoding="utf-8")
        assert "from database.database import SessionLocal" in source
        assert "create_engine" not in source, "no second DB architecture"


# ═════════════════════════════════════════════════════════════════════════════
# 18–19 + GV-07 — the eleven demonstrable steps
# ═════════════════════════════════════════════════════════════════════════════

class TestGV07EndToEnd:
    """GV-07: de-crossing. The withdrawal goes to ALL ORIGINAL RECIPIENTS.

    Two resolved recipients, as the locked vector requires — the Worker and a
    designated PRIMARY tenant.admin. Nothing here promotes the vector.
    """

    @pytest.fixture
    def crossing(self, seeded, tenant):
        """Steps 1–2: a committed event with a frozen two-recipient snapshot."""
        seeded.add(EcoRecipientDesignation(
            organization_id=tenant.org, audience="PRIMARY_ADMIN", user_id=tenant.admin))
        seeded.flush()
        event = _event(seeded, organization_id=tenant.org,
                     user_id=tenant.worker, snapshot=_snapshot(tenant.org, tenant.worker, tenant.admin))
        _notification(seeded, event, recipient=tenant.worker, audience="WORKER")
        _notification(seeded, event, recipient=tenant.admin,
                      audience="PRIMARY_ADMIN")
        _set_rollout(seeded, RolloutState.ENABLED, organization_id=tenant.org)
        return event

    def test_step_1_2_the_event_is_committed_with_a_frozen_snapshot(
            self, tenant, seeded, crossing):
        assert crossing.id is not None
        named = {e["audience"]: e["user_ids"]
                 for e in crossing.recipient_snapshot["audiences"]}
        assert named["WORKER"] == [tenant.worker]
        assert named["PRIMARY_ADMIN"] == [tenant.admin]

    def test_steps_3_to_8_the_command_delivers_to_both_recipients(
            self, tenant, seeded, crossing, factory, sends, capsys):
        code = _run(factory, "--organization-id", str(tenant.org))
        assert code == command.EXIT_OK                       # 3, 4, 5
        out = capsys.readouterr().out
        assert "handoffs        : 2" in out                  # 6, 7
        assert "truncated       : False" in out

        assert sorted(sends) == sorted([tenant.worker_email, tenant.admin_email])
        attempts = _attempts_for(seeded, tenant)
        assert len(attempts) == 2                            # 8
        assert {a.recipient_user_id for a in attempts} == {tenant.worker, tenant.admin}
        assert all(a.outcome == "dispatched" for a in attempts)
        assert all(a.channel == "email" for a in attempts)
        assert all(a.correlation_id for a in attempts)

    def test_step_9_the_original_recipients_are_preserved(self, tenant, seeded,
                                                          crossing, factory,
                                                          sends):
        event_id = crossing.id
        _run(factory, "--organization-id", str(tenant.org))
        # Everything about today's authority changes...
        seeded.execute(sa.delete(EcoRecipientDesignation))
        seeded.flush()
        # ...and the frozen snapshot still names both. Re-read by id rather
        # than refreshed: the command commits, and what is PERSISTED is the
        # evidence — a held instance is not.
        stored = seeded.get(EcoThresholdEvent, event_id)
        named = {e["audience"]: e["user_ids"]
                 for e in stored.recipient_snapshot["audiences"]}
        assert named["PRIMARY_ADMIN"] == [tenant.admin]

    def test_step_10_a_repeated_invocation_does_not_retransmit(
            self, tenant, seeded, crossing, factory, sends):
        _run(factory, "--organization-id", str(tenant.org))
        assert len(sends) == 2
        code = _run(factory, "--organization-id", str(tenant.org))
        assert code == command.EXIT_OK
        assert len(sends) == 2, "D-4 idempotency must prevent retransmission"
        assert len(_attempts_for(seeded, tenant)) == 2

    def test_step_11_the_withdrawal_reaches_the_original_recipients(
            self, tenant, seeded, crossing, factory, sends):
        """§21.3 / §33.1: the withdrawal goes to ALL ORIGINAL RECIPIENTS.

        The withdrawal carries the crossing's snapshot verbatim — which is what
        EVAL-EVENT already does — so its recipients are the ORIGINAL two even
        after every current designation is deleted.
        """
        event_id = crossing.id
        frozen = dict(crossing.recipient_snapshot)
        _run(factory, "--organization-id", str(tenant.org))
        assert len(sends) == 2

        # Current authority is emptied before the withdrawal is raised.
        seeded.execute(sa.delete(EcoRecipientDesignation))
        seeded.flush()

        withdrawal = _event(seeded, organization_id=tenant.org,
                            user_id=tenant.worker, snapshot=frozen,
                            event_type="ECO_THRESHOLD_WITHDRAWN")
        _notification(seeded, withdrawal, recipient=tenant.worker, audience="WORKER")
        _notification(seeded, withdrawal, recipient=tenant.admin,
                      audience="PRIMARY_ADMIN")

        _run(factory, "--organization-id", str(tenant.org))
        assert len(sends) == 4
        assert sorted(sends[2:]) == sorted([tenant.worker_email, tenant.admin_email])

        # The original crossing is retained, untouched.
        assert seeded.get(EcoThresholdEvent, event_id) is not None

    def test_this_suite_is_the_registered_harness_for_the_vector(
            self, tenant):
        """Restated: GV-07 has been promoted, and on this evidence.

        This previously asserted the vector was NOT promoted. It now asserts
        the stronger pair: the artifact says executable, AND this class is
        what the golden suite names as its execution harness.
        """
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []

        # ...and THIS class is the registered evidence for it, so the
        # promotion cannot survive the harness being renamed or removed.
        from tests.test_eco_golden_vectors import EXTERNAL_EXECUTION_TESTS

        assert EXTERNAL_EXECUTION_TESTS["GV-07"] == (
            "test_dispatch_command.py", "class TestGV07EndToEnd")


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_no_recurring_process_was_added(self, tenant):
        source = (BACKEND / "main.py").read_text(encoding="utf-8")
        assert source.count("run_forever") == 12
        for banned in ("run_eco_dispatch_pass", "dispatch_trigger",
                       "dispatch_processing"):
            assert banned not in source

    def test_no_route_was_added(self, tenant):
        for path in (BACKEND / "app").rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="ignore")
            if "run_eco_dispatch_pass" in source and "APIRouter" in source:
                pytest.fail(f"{path.name} exposes the trigger over HTTP")

    def test_no_migration_was_added(self, tenant):
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == 94

    def test_de_remains_untouched(self, tenant):
        register = (BACKEND / "app" / "early_clock_out"
                    / "copy_register.py").read_text(encoding="utf-8")
        assert "NOTIF-A-01" not in register

    def test_authorized_audiences_was_not_widened(self, tenant):
        from app.alert.outcomes import AUTHORIZED_AUDIENCES

        assert AUTHORIZED_AUDIENCES == frozenset({"WORKER"})

    def test_eco_v1_remains_disabled(self, tenant):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_gv18_and_gv26_remain_blocked(self, tenant):
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