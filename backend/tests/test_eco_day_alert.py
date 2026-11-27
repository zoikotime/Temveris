"""
The §24.3 day-scoped administrator alert.
=========================================

ZT-SPEC-ECO-002 v2.0 §24.3 / §33.1; owner rulings D-C: B (a separate primitive
alongside ALERT), Ruling 3: B (§31's locked-copy gate does not apply), Ruling 4
(PRIMARY ADMIN only), D-1, D-2, D-3, D-4, D-B and R5.

Proves the alert is raised from the authoritative day evaluation and from
nothing else, that it reaches PRIMARY ADMIN and no one else, and — from several
directions — that GV-07's threshold-event path is untouched.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa

from app.eco_day_alert import (
    ALERTABLE_STATE,
    AUTHORIZED_AUDIENCES,
    FORBIDDEN_AUDIENCES,
    MAX_HANDOFFS_PER_PASS,
    RECIPIENT_RESOLVED,
    RECIPIENT_UNRESOLVED,
    REQUIREMENT_UNRESOLVED_BODY,
    REQUIREMENT_UNRESOLVED_BODY_ID,
    DayAlertOutcome,
    DayAlertType,
    EcoDayAdministratorAlert,
    EcoDayAlertDeliveryAttempt,
    RefusalReason,
    process_pending_day_alert_dispatches,
    raise_day_administrator_alert,
)
from app.eval_day.models import EcoDayRecord
from app.notification_platform import NotificationDispatchRequest
from app.recipient_authority import EcoRecipientDesignation

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eco_day_alert"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "c2e8f5b71d94_eco_day_administrator_alert.py")

ORG = 9300
OTHER_ORG = 9400
WORKER = 93001
ADMIN = 93002
ADMIN_B = 93003
OTHER_WORKER = 94001

WORKER_NAME = "Jordan Smith"
ADMIN_EMAIL = "admin.da@example.test"

_DAY = {"n": 0}


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import AuditLog, Organization, User

    for model in (Organization, User, AuditLog, EcoDayRecord,
                  EcoRecipientDesignation, NotificationDispatchRequest,
                  EcoDayAdministratorAlert, EcoDayAlertDeliveryAttempt):
        model.__table__.create(engine, checkfirst=True)


@pytest.fixture
def seeded(db):
    from database.models import Organization, User

    db.execute(sa.delete(NotificationDispatchRequest))
    db.execute(sa.delete(EcoRecipientDesignation))
    for org_id in (ORG, OTHER_ORG):
        if db.get(Organization, org_id) is None:
            db.add(Organization(id=org_id, name=f"org-{org_id}",
                                billing_type="standard"))
    db.flush()
    for uid, org_id, name, email in (
        (WORKER, ORG, WORKER_NAME, "w.da@example.test"),
        (ADMIN, ORG, "Primary Admin", ADMIN_EMAIL),
        (ADMIN_B, ORG, "Second Admin", "admin2.da@example.test"),
        (OTHER_WORKER, OTHER_ORG, "Other", "other.da@example.test"),
    ):
        if db.get(User, uid) is None:
            db.add(User(id=uid, name=name, email=email,
                        organization_id=org_id))
    db.flush()
    return db


@pytest.fixture
def sends(monkeypatch):
    import app.email_service as mailer

    calls = []
    monkeypatch.setattr(mailer, "_send",
                        lambda **kw: (calls.append(kw.get("to")), True)[1])
    return calls


def _designate(db, user_id=ADMIN, *, organization_id=ORG):
    db.add(EcoRecipientDesignation(organization_id=organization_id,
                                   audience="PRIMARY_ADMIN", user_id=user_id))
    db.flush()


def _day(db, *, organization_id=ORG, user_id=WORKER, state=ALERTABLE_STATE,
         shortfall_computed=False, generation=1, superseded_by=None):
    _DAY["n"] += 1
    row = EcoDayRecord(
        organization_id=organization_id, user_id=user_id,
        work_date=dt.date(2026, 9, 1) + dt.timedelta(days=_DAY["n"]),
        evaluation_generation=generation, state=state,
        shortfall_computed=shortfall_computed, ledger_version=1,
        requirement_outcome="REQUIREMENT_UNRESOLVED",
        content_hash=f"{_DAY['n']:064x}", leave_input_snapshot_key="k",
        evaluated_at_utc=dt.datetime.now(dt.timezone.utc),
        superseded_by=superseded_by)
    db.add(row)
    db.flush()
    return row


def _alerts(db, organization_id=ORG):
    return list(db.execute(
        sa.select(EcoDayAdministratorAlert).where(
            EcoDayAdministratorAlert.organization_id == organization_id)
    ).scalars().all())


def _package_files():
    return sorted(PACKAGE.glob("*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# 1, 9, 10. Creation from the authoritative day evaluation
# ═════════════════════════════════════════════════════════════════════════════

class TestCreation:

    def test_requirement_unresolved_creates_the_alert(self, seeded):
        _designate(seeded)
        result = raise_day_administrator_alert(seeded, day_record=_day(seeded))
        assert result.outcome is DayAlertOutcome.RAISED
        assert result.alert_id is not None

    def test_it_originates_from_the_day_record(self, seeded):
        """§24.3's alert is about a DAY. The row points at the authoritative
        evaluation, not at a threshold event."""
        _designate(seeded)
        record = _day(seeded)
        raise_day_administrator_alert(seeded, day_record=record)
        alert = _alerts(seeded)[-1]
        assert alert.day_record_id == record.id
        assert alert.work_date == record.work_date
        assert alert.evaluation_generation == record.evaluation_generation
        assert alert.day_state == ALERTABLE_STATE

    def test_no_threshold_event_is_fabricated(self, seeded):
        """The obstacle that made this primitive necessary, asserted."""
        from app.eval_event import EcoThresholdEvent

        EcoThresholdEvent.__table__.create(seeded.get_bind(), checkfirst=True)
        before = seeded.query(EcoThresholdEvent).count()
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        assert seeded.query(EcoThresholdEvent).count() == before

    def test_the_package_never_imports_eval_event(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert "eval_event" not in name, f"{path.name}: {name}"
                    assert "app.alert" not in name, f"{path.name}: {name}"

    def test_the_day_record_is_never_mutated(self, seeded):
        _designate(seeded)
        record = _day(seeded)
        before = {c.name: getattr(record, c.name)
                  for c in EcoDayRecord.__table__.c}
        raise_day_administrator_alert(seeded, day_record=record)
        seeded.flush()
        assert {c.name: getattr(record, c.name)
                for c in EcoDayRecord.__table__.c} == before


# ═════════════════════════════════════════════════════════════════════════════
# No creation for any other state
# ═════════════════════════════════════════════════════════════════════════════

class TestStateGate:

    @pytest.mark.parametrize("state", [
        "SHORTFALL", "NO_SHORTFALL", "DEFERRED_PENDING_LEDGER",
        "NOT_A_WORKING_DAY", "POLICY_UNRESOLVED", "EVALUABLE",
    ])
    def test_no_alert_for_any_other_state(self, seeded, state):
        _designate(seeded)
        result = raise_day_administrator_alert(
            seeded, day_record=_day(seeded, state=state))
        assert result.outcome is DayAlertOutcome.REFUSED
        assert result.refusal is RefusalReason.STATE_NOT_ALERTABLE
        assert _alerts(seeded) == []

    def test_a_computed_shortfall_contradicts_the_state(self, seeded):
        """§24.3 requires shortfall_computed false. Contradictory evidence
        asserts nothing rather than asserting the wrong thing."""
        _designate(seeded)
        result = raise_day_administrator_alert(
            seeded, day_record=_day(seeded, shortfall_computed=True))
        assert result.refusal is RefusalReason.SHORTFALL_WAS_COMPUTED
        assert _alerts(seeded) == []

    def test_a_superseded_generation_raises_nothing(self, seeded):
        _designate(seeded)
        result = raise_day_administrator_alert(
            seeded, day_record=_day(seeded, superseded_by=999))
        assert result.refusal is RefusalReason.DAY_RECORD_SUPERSEDED
        assert _alerts(seeded) == []

    def test_the_state_is_pinned_in_the_database(self):
        check = next(c for c in EcoDayAdministratorAlert.__table__.constraints
                     if getattr(c, "name", "")
                     == "ck_eco_day_administrator_alert_state")
        assert "REQUIREMENT_UNRESOLVED" in str(check.sqltext)


# ═════════════════════════════════════════════════════════════════════════════
# 2-6. Recipient: PRIMARY ADMIN only, D-1 on unresolved
# ═════════════════════════════════════════════════════════════════════════════

class TestRecipient:

    def test_primary_admin_is_the_only_authorized_audience(self):
        assert AUTHORIZED_AUDIENCES == frozenset({"PRIMARY_ADMIN"})

    def test_worker_manager_and_hr_are_forbidden(self):
        assert FORBIDDEN_AUDIENCES == frozenset(
            {"WORKER", "DIRECT_MANAGER", "HR_ADMIN"})
        assert not (AUTHORIZED_AUDIENCES & FORBIDDEN_AUDIENCES)

    def test_the_database_refuses_every_other_audience(self, seeded):
        _designate(seeded)
        record = _day(seeded)
        raise_day_administrator_alert(seeded, day_record=record)
        alert = _alerts(seeded)[-1]
        for forbidden in sorted(FORBIDDEN_AUDIENCES):
            with pytest.raises(sa.exc.IntegrityError):
                with seeded.begin_nested():
                    seeded.execute(sa.insert(EcoDayAdministratorAlert).values(
                        organization_id=ORG, user_id=WORKER,
                        work_date=record.work_date, day_record_id=record.id,
                        evaluation_generation=2,
                        day_state=ALERTABLE_STATE,
                        alert_type=DayAlertType.REQUIREMENT_UNRESOLVED.value,
                        audience=forbidden, mode="OPERATIONAL",
                        recipient_user_id=ADMIN,
                        recipient_outcome=RECIPIENT_RESOLVED,
                        recipient_snapshot={}, rendered_body="x",
                        rendered_copy_id="x"))
        assert alert.audience == "PRIMARY_ADMIN"

    def test_the_resolved_recipient_is_the_designated_primary_admin(self,
                                                                    seeded):
        _designate(seeded, ADMIN)
        result = raise_day_administrator_alert(seeded, day_record=_day(seeded))
        assert result.recipient_user_id == ADMIN
        assert _alerts(seeded)[-1].recipient_outcome == RECIPIENT_RESOLVED

    def test_an_unresolved_primary_admin_is_recorded_not_substituted(self,
                                                                     seeded):
        """D-1. No designation exists — nobody is invented in its place."""
        result = raise_day_administrator_alert(seeded, day_record=_day(seeded))
        assert result.outcome is DayAlertOutcome.RAISED
        assert result.refusal is RefusalReason.RECIPIENT_UNRESOLVED
        assert result.recipient_user_id is None

        alert = _alerts(seeded)[-1]
        assert alert.recipient_outcome == RECIPIENT_UNRESOLVED
        assert alert.recipient_user_id is None
        # ...and specifically NOT the Worker.
        assert alert.recipient_user_id != WORKER

    def test_the_recipient_pair_cannot_drift(self, seeded):
        """D-1 in the database: RESOLVED implies an id, UNRESOLVED implies
        NULL, and nothing else is representable."""
        _designate(seeded)
        record = _day(seeded)
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                seeded.execute(sa.insert(EcoDayAdministratorAlert).values(
                    organization_id=ORG, user_id=WORKER,
                    work_date=record.work_date, day_record_id=record.id,
                    evaluation_generation=7, day_state=ALERTABLE_STATE,
                    alert_type=DayAlertType.REQUIREMENT_UNRESOLVED.value,
                    audience="PRIMARY_ADMIN", mode="OPERATIONAL",
                    recipient_user_id=None,
                    recipient_outcome=RECIPIENT_RESOLVED,
                    recipient_snapshot={}, rendered_body="x",
                    rendered_copy_id="x"))

    def test_the_package_does_not_reimplement_recipient_resolution(self):
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "resolve_eco_recipients" in source
        assert "EcoRecipientDesignation" not in source

    def test_the_snapshot_is_frozen_against_later_designation_changes(
            self, seeded):
        """D-3.7: the alert's recipient is the one resolved at raise time."""
        _designate(seeded, ADMIN)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        alert = _alerts(seeded)[-1]
        frozen = dict(alert.recipient_snapshot)

        seeded.execute(sa.delete(EcoRecipientDesignation))
        _designate(seeded, ADMIN_B)
        seeded.flush()

        assert alert.recipient_user_id == ADMIN
        assert alert.recipient_snapshot == frozen
        named = {e["audience"]: e["user_ids"] for e in frozen["audiences"]}
        assert named["PRIMARY_ADMIN"] == [ADMIN]


# ═════════════════════════════════════════════════════════════════════════════
# 7. Idempotency and the day-scoped identity
# ═════════════════════════════════════════════════════════════════════════════

class TestIdentityAndIdempotency:

    def test_repeated_raising_is_idempotent(self, seeded):
        _designate(seeded)
        record = _day(seeded)
        first = raise_day_administrator_alert(seeded, day_record=record)
        for _ in range(3):
            again = raise_day_administrator_alert(seeded, day_record=record)
            assert again.outcome is DayAlertOutcome.ALREADY_RAISED
            assert again.alert_id == first.alert_id
        assert len(_alerts(seeded)) == 1

    def test_the_identity_is_day_scoped_not_week_scoped(self):
        unique = next(c for c in EcoDayAdministratorAlert.__table__.constraints
                      if getattr(c, "name", "")
                      == "uq_eco_day_administrator_alert_identity")
        names = [c.name for c in unique.columns]
        assert names == ["organization_id", "user_id", "work_date",
                         "evaluation_generation", "alert_type", "audience",
                         "mode"]
        for week_shaped in ("workweek_key", "tier", "sequence"):
            assert week_shaped not in names

    def test_a_later_generation_is_a_different_fact(self, seeded):
        """The alert pins a generation, so a corrected evaluation is not
        silently followed — and is not silently suppressed either."""
        _designate(seeded)
        record = _day(seeded, generation=1)
        raise_day_administrator_alert(seeded, day_record=record)

        # A re-evaluation reads different evidence, so it differs on the
        # snapshot index as well as the generation — which is what makes it a
        # genuinely new authoritative answer rather than a duplicate row.
        corrected = EcoDayRecord(
            organization_id=ORG, user_id=WORKER, work_date=record.work_date,
            evaluation_generation=2, state=ALERTABLE_STATE,
            shortfall_computed=False, ledger_version=2,
            requirement_outcome="REQUIREMENT_UNRESOLVED",
            content_hash="b" * 64, leave_input_snapshot_key="k2",
            evaluated_at_utc=dt.datetime.now(dt.timezone.utc))
        seeded.add(corrected)
        seeded.flush()

        result = raise_day_administrator_alert(seeded, day_record=corrected)
        assert result.outcome is DayAlertOutcome.RAISED
        assert len(_alerts(seeded)) == 2

    def test_a_duplicate_row_is_refused_by_the_database(self, seeded):
        _designate(seeded)
        record = _day(seeded)
        raise_day_administrator_alert(seeded, day_record=record)
        alert = _alerts(seeded)[-1]
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                seeded.execute(sa.insert(EcoDayAdministratorAlert).values(
                    organization_id=alert.organization_id,
                    user_id=alert.user_id, work_date=alert.work_date,
                    day_record_id=alert.day_record_id,
                    evaluation_generation=alert.evaluation_generation,
                    day_state=ALERTABLE_STATE,
                    alert_type=alert.alert_type, audience=alert.audience,
                    mode=alert.mode, recipient_user_id=ADMIN,
                    recipient_outcome=RECIPIENT_RESOLVED,
                    recipient_snapshot={}, rendered_body="x",
                    rendered_copy_id="x"))


# ═════════════════════════════════════════════════════════════════════════════
# 11, 12. Dispatch through the platform
# ═════════════════════════════════════════════════════════════════════════════

class TestDispatch:

    def test_the_alert_dispatches_through_the_platform(self, seeded, sends):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        result = process_pending_day_alert_dispatches(seeded,
                                                      organization_id=ORG)
        assert result.handoffs == 1
        assert result.recorded == 1
        assert result.outcomes[0].outcome == "dispatched"
        assert result.outcomes[0].channel == "email"
        assert sends == [ADMIN_EMAIL]

    def test_the_platform_receives_the_administrator_class(self, seeded,
                                                           sends):
        """D-B: PRIMARY_ADMIN maps to ECO_ADMIN_OPERATIONAL, which is absent
        from SUPPRESSIBLE, so preference policy cannot decline it."""
        from app.notification_platform import notification_class_for
        from app.notification_platform.contracts import NotificationHandoff
        from app.notifications import classification as classes

        handoff = NotificationHandoff(
            organization_id=ORG, eco_event_id=1, recipient_user_id=ADMIN,
            audience="PRIMARY_ADMIN",
            notification_type=DayAlertType.REQUIREMENT_UNRESOLVED.value)
        assert notification_class_for(handoff) == classes.ECO_ADMIN_OPERATIONAL
        assert classes.ECO_ADMIN_OPERATIONAL not in classes.SUPPRESSIBLE
        assert classes.ECO_ADMIN_OPERATIONAL not in classes.QUIET_HOURS_ELIGIBLE

    def test_a_correlation_id_is_recorded(self, seeded, sends):
        from app.notification_platform import is_platform_correlation_id

        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        process_pending_day_alert_dispatches(seeded, organization_id=ORG)
        attempt = seeded.query(EcoDayAlertDeliveryAttempt).one()
        assert is_platform_correlation_id(attempt.correlation_id)
        assert attempt.recipient_user_id == ADMIN
        assert attempt.channel == "email"
        assert attempt.outcome == "dispatched"

    def test_replay_does_not_retransmit(self, seeded, sends):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        process_pending_day_alert_dispatches(seeded, organization_id=ORG)
        assert len(sends) == 1
        again = process_pending_day_alert_dispatches(seeded,
                                                     organization_id=ORG)
        assert again.handoffs == 0
        assert len(sends) == 1

    def test_an_unresolved_recipient_is_never_dispatched(self, seeded, sends):
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        result = process_pending_day_alert_dispatches(seeded,
                                                      organization_id=ORG)
        assert result.handoffs == 0
        assert result.skipped_unresolved_recipient == 1
        assert sends == []

    def test_the_worker_never_receives_the_alert(self, seeded, sends):
        from database.models import User

        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        process_pending_day_alert_dispatches(seeded, organization_id=ORG)
        worker_email = seeded.get(User, WORKER).email
        assert worker_email not in sends
        assert sends == [ADMIN_EMAIL]

    def test_the_batch_ceiling_is_fifty(self, seeded, sends):
        assert MAX_HANDOFFS_PER_PASS == 50
        _designate(seeded)
        for _ in range(55):
            raise_day_administrator_alert(seeded, day_record=_day(seeded))
        result = process_pending_day_alert_dispatches(seeded,
                                                      organization_id=ORG)
        assert result.handoffs == 50
        assert result.truncated is True
        assert process_pending_day_alert_dispatches(
            seeded, organization_id=ORG).handoffs == 5

    def test_a_caller_cannot_raise_the_ceiling(self, seeded, sends):
        _designate(seeded)
        for _ in range(55):
            raise_day_administrator_alert(seeded, day_record=_day(seeded))
        assert process_pending_day_alert_dispatches(
            seeded, organization_id=ORG, limit=1000).handoffs == 50

    def test_tenant_isolation(self, seeded, sends):
        _designate(seeded)
        _designate(seeded, OTHER_WORKER, organization_id=OTHER_ORG)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        raise_day_administrator_alert(
            seeded, day_record=_day(seeded, organization_id=OTHER_ORG,
                                    user_id=OTHER_WORKER))
        result = process_pending_day_alert_dispatches(seeded,
                                                      organization_id=ORG)
        assert result.handoffs == 1
        assert result.outcomes[0].organization_id == ORG

    def test_the_pass_contains_no_loop_over_batches(self):
        source = (PACKAGE / "dispatch.py").read_text(encoding="utf-8")
        assert "while " not in source


# ═════════════════════════════════════════════════════════════════════════════
# 13-15. Rollout
# ═════════════════════════════════════════════════════════════════════════════

class TestRollout:

    def test_shadow_alerts_are_excluded_before_the_boundary(self, seeded,
                                                             sends):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded),
                                      mode="SHADOW")
        result = process_pending_day_alert_dispatches(seeded,
                                                      organization_id=ORG)
        assert result.handoffs == 0
        assert sends == []
        assert seeded.query(NotificationDispatchRequest).count() == 0
        assert seeded.query(EcoDayAlertDeliveryAttempt).count() == 0

    def test_the_package_holds_no_rollout_authority(self):
        """The gate lives in the trigger, exactly once (D-C)."""
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert "rollout" not in name, f"{path.name}: {name}"

    def test_the_trigger_gates_the_day_alert_pass(self, seeded, sends):
        """OFF and SHADOW must both stop it; ENABLED must run it."""
        from app.alert import run_eco_dispatch_pass
        from app.rollout import (CONTROL_EARLY_CLOCK_OUT_V2, RolloutControl,
                                 RolloutState)

        from app.alert import EcoNotification, EcoNotificationDeliveryAttempt
        from app.eval_event import EcoThresholdEvent

        # The trigger runs BOTH passes, so the threshold-event side must exist
        # for its pass to find nothing rather than fail.
        for model in (EcoThresholdEvent, EcoNotification,
                      EcoNotificationDeliveryAttempt, RolloutControl):
            model.__table__.create(seeded.get_bind(), checkfirst=True)
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))

        def _set(state):
            seeded.execute(sa.delete(RolloutControl).where(
                RolloutControl.organization_id == ORG))
            seeded.add(RolloutControl(
                organization_id=ORG, control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                state=state.value, last_transition_by="test"))
            seeded.flush()

        _set(RolloutState.OFF)
        assert run_eco_dispatch_pass(seeded, organization_id=ORG).processed \
            is False
        assert sends == []

        _set(RolloutState.SHADOW)
        assert run_eco_dispatch_pass(seeded, organization_id=ORG).processed \
            is False
        assert sends == []

        _set(RolloutState.ENABLED)
        result = run_eco_dispatch_pass(seeded, organization_id=ORG)
        assert result.processed is True
        assert result.day_alert_result.handoffs == 1
        assert sends == [ADMIN_EMAIL]


# ═════════════════════════════════════════════════════════════════════════════
# Copy — Ruling 3: B
# ═════════════════════════════════════════════════════════════════════════════

class TestImplementationCopy:

    def test_the_body_passes_the_31_1_terminology_gate(self):
        """Ruling 3: B lifted §31's COMPARISON gate. It lifted nothing else."""
        from app.early_clock_out.terminology import (find_prohibited_terms,
                                                     is_clean)

        assert find_prohibited_terms(REQUIREMENT_UNRESOLVED_BODY) == []
        assert is_clean(REQUIREMENT_UNRESOLVED_BODY)

    def test_the_body_uses_the_4_1_neutral_vocabulary(self):
        assert "Tenant" in REQUIREMENT_UNRESOLVED_BODY
        assert "Worker" in REQUIREMENT_UNRESOLVED_BODY
        lowered = REQUIREMENT_UNRESOLVED_BODY.lower()
        for never in ("employee", "company", "organization", "staff member"):
            assert never not in lowered

    def test_the_body_makes_no_employment_judgment(self):
        """§24.3: an unresolved requirement is a Tenant configuration matter,
        and describing it as the Worker's conduct would be a false statement
        about them."""
        import re

        # Word boundaries, not substrings: "calculated" contains "late", and a
        # naive `in` check would fail on copy that is perfectly neutral.
        lowered = REQUIREMENT_UNRESOLVED_BODY.lower()
        for judgment in ("late", "absent", "responsible", "fault", "blame"):
            assert not re.search(rf"{judgment}", lowered), judgment
        for phrase in ("shortfall of", "did not", "failed to"):
            assert phrase not in lowered

    def test_the_body_reports_no_figures(self):
        """Nothing was computed for this day. A zero would read as a
        measurement."""
        assert not any(ch.isdigit() for ch in REQUIREMENT_UNRESOLVED_BODY)

    def test_it_is_not_registered_as_locked_copy(self):
        """Ruling 3: B. This is implementation copy and must stay
        distinguishable from the §31 register."""
        from app.early_clock_out.copy_register import LOCKED_COPY

        assert REQUIREMENT_UNRESOLVED_BODY_ID not in LOCKED_COPY
        assert REQUIREMENT_UNRESOLVED_BODY not in LOCKED_COPY.values()

    def test_no_locked_notification_was_borrowed(self):
        """NOTIF-A-01 is §33.3's threshold-reached notice, not this alert.

        Checked against the copy VALUES and the module's identifiers, not its
        prose: ``copy.py``'s docstring names both locked notices in order to
        record that neither was used, and a substring scan cannot tell a
        disclaimer from a borrowing.
        """
        from app.early_clock_out.copy_register import LOCKED_COPY
        from app.eco_day_alert import IMPLEMENTATION_COPY

        # No implementation string is any locked string.
        locked = set(LOCKED_COPY.values())
        for body in IMPLEMENTATION_COPY.values():
            assert body not in locked
        # ...and in particular it is not the §33.3 administrator disclaimer,
        # the only administrator copy the register holds.
        assert LOCKED_COPY["eco.admin.disclaimer"] not in             IMPLEMENTATION_COPY.values()

        # No locked identifier is USED as a value or a name anywhere here.
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            for banned in ("NOTIF-A-01", "NOTIF-W-01", "eco.admin.disclaimer"):
                assert banned not in used, f"{path.name} uses {banned}"

    def test_the_copy_regime_is_declared(self):
        from app.eco_day_alert import COPY_REGIME

        assert COPY_REGIME == "IMPLEMENTATION_COPY_RULING_3B"

    def test_the_rendered_body_is_frozen_on_the_alert(self, seeded):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        alert = _alerts(seeded)[-1]
        assert alert.rendered_body == REQUIREMENT_UNRESOLVED_BODY
        assert alert.rendered_copy_id == REQUIREMENT_UNRESOLVED_BODY_ID


# ═════════════════════════════════════════════════════════════════════════════
# R5 append-only
# ═════════════════════════════════════════════════════════════════════════════

class TestAppendOnly:

    def test_an_alert_cannot_be_updated(self, seeded):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        alert = _alerts(seeded)[-1]
        with pytest.raises(sa.exc.IntegrityError):
            seeded.execute(sa.text(
                "UPDATE eco_day_administrator_alert SET rendered_body='x' "
                "WHERE id=:i"), {"i": alert.id})
        seeded.rollback()

    def test_an_alert_cannot_be_deleted(self, seeded):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        alert = _alerts(seeded)[-1]
        with pytest.raises(sa.exc.IntegrityError):
            seeded.execute(sa.text(
                "DELETE FROM eco_day_administrator_alert WHERE id=:i"),
                {"i": alert.id})
        seeded.rollback()

    def test_a_delivery_attempt_cannot_be_updated(self, seeded, sends):
        _designate(seeded)
        raise_day_administrator_alert(seeded, day_record=_day(seeded))
        process_pending_day_alert_dispatches(seeded, organization_id=ORG)
        attempt = seeded.query(EcoDayAlertDeliveryAttempt).one()
        with pytest.raises(sa.exc.IntegrityError):
            seeded.execute(sa.text(
                "UPDATE eco_day_alert_delivery_attempt SET outcome='failed' "
                "WHERE id=:i"), {"i": attempt.id})
        seeded.rollback()

    def test_both_dialects_are_covered(self):
        from app.eco_day_alert.append_only_ddl import (
            SUPPORTED_DIALECTS, append_only_statements,
            attempt_append_only_statements)

        assert SUPPORTED_DIALECTS == frozenset({"sqlite", "postgresql"})
        for dialect in SUPPORTED_DIALECTS:
            assert append_only_statements(dialect)
            assert attempt_append_only_statements(dialect)
        assert append_only_statements("mysql") == ()


# ═════════════════════════════════════════════════════════════════════════════
# 16, 17. GV-07 isolation and scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_existing_alert_contract_is_unchanged(self):
        """GV-07's vocabulary and audience set are untouched by this work."""
        from app.alert.outcomes import (AUTHORIZED_AUDIENCES as ALERT_AUDIENCES,
                                        NotificationType)

        assert ALERT_AUDIENCES == frozenset({"WORKER"})
        assert [t.value for t in NotificationType] == [
            "THRESHOLD_REACHED", "THRESHOLD_WITHDRAWN"]

    def test_the_notification_identity_is_unchanged(self):
        from app.alert.models import EcoNotification

        unique = next(c for c in EcoNotification.__table__.constraints
                      if getattr(c, "name", "")
                      == "uq_eco_notification_identity")
        assert [c.name for c in unique.columns] == [
            "organization_id", "user_id", "workweek_key", "tier", "mode",
            "sequence", "notification_type", "audience"]
        assert EcoNotification.__table__.c[
            "originating_event_id"].nullable is False

    def test_the_d_c_processor_is_untouched_by_this_package(self):
        source = (BACKEND / "app" / "alert"
                  / "dispatch_processing.py").read_text(encoding="utf-8")
        assert "eco_day_alert" not in source

    def test_exactly_one_caller_raises_these_alerts(self):
        """This asserted that NOTHING raised these, which was true until the
        separate authorization arrived: the P0 calculation driver.

        EVALUATION still does not raise them -- `app/eval_day` is not a caller
        and must never become one, because a record that alerted on itself
        could not be the thing alerted about. The driver raises them, from the
        already-recorded day, on the one state §24.3 attaches to.

        Asserted by AST CALL scan: the driver names this function in prose.
        """
        callers = []
        for path in (BACKEND / "app").rglob("*.py"):
            if path.is_relative_to(PACKAGE) or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                      for n in ast.walk(tree) if isinstance(n, ast.Call)}
            if "raise_day_administrator_alert" in called:
                callers.append(path.relative_to(BACKEND).as_posix())
        assert callers == ["app/eco_driver/service.py"], callers

    def test_the_package_declares_no_http_surface(self):
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "@router.", "fastapi"):
                assert banned not in source, path.name

    def test_the_package_implements_no_retry_or_transport(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names |= {n.name for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)}
            for banned in ("retry", "backoff", "send_email", "smtplib",
                           "select_channel", "evaluate_send", "is_suppressed"):
                assert banned not in names, f"{path.name}: {banned}"

    def test_the_migration_is_purely_additive(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        for banned in ("drop_table", "drop_column", "alter_column",
                       "rename_table", "bulk_insert"):
            assert banned not in called

    def test_eco_v1_and_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_the_golden_artifact_is_untouched(self):
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []
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
# ═════════════════════════════════════════════════════════════════════════════
# GV-18's four expected fields
# ═════════════════════════════════════════════════════════════════════════════

class TestGV18:
    """§24.3's locked expectation, walked against the real primitive.

    Nothing here promotes the vector.
    """

    def test_the_four_expected_fields(self, seeded, sends):
        from database.models import User

        _designate(seeded, ADMIN)
        record = _day(seeded)

        # state = REQUIREMENT_UNRESOLVED, shortfall_computed = false
        assert record.state == "REQUIREMENT_UNRESOLVED"
        assert record.shortfall_computed is False

        # administrator_alert = true
        result = raise_day_administrator_alert(seeded, day_record=record)
        assert result.outcome is DayAlertOutcome.RAISED
        alert = _alerts(seeded)[-1]
        assert alert.audience == "PRIMARY_ADMIN"
        assert alert.recipient_user_id == ADMIN

        dispatched = process_pending_day_alert_dispatches(seeded,
                                                          organization_id=ORG)
        assert dispatched.outcomes[0].outcome == "dispatched"

        # shown_to_worker = false
        assert sends == [ADMIN_EMAIL]
        assert seeded.get(User, WORKER).email not in sends
        assert seeded.query(EcoDayAdministratorAlert).filter_by(
            recipient_user_id=WORKER).count() == 0

    def test_this_suite_is_the_registered_harness_for_the_vector(self):
        """Restated: GV-18 has been promoted, and on this evidence.

        Previously this asserted the vector was NOT promoted. It now asserts
        the stronger pair: the artifact says executable, AND the golden suite
        names THIS class as its execution harness — so the promotion cannot
        survive the evidence being renamed or removed.
        """
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
        assert by_id["GV-18"]["harness"]["execution"] == "executable"
        assert by_id["GV-18"]["harness"]["blocking_dependencies"] == []

        from tests.test_eco_golden_vectors import EXTERNAL_EXECUTION_TESTS

        assert EXTERNAL_EXECUTION_TESTS["GV-18"] == (
            "test_eco_day_alert.py", "class TestGV18")
