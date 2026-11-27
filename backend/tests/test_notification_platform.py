"""
The notification platform foundation.
=====================================

ZT-SPEC-ECO-002 v2.0 §34; owner rulings D-1…D-4; architecture decisions D-A
(internal platform module) and D-B (a distinct non-suppressible administrator
class).

Proves the ordering D-2 fixes, the two identities D-4 forbids collapsing, the
correlation contract D-3 sets, and — from several directions — that no provider
detail and no Worker PII crosses the boundary.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa

from app.notification_platform import (
    MAX_CORRELATION_ID_CHARS,
    MAX_FAILURE_DETAIL_CHARS,
    SUPPRESSION_REASON_CODES,
    Channel,
    DispatchMode,
    DispatchOutcome,
    DispatchResult,
    NotificationDispatchRequest,
    NotificationHandoff,
    ReasonCode,
    dispatch,
    is_platform_correlation_id,
    new_correlation_id,
    notification_class_for,
    request_key,
    select_channel,
)
from app.notification_platform import idempotency
from app.notifications import classification as _classes

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "notification_platform"

ORG = 7100
OTHER_ORG = 7200
ADMIN = 71001
WORKER = 71002
OUTSIDER = 72001

WORKER_NAME = "Jordan Smith"
WORKER_EMAIL = "jordan.smith@example.test"


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    # AuditLog is where `notifications.router` stores per-user preferences, so
    # the suppressible (WORKER) path reads it. Without it that path raises and
    # the platform correctly reports PLATFORM_ERROR — which would make these
    # tests pass for the wrong reason.
    from database.models import AuditLog, Organization, User

    for model in (Organization, User, AuditLog, NotificationDispatchRequest):
        model.__table__.create(engine, checkfirst=True)


@pytest.fixture
def seeded(db):
    from database.models import Organization, User

    db.execute(sa.delete(NotificationDispatchRequest))
    for org_id in (ORG, OTHER_ORG):
        if db.get(Organization, org_id) is None:
            db.add(Organization(id=org_id, name=f"org-{org_id}",
                                billing_type="standard"))
    db.flush()
    for uid, org_id, name, email in (
        (ADMIN, ORG, "Admin Person", "admin@example.test"),
        (WORKER, ORG, WORKER_NAME, WORKER_EMAIL),
        (OUTSIDER, OTHER_ORG, "Outsider", "outsider@example.test"),
    ):
        if db.get(User, uid) is None:
            db.add(User(id=uid, name=name, email=email,
                        organization_id=org_id))
    db.flush()
    return db


@pytest.fixture(autouse=True)
def _deterministic_quiet_hours(monkeypatch):
    """Pin quiet hours OFF except where a test asserts them.

    `DEFAULT_PREFS` enables quiet hours 20:00-07:00 UTC, so a WORKER-audience
    dispatch would otherwise pass or fail by the clock. The suppression tests
    below monkeypatch `evaluate_send` directly, so they are unaffected.
    """
    import app.notifications.router as notif_router

    monkeypatch.setitem(notif_router.DEFAULT_PREFS, "quiet_hours_enabled",
                        False)


@pytest.fixture
def sends(monkeypatch):
    """Record every transport call without sending anything."""
    import app.email_service as mailer

    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(mailer, "_send", _fake)
    return calls


def _handoff(**kw):
    params = dict(
        organization_id=ORG, eco_event_id=900, recipient_user_id=ADMIN,
        audience="PRIMARY_ADMIN", notification_type="THRESHOLD_REACHED",
        content_inputs={"subject": "Early clock-out threshold reached",
                        "body": "A worker reached the weekly threshold."},
    )
    params.update(kw)
    return NotificationHandoff(**params)


def _package_files():
    return sorted(PACKAGE.glob("*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# 1–2. Successful dispatch, and the platform correlation id
# ═════════════════════════════════════════════════════════════════════════════

class TestSuccessfulDispatch:

    def test_a_successful_email_dispatch(self, seeded, sends):
        result = dispatch(seeded, _handoff())
        assert result.outcome is DispatchOutcome.DISPATCHED
        assert result.channel is Channel.EMAIL
        assert result.reason_code is ReasonCode.TRANSPORTED
        assert result.failure_detail is None
        assert len(sends) == 1

    def test_the_correlation_id_is_platform_generated(self, seeded, sends):
        result = dispatch(seeded, _handoff())
        assert is_platform_correlation_id(result.correlation_id)
        assert result.correlation_id.startswith("ndc_")
        assert len(result.correlation_id) == MAX_CORRELATION_ID_CHARS

    def test_correlation_ids_are_unique_per_handoff(self):
        assert len({new_correlation_id() for _ in range(200)}) == 200

    def test_transported_is_not_a_receipt(self):
        """D-5.1: "notified" means the handoff was made and the outcome
        recorded. The vocabulary must not contain a receipt claim."""
        values = {r.value for r in ReasonCode}
        for banned in ("DELIVERED", "RECEIVED", "OPENED", "READ", "BOUNCED"):
            assert banned not in values


# ═════════════════════════════════════════════════════════════════════════════
# 3–7, 24. Idempotency (D-4)
# ═════════════════════════════════════════════════════════════════════════════

class TestIdempotency:

    def test_the_request_key_is_exactly_the_ruled_identity(self):
        assert request_key(eco_event_id=5, recipient_user_id=9) == "eco:5:9"

    def test_the_request_key_carries_no_channel(self):
        """D-4: ECO cannot know the channel before the platform selects it,
        which is the whole reason the request key differs from the ECO record
        identity ``(threshold_event_id, recipient_id, channel)``."""
        key = request_key(eco_event_id=5, recipient_user_id=9)
        for channel in Channel:
            assert channel.value not in key

    def test_a_duplicate_request_returns_the_original_result(self, seeded,
                                                             sends):
        first = dispatch(seeded, _handoff())
        second = dispatch(seeded, _handoff())
        assert second == first
        assert len(sends) == 1, "a replay must not reach transport"

    def test_a_duplicate_preserves_channel_and_correlation(self, seeded,
                                                           sends):
        first = dispatch(seeded, _handoff())
        second = dispatch(seeded, _handoff())
        assert second.channel is first.channel
        assert second.correlation_id == first.correlation_id
        assert second.outcome is first.outcome
        assert second.reason_code is first.reason_code

    def test_only_one_logical_dispatch_row_exists(self, seeded, sends):
        dispatch(seeded, _handoff())
        dispatch(seeded, _handoff())
        dispatch(seeded, _handoff())
        assert seeded.query(NotificationDispatchRequest).count() == 1

    def test_a_concurrent_duplicate_cannot_create_a_second_row(self, seeded,
                                                               sends):
        """The uniqueness constraint is the guarantee, not a read-then-write.

        Simulated by inserting the row directly and then dispatching: the
        service must find the claim rather than mint a second one.
        """
        handoff = _handoff()
        row, replayed = idempotency.begin(seeded, handoff)
        assert replayed is None
        with pytest.raises(idempotency.DispatchInProgress):
            dispatch(seeded, handoff)
        assert seeded.query(NotificationDispatchRequest).count() == 1
        assert sends == [], "an in-progress claim must not transport"

    def test_the_unique_constraint_rejects_a_second_claim(self, seeded):
        handoff = _handoff()
        idempotency.begin(seeded, handoff)
        duplicate = NotificationDispatchRequest(
            scope="notification_dispatch",
            idempotency_key=request_key(eco_event_id=900,
                                        recipient_user_id=ADMIN),
            organization_id=ORG, eco_event_id=900, recipient_user_id=ADMIN,
            request_hash="x" * 64, correlation_id=new_correlation_id())
        seeded.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                seeded.flush()

    def test_a_changed_payload_under_the_same_key_is_rejected(self, seeded,
                                                              sends):
        dispatch(seeded, _handoff())
        mutated = _handoff(content_inputs={"subject": "Different",
                                           "body": "Different body"})
        with pytest.raises(idempotency.RequestHashConflict):
            dispatch(seeded, mutated)
        assert len(sends) == 1

    def test_key_ordering_does_not_look_like_a_payload_change(self, seeded,
                                                              sends):
        dispatch(seeded, _handoff(
            content_inputs={"subject": "Early clock-out threshold reached",
                            "body": "A worker reached the weekly threshold."}))
        # Same content, different dict insertion order.
        replay = dispatch(seeded, _handoff(
            content_inputs={"body": "A worker reached the weekly threshold.",
                            "subject": "Early clock-out threshold reached"}))
        assert replay.reason_code is ReasonCode.TRANSPORTED
        assert len(sends) == 1

    def test_a_different_recipient_is_a_different_dispatch(self, seeded,
                                                           sends):
        dispatch(seeded, _handoff(recipient_user_id=ADMIN))
        dispatch(seeded, _handoff(recipient_user_id=WORKER,
                                  audience="WORKER"))
        assert seeded.query(NotificationDispatchRequest).count() == 2
        assert len(sends) == 2

    def test_the_platform_store_is_not_the_eco_delivery_record(self):
        """D-4: two identities, never collapsed."""
        from app.alert.models import EcoNotificationDeliveryAttempt

        assert NotificationDispatchRequest.__tablename__ != \
            EcoNotificationDeliveryAttempt.__tablename__
        platform = {c.name for c in NotificationDispatchRequest.__table__.c}
        # The platform key's components, and NO channel in its uniqueness.
        assert {"eco_event_id", "recipient_user_id"} <= platform
        unique = next(c for c in NotificationDispatchRequest.__table__.constraints
                      if getattr(c, "name", "")
                      == "uq_notification_dispatch_request_key")
        assert [c.name for c in unique.columns] == ["scope", "idempotency_key"]


# ═════════════════════════════════════════════════════════════════════════════
# 8–11. Ordering, suppression and failure (D-2)
# ═════════════════════════════════════════════════════════════════════════════

class TestOrderingAndSuppression:

    def test_the_channel_is_selected_before_suppression(self):
        """D-2's ordering, asserted on the source.

        It is what guarantees a channel exists on a SUPPRESSED result, which is
        in turn what lets ECO form its three-part record identity for one.
        """
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert source.index("# ── 1. CHANNEL SELECTION") < \
            source.index("# ── 2. PREFERENCE / SUPPRESSION") < \
            source.index("# ── 3. TRANSPORT")

    def test_a_suppressed_notification_is_dispatched_with_a_reason(
            self, seeded, sends, monkeypatch):
        import app.notifications.router as notif_router

        monkeypatch.setattr(notif_router, "evaluate_send",
                            lambda **kw: (False, "quiet_hours"))
        result = dispatch(seeded, _handoff(recipient_user_id=WORKER,
                                           audience="WORKER"))
        assert result.outcome is DispatchOutcome.DISPATCHED
        assert result.reason_code is ReasonCode.SUPPRESSED_QUIET_HOURS
        assert result.suppressed is True
        assert sends == [], "suppression must not reach transport"

    def test_a_suppressed_result_still_carries_the_selected_channel(
            self, seeded, sends, monkeypatch):
        import app.notifications.router as notif_router

        monkeypatch.setattr(notif_router, "evaluate_send",
                            lambda **kw: (False, "type_disabled"))
        result = dispatch(seeded, _handoff(recipient_user_id=WORKER,
                                           audience="WORKER"))
        assert result.channel is Channel.EMAIL

    def test_suppression_is_not_a_third_outcome(self):
        assert {o.value for o in DispatchOutcome} == {"dispatched", "failed"}
        for code in SUPPRESSION_REASON_CODES:
            assert DispatchResult(
                outcome=DispatchOutcome.DISPATCHED, channel=Channel.EMAIL,
                reason_code=code, correlation_id=new_correlation_id(),
            ).suppressed is True

    def test_outcome_and_reason_code_cannot_disagree(self):
        with pytest.raises(ValueError):
            DispatchResult(outcome=DispatchOutcome.FAILED,
                           channel=Channel.EMAIL,
                           reason_code=ReasonCode.TRANSPORTED,
                           correlation_id=new_correlation_id())
        with pytest.raises(ValueError):
            DispatchResult(outcome=DispatchOutcome.DISPATCHED,
                           channel=Channel.EMAIL,
                           reason_code=ReasonCode.TRANSPORT_FAILED,
                           correlation_id=new_correlation_id())

    def test_a_transport_failure_returns_failed(self, seeded, monkeypatch):
        import app.email_service as mailer

        monkeypatch.setattr(mailer, "_send", lambda **kw: False)
        result = dispatch(seeded, _handoff())
        assert result.outcome is DispatchOutcome.FAILED
        assert result.reason_code is ReasonCode.TRANSPORT_FAILED
        assert result.channel is Channel.EMAIL

    def test_no_eligible_channel_is_failed_not_suppression(self, seeded):
        """A policy declined nothing; there was nothing to decline. D-2's
        ordering presupposes a selected channel, so this can only be FAILED."""
        result = dispatch(seeded, _handoff(audience="NOT_AN_AUDIENCE"))
        assert result.outcome is DispatchOutcome.FAILED
        assert result.reason_code is ReasonCode.INVALID_HANDOFF
        assert result.channel is None
        assert select_channel(_handoff(audience="NOT_AN_AUDIENCE")) is None

    def test_a_recipient_with_no_address_is_undeliverable(self, seeded,
                                                          monkeypatch):
        from database.models import User

        seeded.execute(sa.update(User).where(User.id == ADMIN)
                       .values(email=""))
        seeded.flush()
        result = dispatch(seeded, _handoff())
        assert result.outcome is DispatchOutcome.FAILED
        assert result.reason_code is ReasonCode.RECIPIENT_UNDELIVERABLE

    def test_the_five_categories_are_distinguishable(self):
        for code in (ReasonCode.TRANSPORTED,
                     ReasonCode.SUPPRESSED_BY_PREFERENCE,
                     ReasonCode.TRANSPORT_FAILED,
                     ReasonCode.CHANNEL_NOT_CONFIGURED,
                     ReasonCode.PLATFORM_ERROR):
            assert isinstance(code.value, str) and code.value.isupper()
        assert ReasonCode.TRANSPORTED not in SUPPRESSION_REASON_CODES
        assert ReasonCode.NO_ELIGIBLE_CHANNEL not in SUPPRESSION_REASON_CODES


# ═════════════════════════════════════════════════════════════════════════════
# 12–15. Sanitisation, PII and provider containment
# ═════════════════════════════════════════════════════════════════════════════

class TestSanitisation:

    def test_provider_diagnostics_never_reach_the_result(self, seeded,
                                                          monkeypatch):
        import app.email_service as mailer

        def _boom(**kw):
            raise RuntimeError(
                f"Resend 422: invalid recipient {WORKER_EMAIL} "
                f"for {WORKER_NAME}; api_key=sk_live_SECRET")

        monkeypatch.setattr(mailer, "_send", _boom)
        result = dispatch(seeded, _handoff())
        assert result.outcome is DispatchOutcome.FAILED
        detail = result.failure_detail or ""
        assert WORKER_EMAIL not in detail
        assert WORKER_NAME not in detail
        assert "sk_live" not in detail
        assert "422" not in detail
        assert "RuntimeError" in detail

    def test_failure_detail_never_contains_worker_pii(self, seeded,
                                                      monkeypatch):
        from database.models import User

        seeded.execute(sa.update(User).where(User.id == ADMIN)
                       .values(email=""))
        seeded.flush()
        result = dispatch(seeded, _handoff())
        detail = result.failure_detail or ""
        assert "@" not in detail
        assert WORKER_NAME not in detail

    def test_failure_detail_is_bounded(self):
        assert MAX_FAILURE_DETAIL_CHARS == 512
        with pytest.raises(ValueError):
            DispatchResult(outcome=DispatchOutcome.FAILED, channel=None,
                           reason_code=ReasonCode.PLATFORM_ERROR,
                           correlation_id=new_correlation_id(),
                           failure_detail="x" * 513)

    def test_failure_detail_may_be_null(self, seeded, sends):
        assert dispatch(seeded, _handoff()).failure_detail is None

    def test_a_provider_id_can_never_be_a_correlation_id(self):
        """D-3. Provider ids are not in this format, so a leak is detectable
        rather than merely forbidden."""
        for provider_id in ("re_abc123", "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794",
                            "<CADnHK@mail.example>", "", "ndc_short"):
            assert not is_platform_correlation_id(provider_id)
        assert is_platform_correlation_id(new_correlation_id())

    def test_the_result_contract_has_no_provider_field(self):
        fields = set(DispatchResult.__dataclass_fields__)
        assert fields == {"outcome", "channel", "reason_code",
                          "correlation_id", "failure_detail"}
        for banned in ("provider", "provider_id", "message_id", "destination",
                       "email", "address"):
            assert banned not in fields

    def test_the_handoff_contract_forbids_caller_owned_platform_fields(self):
        """§34, expressed as a shape: a caller cannot supply what it may not
        decide, because there is nowhere to put it."""
        fields = set(NotificationHandoff.__dataclass_fields__)
        for banned in ("channel", "provider", "email", "address", "retry",
                       "suppression", "preference", "rendered_body",
                       "transport"):
            assert banned not in fields


# ═════════════════════════════════════════════════════════════════════════════
# 16. Provider fallback stays one channel
# ═════════════════════════════════════════════════════════════════════════════

class TestChannelVocabulary:

    def test_resend_and_smtp_are_one_channel(self):
        """Falling back between providers does not change the channel."""
        assert {c.value for c in Channel} == {"email"}
        for banned in ("resend", "smtp", "sendgrid", "ses"):
            assert banned not in {c.value for c in Channel}

    def test_no_fake_multi_channel_support_is_claimed(self):
        for banned in ("sms", "push", "in_app", "webhook", "slack"):
            assert banned not in {c.value for c in Channel}

    def test_the_selected_channel_is_reported(self, seeded, sends):
        assert dispatch(seeded, _handoff()).channel is Channel.EMAIL


# ═════════════════════════════════════════════════════════════════════════════
# 17–19. The administrator class (D-B)
# ═════════════════════════════════════════════════════════════════════════════

class TestAdministratorClass:

    def test_the_eco_admin_class_exists_and_is_distinct(self):
        assert _classes.ECO_ADMIN_OPERATIONAL == "eco_admin_operational"
        assert _classes.ECO_ADMIN_OPERATIONAL != _classes.OPERATIONAL
        assert _classes.ECO_ADMIN_OPERATIONAL in _classes.ALL

    def test_it_is_non_suppressible(self):
        """§33.1's "Always", enforced by the vocabulary rather than a branch."""
        assert _classes.ECO_ADMIN_OPERATIONAL not in _classes.SUPPRESSIBLE

    def test_it_is_not_quiet_hours_eligible(self):
        assert _classes.ECO_ADMIN_OPERATIONAL not in \
            _classes.QUIET_HOURS_ELIGIBLE

    def test_it_is_not_marketing_like(self):
        assert _classes.ECO_ADMIN_OPERATIONAL not in _classes.MARKETING_LIKE

    def test_the_existing_operational_semantics_are_unchanged(self):
        """D-B must not alter what OPERATIONAL means for everybody else."""
        assert _classes.OPERATIONAL in _classes.SUPPRESSIBLE
        assert _classes.OPERATIONAL in _classes.QUIET_HOURS_ELIGIBLE
        assert _classes.SUPPRESSIBLE == {
            _classes.OPERATIONAL, _classes.CUSTOMER_SUCCESS,
            _classes.MARKETING}

    def test_no_existing_family_moved_class(self):
        for family, (cls, _sender) in _classes.FAMILIES.items():
            assert cls != _classes.ECO_ADMIN_OPERATIONAL, family

    def test_administrator_audiences_carry_the_admin_class(self):
        for audience in ("PRIMARY_ADMIN", "DIRECT_MANAGER", "HR_ADMIN"):
            assert notification_class_for(_handoff(audience=audience)) == \
                _classes.ECO_ADMIN_OPERATIONAL

    def test_the_worker_audience_stays_suppressible(self):
        """§34: a Worker with notifications disabled still has the record."""
        assert notification_class_for(_handoff(audience="WORKER")) == \
            _classes.OPERATIONAL

    def test_an_admin_notification_is_not_suppressed(self, seeded, sends,
                                                     monkeypatch):
        import app.notifications.router as notif_router

        # Even with every preference gate answering "no", the admin class is
        # not a member of SUPPRESSIBLE, so the gate is never consulted.
        monkeypatch.setattr(notif_router, "evaluate_send",
                            lambda **kw: (False, "quiet_hours"))
        result = dispatch(seeded, _handoff(audience="PRIMARY_ADMIN"))
        assert result.reason_code is ReasonCode.TRANSPORTED
        assert len(sends) == 1

    def test_no_eco_specific_bypass_was_added_to_suppression_logic(self):
        """D-B option A, not option B: the guarantee is the vocabulary, so no
        ECO-shaped conditional may appear in the generic gate."""
        for name in ("router.py", "suppression.py", "classification.py"):
            source = (BACKEND / "app" / "notifications" / name).read_text(
                encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    rendered = ast.dump(node.test)
                    assert "eco" not in rendered.lower(), \
                        f"{name} branches on ECO inside generic logic"


# ═════════════════════════════════════════════════════════════════════════════
# 20. SHADOW (defence in depth)
# ═════════════════════════════════════════════════════════════════════════════

class TestShadow:

    def test_shadow_never_reaches_transport(self, seeded, sends):
        result = dispatch(seeded, _handoff(mode=DispatchMode.SHADOW))
        assert result.outcome is DispatchOutcome.FAILED
        assert result.reason_code is ReasonCode.SHADOW_REFUSED
        assert sends == []

    def test_shadow_writes_no_idempotency_row(self, seeded, sends):
        """A backstop must leave no state, or it becomes a parallel path."""
        dispatch(seeded, _handoff(mode=DispatchMode.SHADOW))
        assert seeded.query(NotificationDispatchRequest).count() == 0

    def test_shadow_does_not_consume_the_key(self, seeded, sends):
        """A SHADOW refusal must not block the later OPERATIONAL handoff."""
        dispatch(seeded, _handoff(mode=DispatchMode.SHADOW))
        result = dispatch(seeded, _handoff(mode=DispatchMode.OPERATIONAL))
        assert result.reason_code is ReasonCode.TRANSPORTED

    def test_the_platform_is_not_a_second_rollout_authority(self):
        """ECO owns rollout. The platform must not read the rollout control."""
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "rollout" not in node.module, path.name
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "rollout" not in alias.name, path.name


# ═════════════════════════════════════════════════════════════════════════════
# 21–23. Tenant isolation, validation, independent recipients
# ═════════════════════════════════════════════════════════════════════════════

class TestTenantIsolationAndValidation:

    def test_a_cross_tenant_recipient_is_refused(self, seeded, sends):
        result = dispatch(seeded, _handoff(recipient_user_id=OUTSIDER))
        assert result.outcome is DispatchOutcome.FAILED
        assert result.reason_code is ReasonCode.INVALID_HANDOFF
        assert sends == []

    def test_a_cross_tenant_refusal_claims_no_key(self, seeded, sends):
        dispatch(seeded, _handoff(recipient_user_id=OUTSIDER))
        assert seeded.query(NotificationDispatchRequest).count() == 0

    def test_an_unknown_recipient_is_refused(self, seeded, sends):
        result = dispatch(seeded, _handoff(recipient_user_id=999999))
        assert result.reason_code is ReasonCode.INVALID_HANDOFF
        assert sends == []

    def test_an_unknown_audience_is_refused(self, seeded, sends):
        result = dispatch(seeded, _handoff(audience="TEAM_LEAD"))
        assert result.reason_code is ReasonCode.INVALID_HANDOFF
        assert sends == []

    def test_a_malformed_handoff_is_refused(self, seeded, sends):
        for kw in ({"eco_event_id": 0}, {"organization_id": -1},
                   {"notification_type": ""}, {"audience": ""}):
            result = dispatch(seeded, _handoff(**kw))
            assert result.reason_code is ReasonCode.INVALID_HANDOFF, kw
        assert sends == []

    def test_a_refusal_still_carries_a_correlation_id(self, seeded, sends):
        result = dispatch(seeded, _handoff(recipient_user_id=OUTSIDER))
        assert is_platform_correlation_id(result.correlation_id)

    def test_multiple_recipients_get_independent_results(self, seeded,
                                                          monkeypatch):
        import app.email_service as mailer

        seen = []

        def _selective(**kw):
            seen.append(kw["to"])
            return kw["to"] != WORKER_EMAIL     # the Worker's send fails

        monkeypatch.setattr(mailer, "_send", _selective)
        admin = dispatch(seeded, _handoff(recipient_user_id=ADMIN,
                                          audience="PRIMARY_ADMIN"))
        worker = dispatch(seeded, _handoff(recipient_user_id=WORKER,
                                           audience="WORKER"))
        assert admin.outcome is DispatchOutcome.DISPATCHED
        assert worker.outcome is DispatchOutcome.FAILED
        assert admin.correlation_id != worker.correlation_id
        assert len(seen) == 2


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_platform_imports_no_eco_package(self):
        """D-A places the module in-process, so the boundary is enforced by
        tests rather than by a network hop. This is that enforcement."""
        eco = ("eval_day", "eval_week", "eval_event", "alert", "work_ledger",
               "policy_version", "recipient_authority", "eco_day_view",
               "eco_view_authority", "early_clock_out", "rollout")
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    for package in eco:
                        assert f"app.{package}" not in name, \
                            f"{path.name} imports {name}"

    def test_no_eco_package_imports_the_transport(self):
        """ECO may call the platform. ECO may NEVER call email transport."""
        for package in ("eval_event", "alert", "eval_day", "eval_week",
                        "recipient_authority"):
            root = BACKEND / "app" / package
            if not root.exists():
                continue
            for path in sorted(root.glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for name in names:
                        assert "email_service" not in name, \
                            f"{package}/{path.name} imports {name}"

    def test_only_the_authorized_module_wires_the_platform(self):
        """Restated for ruling D-C: B, which authorised exactly one consumer.

        This previously asserted that NO ECO path called the platform. D-C
        authorised ``app/alert/dispatch_processing.py`` — the persist-first
        processing step — so the claim becomes the narrower and stronger one:
        that module is the only ECO code that touches the boundary, and in
        particular ``eval_event`` still does not, which is what keeps event
        creation independent of transport.
        """
        wired = []
        for package in ("eval_event", "alert"):
            for path in sorted((BACKEND / "app" / package).glob("*.py")):
                if "notification_platform" in path.read_text(encoding="utf-8"):
                    wired.append(f"{package}/{path.name}")
        assert wired == ["alert/__init__.py", "alert/dispatch_processing.py"]

        # The load-bearing half: EVAL-EVENT must never reach the boundary.
        for path in sorted((BACKEND / "app" / "eval_event").glob("*.py")):
            assert "notification_platform" not in path.read_text(
                encoding="utf-8"), path.name

    def test_exactly_one_authorized_trigger_exists(self):
        """Restated: D-C's trigger has since been authorized.

        One caller, and it is the rollout gate. No unauthorized, ungated entry
        point may appear beside it — which is the property that actually
        matters now that a caller is permitted to exist at all.
        """
        callers = []
        for path in (BACKEND / "app").rglob("*.py"):
            if path.name in ("dispatch_processing.py", "__init__.py"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8",
                                                errors="ignore"))
            except SyntaxError:  # pragma: no cover
                continue
            # CALLS, not text: `eco_day_alert/dispatch.py` names this function
            # in prose to explain why it does not widen it.
            called = {n.func.id for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)}
            if "process_pending_notification_dispatches" in called:
                callers.append(path.name)
        assert callers == ["dispatch_trigger.py"]

    def test_the_platform_implements_no_retry(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            identifiers = {n.id for n in ast.walk(tree)
                           if isinstance(n, ast.Name)}
            identifiers |= {n.name for n in ast.walk(tree)
                            if isinstance(n, ast.FunctionDef)}
            for banned in ("retry", "backoff", "requeue", "redeliver"):
                assert banned not in identifiers, f"{path.name} has {banned}"

    def test_no_http_surface_exists(self):
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "@router.", "fastapi"):
                assert banned not in source, path.name

    def test_no_broker_or_second_service_was_introduced(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    for banned in ("celery", "redis", "kombu", "pika",
                                   "kafka", "boto3", "httpx", "requests"):
                        assert not name.startswith(banned), \
                            f"{path.name} imports {name}"

    def test_eco_v1_and_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

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
    def test_the_eco_delivery_record_can_hold_the_platform_result(self):
        from app.alert.models import EcoNotificationDeliveryAttempt

        columns = {c.name for c in EcoNotificationDeliveryAttempt.__table__.c}
        for required in ("channel", "outcome", "reason_code",
                         "correlation_id", "failure_detail", "mode",
                         "recipient_user_id", "attempted_at_utc"):
            assert required in columns, required

    def test_the_eco_outcome_vocabulary_is_pinned_in_the_database(self):
        from app.alert.models import EcoNotificationDeliveryAttempt

        check = next(
            c for c in EcoNotificationDeliveryAttempt.__table__.constraints
            if getattr(c, "name", "") == "ck_eco_notification_attempt_outcome")
        rendered = str(check.sqltext)
        assert "'dispatched'" in rendered and "'failed'" in rendered
        for banned in ("pending", "queued", "retrying", "suppressed",
                       "delivered", "opened", "bounced"):
            assert f"'{banned}'" not in rendered
