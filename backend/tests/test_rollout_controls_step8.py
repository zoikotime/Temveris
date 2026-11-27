"""
STEP 8 — durable rollout controls.
==================================

The property under test throughout is that NOTHING turns this control on except
a deliberate, valid, observed transition — and that every other outcome is OFF.

Owner ruling D6 added a second precondition to promotion: §44.3's enablement
gates are machine-enforced at SHADOW → ENABLED. The lifecycle tests below
therefore operate on Tenants made gate-ready by ``tests/_eco_enablement.py``,
and ``TestEnablementGates`` covers the refusal on one that is not. Readiness is
set up ONCE per module rather than per test, because these tests share an
in-memory database and committed rows outlive the transaction.

Expected values are written independently. State strings are literals here
("OFF", "SHADOW", "ENABLED") rather than imports of the enum, so a rename in the
implementation cannot silently agree with itself; the enum is used only where
the API requires it.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.rollout import (
    CONTROL_EARLY_CLOCK_OUT_V2,
    EnablementGatesNotSatisfied,
    InvalidRolloutTransition,
    ObservationWindowNotElapsed,
    RolloutControl,
    RolloutOrganizationRequired,
    RolloutState,
    StaleRolloutVersion,
    UnknownRolloutControl,
    canary_organizations,
    coerce_state,
    get_status,
    is_enabled,
    is_shadow,
    organizations_in_state,
    rollout_state,
    set_canary,
    set_observation_days,
    transition,
)

UTC = timezone.utc
ORG = 31
OTHER_ORG = 32
#: Deliberately never made §44.3-ready. Promotion of this one must be refused.
UNREADY_ORG = 33
#: Nothing configured at all: even entering SHADOW must be refused (D6).
BARE_ORG = 34
#: The five operational gates and NOTHING else — enters SHADOW, cannot be
#: promoted. This is the pair D6 splits, and the reason the split matters.
READY_NO_COMPLIANCE_ORG = 35
#: Fully ready, used for the ENABLED → SHADOW path.
REENTRY_ORG = 36
ADMIN, OTHER_ADMIN, UNREADY_ADMIN = 3101, 3201, 3301
BARE_ADMIN, READY_NC_ADMIN, REENTRY_ADMIN = 3401, 3501, 3601

#: One organization per withheld gate, so the parameterised test can leave a
#: different gate unsatisfied in each without the others interfering.
PARTIAL_ORGS = {
    "WORKWEEK_TIMEZONE_ATTRIBUTION": (41, 4101),
    "REQUIREMENT_FALLBACK": (42, 4201),
    "NETTING_DECISION": (43, 4301),
    "BREAK_TREATMENT": (44, 4401),
    "ADMINISTRATIVE_RECIPIENT": (45, 4501),
}
KEY = CONTROL_EARLY_CLOCK_OUT_V2

#: Written literally, never imported: these are the strings the durable column
#: must hold, and a test that imported them could not detect a rename.
OFF, SHADOW, ENABLED = "OFF", "SHADOW", "ENABLED"

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "rollout"
MIGRATION = (
    BACKEND / "alembic" / "versions" / "a7e2d5f8b031_durable_rollout_control.py"
)
EXPECTED_REVISION = "a7e2d5f8b031"
EXPECTED_PARENT_REVISION = "f1a6b9c4d283"


@pytest.fixture(scope="module", autouse=True)
def _step8_schema(engine):
    """Schema and §44.3 readiness, once.

    ``transition`` now composes the gate checker for this control, so the
    tables the checker reads have to exist — an absent one would surface as
    "no such table" inside a promotion rather than as a missing gate.
    """
    from sqlalchemy.orm import Session as _S

    import database.models as dbm
    from tests import _eco_enablement as ready

    ready.create_schema(engine)

    with _S(engine, expire_on_commit=False) as s:
        everyone = [(ORG, ADMIN), (OTHER_ORG, OTHER_ADMIN),
                    (UNREADY_ORG, UNREADY_ADMIN), (BARE_ORG, BARE_ADMIN),
                    (READY_NO_COMPLIANCE_ORG, READY_NC_ADMIN),
                    (REENTRY_ORG, REENTRY_ADMIN)]
        everyone += list(PARTIAL_ORGS.values())
        for org, admin in everyone:
            if s.get(dbm.Organization, org) is None:
                s.add(dbm.Organization(id=org, name=f"Rollout Org {org}"))
                s.commit()
            if s.get(dbm.User, admin) is None:
                s.add(dbm.User(id=admin, name="Admin",
                               email=f"{admin}@rollout.test",
                               organization_id=org, role="admin"))
                s.commit()

        # UNREADY_ORG and BARE_ORG are given nothing on purpose. The
        # PARTIAL_ORGS are configured per-test, by the test that needs them.
        ready.classification_evidence(s)
        for org, admin in ((ORG, ADMIN), (OTHER_ORG, OTHER_ADMIN),
                           (REENTRY_ORG, REENTRY_ADMIN)):
            ready.jurisdiction_evidence(s, organization_id=org)
            ready.operational_evidence(s, organization_id=org,
                                       admin_user_id=admin)
            ready.compliance_evidence(s, organization_id=org,
                                      admin_user_id=admin)

        # The five operational gates only. No DPIA, no attestation, no
        # worker notice — D6 does not bind those to entering SHADOW.
        ready.operational_evidence(s, organization_id=READY_NO_COMPLIANCE_ORG,
                                   admin_user_id=READY_NC_ADMIN)

        # UNREADY_ORG needs the operational five so its promotion refusal is
        # about COMPLIANCE, and so it can still enter SHADOW at all.
        ready.operational_evidence(s, organization_id=UNREADY_ORG,
                                   admin_user_id=UNREADY_ADMIN)


def _to_shadow(db, *, org=ORG, at=T0, version=None):
    return transition(
        db, organization_id=org, control_key=KEY, target=RolloutState.SHADOW,
        expected_version=version, now=at, actor_label="admin",
    )


def _to_enabled(db, *, org=ORG, version, at=None):
    return transition(
        db, organization_id=org, control_key=KEY, target=RolloutState.ENABLED,
        expected_version=version, now=at or (T0 + timedelta(days=15)),
        actor_label="admin",
    )


def _to_off(db, *, org=ORG, at=None):
    """Return a control to OFF. This module shares one in-memory database, so
    a test that leaves a control ENABLED changes what the next one starts
    from."""
    st = get_status(db, organization_id=org, control_key=KEY)
    if st.state is RolloutState.OFF:
        return st
    return transition(
        db, organization_id=org, control_key=KEY, target=RolloutState.OFF,
        expected_version=st.state_version, now=at or (T0 + timedelta(days=20)),
        actor_label="admin")


def _restart_shadow(db, *, org=ORG, at=T0):
    """Put a control into a FRESH shadow period, whatever state it is in.

    ``_to_shadow`` passes ``version=None``, which means "no row yet" -- true
    only until the first test in this module creates one.
    """
    _to_off(db, org=org, at=at)
    st = get_status(db, organization_id=org, control_key=KEY)
    return transition(
        db, organization_id=org, control_key=KEY, target=RolloutState.SHADOW,
        # ``None`` means "no row yet"; an absent control reports version 0,
        # which is a different claim and is rejected as stale.
        expected_version=(st.state_version if st.exists else None),
        now=at, actor_label="admin")


def _audit_rows(db, action="RolloutTransition"):
    from database import models

    return db.execute(
        sa.select(models.AuditLog)
        .where(models.AuditLog.entity == "RolloutControl",
               models.AuditLog.action == action)
        .order_by(models.AuditLog.id)
    ).scalars().all()


# ═════════════════════════════════════════════════════════════════════════════
# Default OFF — the property everything else rests on
# ═════════════════════════════════════════════════════════════════════════════
class TestDefaultOff:
    def test_a_control_with_no_row_is_off(self, db):
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF

    def test_no_row_is_not_enabled(self, db):
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is False
        assert is_shadow(db, organization_id=ORG, control_key=KEY) is False

    def test_a_missing_organization_is_off(self, db):
        assert rollout_state(
            db, organization_id=None, control_key=KEY
        ).value == OFF

    def test_an_unknown_control_key_is_off(self, db):
        assert rollout_state(
            db, organization_id=ORG, control_key="no_such_control"
        ).value == OFF

    def test_status_distinguishes_never_touched_from_turned_off(self, db):
        fresh = get_status(db, organization_id=ORG, control_key=KEY)
        assert fresh.state.value == OFF
        assert fresh.exists is False

        _to_shadow(db)
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=1,
                   now=T0 + timedelta(days=1))
        turned_off = get_status(db, organization_id=ORG, control_key=KEY)
        assert turned_off.state.value == OFF
        assert turned_off.exists is True

    def test_the_eco_control_has_no_rows_at_all(self, db):
        """The required final state of Step 8."""
        count = db.execute(
            sa.select(sa.func.count()).select_from(RolloutControl)
            .where(RolloutControl.control_key == KEY)
        ).scalar()
        assert count == 0


# ═════════════════════════════════════════════════════════════════════════════
# Fail-safe coercion
# ═════════════════════════════════════════════════════════════════════════════
class TestFailSafeCoercion:
    @pytest.mark.parametrize("raw", [
        None, "", " ", "off", "Off", "shadow", "enabled", "Enabled",
        " ENABLED", "ENABLED ", "ON", "TRUE", "1", 1, True, 0, [], {},
        object(), "DISABLED", "ENABLE", "ENABLEDD",
    ])
    def test_everything_unrecognised_resolves_to_off(self, raw):
        assert coerce_state(raw).value == OFF

    @pytest.mark.parametrize("raw,expected", [
        ("OFF", OFF), ("SHADOW", SHADOW), ("ENABLED", ENABLED),
    ])
    def test_only_exact_values_resolve(self, raw, expected):
        assert coerce_state(raw).value == expected

    def test_a_corrupted_persisted_state_reads_as_off(self, db):
        """End-to-end, not just the pure function.

        The column deliberately has no CHECK constraint so this path is
        reachable — a guarantee that cannot be exercised is not a guarantee.
        """
        _to_shadow(db)
        db.execute(
            sa.update(RolloutControl)
            .where(RolloutControl.organization_id == ORG)
            .values(state="TOTALLY-BOGUS")
        )
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is False
        db.rollback()

    def test_a_lowercase_persisted_state_reads_as_off(self, db):
        """Case is not normalised: the vocabulary is closed or it is not."""
        _to_shadow(db)
        db.execute(
            sa.update(RolloutControl)
            .where(RolloutControl.organization_id == ORG)
            .values(state="enabled")
        )
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF
        db.rollback()

    def test_a_row_with_an_unreadable_state_is_listed_in_no_state(self, db):
        _to_shadow(db)
        db.execute(
            sa.update(RolloutControl)
            .where(RolloutControl.organization_id == ORG)
            .values(state="???")
        )
        for state in RolloutState:
            assert ORG not in organizations_in_state(
                db, control_key=KEY, state=state
            ), f"a corrupted row was reported as {state.value}"
        db.rollback()

    def test_a_database_error_while_reading_is_off_not_an_exception(self, db):
        """A rollout control must not be able to fail the request consulting it."""
        class _Exploding:
            def execute(self, *a, **kw):
                raise RuntimeError("database is on fire")

        assert rollout_state(
            _Exploding(), organization_id=ORG, control_key=KEY
        ).value == OFF


# ═════════════════════════════════════════════════════════════════════════════
# The lifecycle
# ═════════════════════════════════════════════════════════════════════════════
class TestTransitions:
    def test_off_to_shadow(self, db):
        status = _to_shadow(db)
        assert status.state.value == SHADOW
        assert status.state_version == 1
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW

    def test_shadow_to_enabled_after_the_window(self, db):
        _to_shadow(db)
        status = _to_enabled(db, version=1)
        assert status.state.value == ENABLED
        assert status.state_version == 2
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is True

    def test_off_to_enabled_does_not_exist(self, db):
        """The rule that makes observation unavoidable rather than advisory."""
        with pytest.raises(InvalidRolloutTransition) as excinfo:
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.ENABLED, expected_version=None,
                       now=T0)
        assert "SHADOW" in str(excinfo.value)
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF
        db.rollback()

    def test_off_to_enabled_is_refused_even_on_an_existing_row(self, db):
        _to_shadow(db)
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=1,
                   now=T0 + timedelta(days=1))
        with pytest.raises(InvalidRolloutTransition):
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.ENABLED, expected_version=2,
                       now=T0 + timedelta(days=100))
        db.rollback()

    @pytest.mark.parametrize("target", [SHADOW, OFF])
    def test_rollback_from_enabled(self, db, target):
        _to_shadow(db)
        _to_enabled(db, version=1)
        status = transition(
            db, organization_id=ORG, control_key=KEY,
            target=RolloutState(target), expected_version=2,
            now=T0 + timedelta(days=16),
        )
        assert status.state.value == target
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is False

    def test_rollback_is_never_delayed_by_the_observation_window(self, db):
        """A control that is hard to turn off is not a safety control."""
        _to_shadow(db)
        _to_enabled(db, version=1)
        # One second later — no waiting.
        status = transition(
            db, organization_id=ORG, control_key=KEY, target=RolloutState.OFF,
            expected_version=2, now=T0 + timedelta(days=15, seconds=1),
        )
        assert status.state.value == OFF

    def test_shadow_to_off_rollback(self, db):
        _to_shadow(db)
        status = transition(
            db, organization_id=ORG, control_key=KEY, target=RolloutState.OFF,
            expected_version=1, now=T0 + timedelta(hours=1),
        )
        assert status.state.value == OFF

    def test_requesting_the_current_state_is_a_no_op(self, db):
        first = _to_shadow(db)
        again = _to_shadow(db, version=1, at=T0 + timedelta(days=1))
        assert again.state.value == SHADOW
        assert again.state_version == first.state_version == 1
        assert len(_audit_rows(db)) == 1, "a non-event was audited"

    def test_a_non_state_target_is_refused(self, db):
        with pytest.raises(InvalidRolloutTransition):
            transition(db, organization_id=ORG, control_key=KEY,
                       target="ENABLED", expected_version=None, now=T0)
        db.rollback()

    def test_an_undeclared_control_cannot_be_transitioned(self, db):
        with pytest.raises(UnknownRolloutControl):
            transition(db, organization_id=ORG, control_key="typo_control",
                       target=RolloutState.SHADOW, expected_version=None, now=T0)
        db.rollback()

    def test_a_transition_without_an_organization_is_refused(self, db):
        """There is no global rollout switch, so no organization-less change."""
        with pytest.raises(RolloutOrganizationRequired):
            transition(db, organization_id=None, control_key=KEY,
                       target=RolloutState.SHADOW, expected_version=None, now=T0)
        db.rollback()

    def test_a_refused_transition_leaves_no_row_behind(self, db):
        with pytest.raises(InvalidRolloutTransition):
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.ENABLED, expected_version=None, now=T0)
        db.rollback()
        assert get_status(db, organization_id=ORG, control_key=KEY).exists is False


# ═════════════════════════════════════════════════════════════════════════════
# Observation window
# ═════════════════════════════════════════════════════════════════════════════
class TestObservationWindow:
    def test_promotion_before_the_window_is_refused(self, db):
        _to_shadow(db)
        with pytest.raises(ObservationWindowNotElapsed) as excinfo:
            _to_enabled(db, version=1, at=T0 + timedelta(days=13, hours=23))
        assert excinfo.value.days_remaining > 0
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW
        db.rollback()

    def test_the_default_window_is_two_weeks(self, db):
        """14 days, stated here independently of the implementation constant."""
        _to_shadow(db)
        status = get_status(db, organization_id=ORG, control_key=KEY)
        assert status.min_observation_days == 14

    def test_exactly_at_the_boundary_is_permitted(self, db):
        _to_shadow(db)
        status = _to_enabled(db, version=1, at=T0 + timedelta(days=14))
        assert status.state.value == ENABLED

    def test_one_second_short_is_refused(self, db):
        _to_shadow(db)
        with pytest.raises(ObservationWindowNotElapsed):
            _to_enabled(db, version=1, at=T0 + timedelta(days=14, seconds=-1))
        db.rollback()

    def test_re_shadowing_restarts_the_clock(self, db):
        """A rolled-back control does not inherit credit for its earlier run."""
        _to_shadow(db)
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=1,
                   now=T0 + timedelta(days=13))
        restart = T0 + timedelta(days=13, hours=1)
        _to_shadow(db, version=2, at=restart)

        # 2 days after restarting is not 15 days of observation.
        with pytest.raises(ObservationWindowNotElapsed):
            _to_enabled(db, version=3, at=restart + timedelta(days=2))
        db.rollback()

    def test_elapsed_days_are_reported_while_observing(self, db):
        _to_shadow(db)
        status = get_status(db, organization_id=ORG, control_key=KEY)
        elapsed = status.observation_days_elapsed(now=T0 + timedelta(days=3))
        assert elapsed == pytest.approx(3.0)

    def test_elapsed_days_are_none_when_not_observing(self, db):
        assert get_status(
            db, organization_id=ORG, control_key=KEY
        ).observation_days_elapsed() is None

    def test_the_window_can_be_lengthened(self, db):
        _to_shadow(db)
        set_observation_days(db, organization_id=ORG, control_key=KEY,
                             days=30, expected_version=1)
        with pytest.raises(ObservationWindowNotElapsed):
            _to_enabled(db, version=2, at=T0 + timedelta(days=20))
        db.rollback()

    def test_the_window_cannot_be_shortened_while_observing(self, db):
        """Otherwise the gate is advisory: shorten to zero, promote at once."""
        _to_shadow(db)
        with pytest.raises(InvalidRolloutTransition):
            set_observation_days(db, organization_id=ORG, control_key=KEY,
                                 days=0, expected_version=1)
        db.rollback()

    def test_a_negative_window_is_refused(self, db):
        _to_shadow(db)
        with pytest.raises(InvalidRolloutTransition):
            set_observation_days(db, organization_id=ORG, control_key=KEY,
                                 days=-1, expected_version=1)
        db.rollback()

    def test_nothing_promotes_itself(self, db):
        """No scheduler, no automatic advance. Time alone changes nothing."""
        _to_shadow(db)
        # Simulate a year passing: re-read, do not transition.
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW
        status = get_status(db, organization_id=ORG, control_key=KEY)
        assert status.observation_days_elapsed(
            now=T0 + timedelta(days=365)
        ) > 350
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW


# ═════════════════════════════════════════════════════════════════════════════
# Concurrency
# ═════════════════════════════════════════════════════════════════════════════
class TestConcurrency:
    def test_a_stale_transition_is_rejected(self, db):
        _to_shadow(db)
        _to_enabled(db, version=1)
        with pytest.raises(StaleRolloutVersion):
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.OFF, expected_version=1,
                       now=T0 + timedelta(days=16))
        db.rollback()

    def test_a_first_transition_against_an_existing_row_is_rejected(self, db):
        _to_shadow(db)
        with pytest.raises(StaleRolloutVersion):
            _to_shadow(db, version=None, at=T0 + timedelta(days=1))
        db.rollback()

    def test_the_loser_of_a_race_does_not_overwrite_the_winner(self, db):
        """Two administrators, opposite intentions, one wins loudly."""
        _to_shadow(db)
        seen = get_status(db, organization_id=ORG, control_key=KEY).state_version

        # A rolls back.
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=seen,
                   now=T0 + timedelta(hours=1))
        # B, holding the same stale read, tries to promote.
        with pytest.raises(StaleRolloutVersion):
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.ENABLED, expected_version=seen,
                       now=T0 + timedelta(days=20))
        db.rollback()
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF

    def test_the_version_increments_on_every_accepted_change(self, db):
        _to_shadow(db)
        assert get_status(db, organization_id=ORG, control_key=KEY).state_version == 1
        _to_enabled(db, version=1)
        assert get_status(db, organization_id=ORG, control_key=KEY).state_version == 2
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=2,
                   now=T0 + timedelta(days=16))
        assert get_status(db, organization_id=ORG, control_key=KEY).state_version == 3

    def test_a_stale_canary_designation_is_rejected(self, db):
        _to_shadow(db)
        with pytest.raises(StaleRolloutVersion):
            set_canary(db, organization_id=ORG, control_key=KEY,
                       is_canary=True, expected_version=99)
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Organization scope and canaries
# ═════════════════════════════════════════════════════════════════════════════
class TestOrganizationScope:
    def test_enabling_one_organization_does_not_touch_another(self, db):
        _to_shadow(db, org=ORG)
        _to_enabled(db, org=ORG, version=1)
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is True
        assert is_enabled(db, organization_id=OTHER_ORG, control_key=KEY) is False
        assert rollout_state(
            db, organization_id=OTHER_ORG, control_key=KEY
        ).value == OFF

    def test_there_is_no_row_shape_meaning_every_organization(self, db):
        """A global switch is unrepresentable: organization_id is NOT NULL."""
        assert RolloutControl.__table__.c.organization_id.nullable is False

    def test_two_organizations_hold_independent_states(self, db):
        _to_shadow(db, org=ORG)
        _to_shadow(db, org=OTHER_ORG)
        _to_enabled(db, org=ORG, version=1)
        assert organizations_in_state(
            db, control_key=KEY, state=RolloutState.ENABLED
        ) == (ORG,)
        assert organizations_in_state(
            db, control_key=KEY, state=RolloutState.SHADOW
        ) == (OTHER_ORG,)

    def test_one_row_per_organization_and_control(self, db):
        _to_shadow(db)
        db.add(RolloutControl(
            organization_id=ORG, control_key=KEY, state=OFF, state_version=1,
            is_canary=False, min_observation_days=14,
            last_transition_at=T0, last_transition_by="TEST",
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


class TestCanary:
    def test_canary_is_off_by_default(self, db):
        _to_shadow(db)
        assert get_status(db, organization_id=ORG, control_key=KEY).is_canary is False

    def test_canary_is_designated_explicitly(self, db):
        _to_shadow(db)
        status = set_canary(db, organization_id=ORG, control_key=KEY,
                            is_canary=True, expected_version=1)
        assert status.is_canary is True
        assert canary_organizations(db, control_key=KEY) == (ORG,)

    def test_canary_can_be_withdrawn(self, db):
        _to_shadow(db)
        set_canary(db, organization_id=ORG, control_key=KEY,
                   is_canary=True, expected_version=1)
        set_canary(db, organization_id=ORG, control_key=KEY,
                   is_canary=False, expected_version=2)
        assert canary_organizations(db, control_key=KEY) == ()

    def test_designating_a_canary_does_not_change_the_rollout_state(self, db):
        """A canary marker says "this one goes first", not "this one is on"."""
        _to_shadow(db)
        set_canary(db, organization_id=ORG, control_key=KEY,
                   is_canary=True, expected_version=1)
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW
        assert is_enabled(db, organization_id=ORG, control_key=KEY) is False

    def test_a_canary_still_serves_the_full_observation_window(self, db):
        _to_shadow(db)
        set_canary(db, organization_id=ORG, control_key=KEY,
                   is_canary=True, expected_version=1)
        with pytest.raises(ObservationWindowNotElapsed):
            _to_enabled(db, version=2, at=T0 + timedelta(days=2))
        db.rollback()

    def test_an_organization_must_exist_as_a_control_before_being_a_canary(self, db):
        with pytest.raises(UnknownRolloutControl):
            set_canary(db, organization_id=ORG, control_key=KEY,
                       is_canary=True, expected_version=0)
        db.rollback()

    def test_no_canary_selection_rule_exists_in_the_package(self):
        """Membership is designated, never inferred.

        Scanned over identifiers so the docstrings that explain this rule
        cannot fail it.
        """
        banned = ("bucket", "hash", "sample", "percent", "cohort",
                  "random", "weight", "seed")
        for path in _package_files():
            names = {n.lower() for n in _code_identifiers(path)}
            offenders = {n for n in names if any(b in n for b in banned)}
            assert not offenders, f"{path.name} defines/uses {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# Audit
# ═════════════════════════════════════════════════════════════════════════════
class TestAuditability:
    def test_every_transition_writes_one_audit_row(self, db):
        _to_shadow(db)
        _to_enabled(db, version=1)
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=2,
                   now=T0 + timedelta(days=16))
        assert len(_audit_rows(db)) == 3

    def test_the_audit_row_carries_everything_needed_to_reconstruct(self, db):
        _to_shadow(db)
        transition(db, organization_id=ORG, control_key=KEY,
                   target=RolloutState.OFF, expected_version=1,
                   now=T0 + timedelta(days=2), actor_user_id=77,
                   actor_label="admin:77", reason="regression found in shadow")

        row = _audit_rows(db)[-1]
        assert row.organization_id == ORG
        assert row.actor_id == 77
        assert row.entity == "RolloutControl"
        payload = row.payload
        assert payload["control_key"] == KEY
        assert payload["previous_state"] == SHADOW
        assert payload["new_state"] == OFF
        assert payload["reason"] == "regression found in shadow"
        assert payload["rollback"] is True
        assert payload["state_version"] == 2
        assert payload["transitioned_at"].startswith("2026-09-03")

    def test_a_forward_transition_is_not_marked_as_a_rollback(self, db):
        _to_shadow(db)
        assert _audit_rows(db)[-1].payload["rollback"] is False

    def test_the_audit_uses_the_canonical_chain_not_a_second_system(self, db):
        """AuditLog, keyed on the same integer organization_id as the control."""
        from database import models

        _to_shadow(db)
        row = _audit_rows(db)[-1]
        assert isinstance(row, models.AuditLog)
        assert row.organization_id == ORG

    def test_the_audit_payload_carries_no_personal_information(self, db):
        _to_shadow(db, )
        payload = _audit_rows(db)[-1].payload
        for field in ("name", "email", "phone", "actor_name", "actor_email"):
            assert field not in payload

    def test_canary_changes_are_audited(self, db):
        _to_shadow(db)
        set_canary(db, organization_id=ORG, control_key=KEY,
                   is_canary=True, expected_version=1, actor_user_id=5)
        rows = _audit_rows(db, action="RolloutCanaryChanged")
        assert len(rows) == 1
        assert rows[0].payload["is_canary"] is True

    def test_window_changes_are_audited(self, db):
        _to_shadow(db)
        set_observation_days(db, organization_id=ORG, control_key=KEY,
                             days=21, expected_version=1)
        rows = _audit_rows(db, action="RolloutObservationWindowChanged")
        assert len(rows) == 1
        assert (rows[0].payload["previous_days"], rows[0].payload["new_days"]) == (14, 21)

    def test_a_refused_transition_writes_no_audit_row(self, db):
        with pytest.raises(InvalidRolloutTransition):
            transition(db, organization_id=ORG, control_key=KEY,
                       target=RolloutState.ENABLED, expected_version=None, now=T0)
        db.rollback()
        assert _audit_rows(db) == []


# ═════════════════════════════════════════════════════════════════════════════
# Durability
# ═════════════════════════════════════════════════════════════════════════════
class TestDurability:
    def test_state_survives_a_new_session(self, db, engine):
        """The point of the step: no process-local state.

        A second Session is a new unit of work reading the same committed data —
        the closest in-process equivalent of a restart, and enough to prove the
        answer is not held in the first session's identity map.
        """
        _to_shadow(db)
        _to_enabled(db, version=1)
        db.commit()
        try:
            Fresh = sessionmaker(bind=engine)
            with Fresh() as other:
                assert rollout_state(
                    other, organization_id=ORG, control_key=KEY
                ).value == ENABLED
        finally:
            with sessionmaker(bind=engine)() as cleanup:
                cleanup.execute(sa.delete(RolloutControl))
                from database import models
                cleanup.execute(
                    sa.delete(models.AuditLog)
                    .where(models.AuditLog.entity == "RolloutControl")
                )
                cleanup.commit()

    def test_the_module_holds_no_process_level_state(self):
        """A cached rollout state is a rollback that does not take effect."""
        import app.rollout.service as svc

        cached = [
            name for name, value in vars(svc).items()
            if isinstance(value, (dict, list, set))
            and not name.startswith("__")
            and not isinstance(value, frozenset)
        ]
        assert cached == [], f"mutable module-level state: {cached}"

    def test_the_transition_table_cannot_be_widened_at_runtime(self):
        """OFF -> ENABLED must not be addable by one line executed anywhere."""
        from app.rollout import ALLOWED_TRANSITIONS

        with pytest.raises(TypeError):
            ALLOWED_TRANSITIONS[RolloutState.OFF] = frozenset(RolloutState)
        with pytest.raises(AttributeError):
            ALLOWED_TRANSITIONS[RolloutState.OFF].add(RolloutState.ENABLED)

    def test_reads_go_to_the_database_every_time(self, db):
        """No memoisation between calls, so a rollback is seen immediately."""
        _to_shadow(db)
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == SHADOW
        db.execute(
            sa.update(RolloutControl)
            .where(RolloutControl.organization_id == ORG)
            .values(state=OFF)
        )
        db.expire_all()
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# ECO stays off, and stays unreachable
# ═════════════════════════════════════════════════════════════════════════════
class TestEcoUntouched:
    def test_eco_v1_gate_is_still_a_source_constant_set_to_false(self):
        """v1 is disabled in source, checked BEFORE any durable setting.

        That ordering is what makes v1 unreachable from this package: no row in
        any table can turn it on.
        """
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8"
        )
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
        gate = router.index("if not EARLY_CLOCK_OUT_FEATURE_ENABLED:")
        setting = router.index("SETTING_EARLY_CLOCKOUT_ENABLED)")
        assert gate < setting, (
            "the source constant must be checked before the durable setting"
        )

    def test_the_rollout_package_never_references_the_v1_gate(self):
        """Scanned over identifiers, not raw text.

        The package docstring names the v1 constant precisely to record that
        nothing here can reach it; a text scan would fail on the documentation
        of its own rule. What matters is that no code touches it.
        """
        for path in _package_files():
            assert "EARLY_CLOCK_OUT_FEATURE_ENABLED" not in _code_identifiers(path)

    def test_no_eco_calculation_dependency(self):
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if any(t in m for t in (
                    "early_clock_out", "requirement_resolution", "work_ledger",
                    "_worked", "attendance_policy",
                ))
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_activity_or_screenshot_dependency(self):
        banned = ("active_time", "Activity", "screenshot", "keystroke",
                  "app.intelligence", "sklearn", "numpy", "torch")
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path) if any(b in m for b in banned)
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_in_memory_flag_system_is_not_touched(self):
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if "phase5" in m or "commercial.enforcement" in m
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_declared_control_enables_nothing_by_existing(self, db):
        """Declaring the key creates no row and turns nothing on."""
        assert KEY == "early_clock_out_v2"
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF


# ═════════════════════════════════════════════════════════════════════════════
# Storage constraints
# ═════════════════════════════════════════════════════════════════════════════
class TestStorageConstraints:
    def _row(self, db, **kw):
        base = dict(
            organization_id=ORG, control_key=KEY, state=OFF, state_version=1,
            is_canary=False, min_observation_days=14,
            last_transition_at=T0, last_transition_by="TEST",
        )
        base.update(kw)
        row = RolloutControl(**base)
        db.add(row)
        db.flush()
        return row

    def test_version_zero_is_refused(self, db):
        with pytest.raises(IntegrityError):
            self._row(db, state_version=0)
        db.rollback()

    def test_a_negative_observation_window_is_refused(self, db):
        with pytest.raises(IntegrityError):
            self._row(db, min_observation_days=-1)
        db.rollback()

    def test_an_empty_control_key_is_refused(self, db):
        with pytest.raises(IntegrityError):
            self._row(db, control_key="")
        db.rollback()

    def test_an_arbitrary_state_string_is_storable_by_design(self, db):
        """The absence of a CHECK is deliberate — see the model docstring.

        It is what makes the read-path fail-safe reachable, and therefore
        testable, rather than merely asserted.
        """
        self._row(db, state="SOMETHING-ELSE")
        assert rollout_state(db, organization_id=ORG, control_key=KEY).value == OFF
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════
def _migration_tree() -> ast.Module:
    return ast.parse(MIGRATION.read_text(encoding="utf-8"))


def _module_constant(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {MIGRATION.name}")


def _create_table_calls(tree: ast.Module) -> dict:
    tables = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"):
            continue
        tables[node.args[0].value] = [
            arg.args[0].value for arg in node.args[1:]
            if isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "Column"
        ]
    return tables


def _called_attributes(tree: ast.Module) -> set:
    return {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _load_migration_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_step8_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrated_schema():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from database.models import Organization

    module = _load_migration_module()
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    Organization.__table__.create(eng, checkfirst=True)
    with eng.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
    return eng


class TestMigrationSafety:
    def test_revision_and_parent_are_exactly_as_authorised(self):
        tree = _migration_tree()
        assert _module_constant(tree, "revision") == EXPECTED_REVISION
        assert _module_constant(tree, "down_revision") == EXPECTED_PARENT_REVISION

    def test_it_is_a_single_parent_revision_not_a_merge(self):
        assert isinstance(_module_constant(_migration_tree(), "down_revision"), str)

    def test_the_graph_has_exactly_one_head_and_one_root(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert len(sd.get_heads()) == 1, f"the graph forked: {sd.get_heads()}"
        assert len(sd.get_bases()) == 1

    def test_exactly_one_revision_claims_that_parent(self):
        children = []
        for path in (BACKEND / "alembic" / "versions").glob("*.py"):
            try:
                parent = _module_constant(
                    ast.parse(path.read_text(encoding="utf-8")), "down_revision")
            except AssertionError:
                continue
            if parent == EXPECTED_PARENT_REVISION:
                children.append(path.name)
        assert children == [MIGRATION.name], f"multiple children: {children}"

    def test_the_migration_is_additive_only(self):
        forbidden = {"alter_column", "drop_column", "add_column",
                     "rename_table", "drop_constraint", "drop_index"}
        called = _called_attributes(_migration_tree())
        assert not (called & forbidden), sorted(called & forbidden)

    def test_only_the_one_authorised_table_is_created(self):
        assert set(_create_table_calls(_migration_tree())) == {"rollout_control"}

    def test_the_migration_creates_no_rows(self):
        """Seeding would be fabricated rollout history, and OFF needs no row."""
        assert "bulk_insert" not in _called_attributes(_migration_tree())
        for node in _migration_tree().body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    upper = " ".join(inner.value.upper().split())
                    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                        assert verb not in upper, inner.value

    def test_the_migration_opens_no_connection_of_its_own(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv"):
            assert banned not in body


class TestMigrationActuallyRuns:
    def test_the_table_is_created(self, migrated_schema):
        assert "rollout_control" in sa.inspect(migrated_schema).get_table_names()

    def test_executed_columns_match_the_orm_model(self, migrated_schema):
        actual = sa.inspect(migrated_schema).get_columns("rollout_control")
        assert sorted(c["name"] for c in actual) == sorted(
            c.name for c in RolloutControl.__table__.columns
        )

    def test_executed_nullability_matches_the_orm_model(self, migrated_schema):
        actual = {
            c["name"]: c["nullable"]
            for c in sa.inspect(migrated_schema).get_columns("rollout_control")
        }
        assert actual == {
            c.name: c.nullable for c in RolloutControl.__table__.columns
        }

    def test_the_migrated_table_is_empty(self, migrated_schema):
        with migrated_schema.connect() as conn:
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM rollout_control")
            ).scalar() == 0

    def test_the_uniqueness_rule_is_live(self, migrated_schema):
        with Session(migrated_schema) as s:
            for _ in range(2):
                s.add(RolloutControl(
                    organization_id=ORG, control_key=KEY, state=OFF,
                    state_version=1, is_canary=False, min_observation_days=14,
                    last_transition_at=T0, last_transition_by="TEST",
                ))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()

    def test_downgrade_removes_exactly_that_table(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        before = set(sa.inspect(eng).get_table_names())
        with eng.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                module.upgrade()
        after = set(sa.inspect(eng).get_table_names())
        with eng.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                module.downgrade()
        assert after - before == {"rollout_control"}
        assert set(sa.inspect(eng).get_table_names()) == before

    def test_reapplying_the_migration_is_a_no_op(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        for _ in range(2):
            with eng.begin() as c:
                with Operations.context(MigrationContext.configure(c)):
                    module.upgrade()
        assert "rollout_control" in sa.inspect(eng).get_table_names()


class TestPostgresDialectCompilation:
    """SQLite passing does not prove PostgreSQL correctness."""

    @staticmethod
    def _ddl():
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        return str(CreateTable(RolloutControl.__table__).compile(
            dialect=postgresql.dialect()
        ))

    def test_instants_compile_to_timestamp_with_time_zone(self):
        ddl = self._ddl()
        for column in ("shadow_entered_at", "enabled_at", "last_transition_at"):
            assert f"{column} TIMESTAMP WITH TIME ZONE" in ddl, ddl

    def test_the_state_column_is_plain_text_not_an_enum(self):
        """A PostgreSQL enum type would need a migration to add a state."""
        ddl = self._ddl()
        assert "state VARCHAR(16)" in ddl
        assert "CREATE TYPE" not in ddl

    def test_there_is_no_check_constraint_on_state(self):
        """Deliberate: it keeps the read-path fail-safe reachable."""
        ddl = self._ddl()
        assert "ck_rollout_control_version_positive" in ddl
        assert "CHECK (state IN" not in ddl.replace("\n", " ")

    def test_the_organization_reference_restricts(self):
        assert "REFERENCES organizations (id) ON DELETE RESTRICT" in self._ddl()

    def test_the_natural_key_compiles_as_unique(self):
        assert "UNIQUE (organization_id, control_key)" in self._ddl()


# ═════════════════════════════════════════════════════════════════════════════
# Guards
# ═════════════════════════════════════════════════════════════════════════════
def _imported_modules(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _package_files() -> list:
    files = sorted(PACKAGE.glob("*.py"))
    assert files, f"no modules found under {PACKAGE}"
    return files


def _code_identifiers(path: pathlib.Path) -> set:
    """Names the code defines or references — never its prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
    return names


class TestGuards:
    def test_the_guard_reads_the_whole_package(self):
        assert {p.name for p in _package_files()} == {
            "__init__.py", "errors.py", "models.py", "service.py", "states.py",
        }

    def test_the_lifecycle_has_exactly_three_states(self):
        assert sorted(s.value for s in RolloutState) == [ENABLED, OFF, SHADOW]

    def test_off_cannot_reach_enabled_directly(self):
        from app.rollout import ALLOWED_TRANSITIONS

        assert RolloutState.ENABLED not in ALLOWED_TRANSITIONS[RolloutState.OFF]

    def test_every_state_can_reach_off(self):
        """Rollback must be possible from anywhere."""
        from app.rollout import ALLOWED_TRANSITIONS

        for state in RolloutState:
            if state is RolloutState.OFF:
                continue
            assert RolloutState.OFF in ALLOWED_TRANSITIONS[state], state

    def test_only_authorized_readers_wire_this_package(self):
        """ALERT is this control's FIRST consumer, by owner authorization.

        This asserted "nothing wires rollout" until the ALERT increment, whose
        whole point is to gate ECO notification recording on the durable
        control (D8). Keeping the old wording would have meant either a false
        claim or refusing an authorized consumer.

        What actually matters is preserved and made explicit below: a consumer
        may READ the state and may never WRITE it. A reader cannot turn ECO on;
        a writer could.

        ``eco_day_view`` is the SECOND authorized reader, added with the GV-17
        visibility surface. §44.2 makes rollout part of that surface's contract
        rather than an optimisation: shadow mode is "full evaluation, full
        event emission, no notifications and no Worker-facing surfaces. The
        Tenant administrator sees what would have happened." The route must
        therefore consult the durable control to know whether it may answer at
        all — and it reads only, which
        ``test_no_consumer_outside_this_package_writes_rollout_state`` below
        continues to enforce for it exactly as for ALERT.

        ``eco_driver`` is the THIRD, and the most load-bearing: it is the
        production invocation of the calculation chain, so it is where
        §44.2's three-way distinction actually lives. OFF runs nothing at all;
        SHADOW runs the full chain and stamps every artifact SHADOW; ENABLED
        runs it operationally. It reads the control ONCE per pass and passes
        the derived mode down, which is what keeps D8's "one authority" true
        for every stage beneath it -- and it writes no state, which the test
        below continues to enforce for it exactly as for the other two.

        ``enablement_gates`` was the FOURTH and is no longer a reader at all.
        §44.3's sixth gate is "Shadow period completed — two complete workweeks
        (§44.2)", and the durable control is still the only place that fact is
        recorded — but under owner ruling D6 the dependency was INVERTED. This
        package now composes the checker to gate its own promotion, so the
        checker takes the observation as a ``ShadowObservation`` value and
        imports nothing from here. It is deliberately absent from the set
        below: an unused permission is one that stops noticing the day
        something quietly reintroduces the import.
        """
        AUTHORIZED_READERS = {"alert", "eco_day_view", "eco_driver"}
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "rollout" in path.parts or AUTHORIZED_READERS & set(path.parts):
                continue
            if any(m.startswith("app.rollout") for m in _imported_modules(path)):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_no_consumer_outside_this_package_writes_rollout_state(self):
        """The property the guard above exists to protect.

        Reading a control is how a feature stays off; writing one is how it
        gets turned on. No consumer may do the second.
        """
        writers = ("transition", "set_canary", "set_observation_days")
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "rollout" in path.parts:
                continue
            if not any(m.startswith("app.rollout")
                       for m in _imported_modules(path)):
                continue
            names = _code_identifiers(path)
            for writer in writers:
                if writer in names:
                    offenders.append(f"{path.relative_to(BACKEND)}:{writer}")
        assert offenders == [], f"a consumer writes rollout state: {offenders}"

    def test_no_scheduler_or_background_promotion_exists(self):
        banned = ("celery", "apscheduler", "schedule", "cron", "BackgroundTasks")
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path) if any(b in m for b in banned)
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_global_switch_identifier_exists(self):
        banned = ("global_enable", "enable_all", "kill_switch", "master_switch",
                  "enable_everywhere")
        for path in _package_files():
            names = {n.lower() for n in _code_identifiers(path)}
            offenders = {n for n in names if any(b in n for b in banned)}
            assert not offenders, f"{path.name} defines {offenders}"

    def test_no_environment_variable_can_enable_a_control(self):
        """The entitlement ladder gates on an env var; this deliberately does not.

        An environment variable is a deploy-time value that no audit row records
        and no administrator can roll back without a redeploy.
        """
        for path in _package_files():
            body = path.read_text(encoding="utf-8")
            assert "os.environ" not in body, path.name
            assert "getenv" not in body, path.name

# ══════════════════════════════════════════════════════════════════════════════
# §44.3 gates are a PRECONDITION of promotion (owner rulings D6, G: C)
# ══════════════════════════════════════════════════════════════════════════════
class TestEnablementGates:
    """The window elapsing is necessary and NOT sufficient."""

    def test_an_unready_tenant_is_refused_after_the_full_window(self, db):
        """UNREADY_ORG has no evidence of any kind. Waiting does not help it."""
        st = _restart_shadow(db, org=UNREADY_ORG)
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            _to_enabled(db, org=UNREADY_ORG, version=st.state_version)
        # Refused for the gates, NOT for the window.
        assert not isinstance(exc.value, ObservationWindowNotElapsed)
        assert rollout_state(db, organization_id=UNREADY_ORG,
                             control_key=KEY).value == SHADOW

    def test_the_refusal_names_every_failing_gate_not_just_the_first(self, db):
        st = _restart_shadow(db, org=UNREADY_ORG)
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            _to_enabled(db, org=UNREADY_ORG, version=st.state_version)
        failing = {g.gate.value for g in exc.value.gates}
        # UNREADY_ORG is operationally configured, so what it is missing is
        # COMPLIANCE evidence -- all three of them, named together. An
        # operator told about one gate at a time discovers the requirements
        # one refusal at a time.
        assert failing == {"DPIA", "CONSULTATION_ATTESTATION",
                           "WORKER_NOTICE"}, failing

    def test_a_blocked_promotion_writes_nothing(self, db):
        """Not the state, not the version, not an audit row."""
        st = _restart_shadow(db, org=UNREADY_ORG)
        before = len(_audit_rows(db))
        with pytest.raises(EnablementGatesNotSatisfied):
            _to_enabled(db, org=UNREADY_ORG, version=st.state_version)
        after = get_status(db, organization_id=UNREADY_ORG, control_key=KEY)
        assert after.state.value == SHADOW
        assert after.state_version == st.state_version
        assert len(_audit_rows(db)) == before

    def test_the_gates_are_revalidated_at_promotion_not_at_shadow_entry(self, db):
        """Owner ruling G: C.

        A Tenant that was ready on day 1 and had its administrative recipient
        removed on day 13 must not be promoted on day 15 on the strength of a
        check that passed a fortnight earlier.
        """
        from app.recipient_authority import EcoRecipientDesignation

        st = _restart_shadow(db, org=ORG)
        # Ready right now: the promotion below would otherwise succeed.
        removed = db.execute(
            sa.select(EcoRecipientDesignation)
            .where(EcoRecipientDesignation.organization_id == ORG)
        ).scalars().all()
        assert removed, "precondition: ORG has a designated recipient"
        for row in removed:
            db.delete(row)
        db.commit()
        try:
            with pytest.raises(EnablementGatesNotSatisfied) as exc:
                _to_enabled(db, org=ORG, version=st.state_version)
            assert "ADMINISTRATIVE_RECIPIENT" in {
                g.gate.value for g in exc.value.gates}
        finally:
            db.rollback()
            for row in removed:
                db.add(EcoRecipientDesignation(
                    organization_id=row.organization_id,
                    audience=row.audience, user_id=row.user_id))
            db.commit()
            _to_off(db, org=ORG)

    def test_the_window_is_still_checked_first(self, db):
        """A Tenant that is BOTH unready and early is told to wait.

        The two refusals are different problems: gates say "this Tenant is not
        configured for live operation", the window says "not yet". Collapsing
        them would tell an operator to wait when waiting cannot help.
        """
        st = _restart_shadow(db, org=UNREADY_ORG)
        with pytest.raises(ObservationWindowNotElapsed):
            _to_enabled(db, org=UNREADY_ORG, version=st.state_version,
                        at=T0 + timedelta(days=1))

    def test_a_ready_tenant_is_promoted(self, db):
        """The gate refuses; it does not block everything.

        Without this, every test above would pass against a gate that always
        said no.
        """
        st = _restart_shadow(db, org=ORG)
        enabled = _to_enabled(db, org=ORG, version=st.state_version)
        assert enabled.state.value == ENABLED
        _to_off(db, org=ORG)

    def test_rollback_is_never_gated(self, db):
        """Gates guard turning it ON. A rollback must never be delayed by
        evidence, or a Tenant in trouble stays in trouble until its paperwork
        is in order."""
        st = _restart_shadow(db, org=UNREADY_ORG)
        off = transition(db, organization_id=UNREADY_ORG, control_key=KEY,
                         target=RolloutState.OFF,
                         expected_version=st.state_version, now=T0,
                         actor_label="admin")
        assert off.state.value == OFF


# ═════════════════════════════════════════════════════════════════════════════
# OFF → SHADOW binds the five OPERATIONAL gates (owner ruling D6)
# ═════════════════════════════════════════════════════════════════════════════
class TestShadowEntryGates:
    """D6 binds gates to BOTH transitions, with different sets.

    Entering SHADOW requires the five gates that describe CONFIGURATION,
    because a shadow run that computes against an unset workweek or a missing
    fallback policy observes nothing worth observing. It does NOT require the
    three compliance gates: §44.2 shadow changes nothing a Worker experiences,
    and demanding a DPIA before you may even OBSERVE would make the
    observation period unreachable.
    """

    #: The five, written literally rather than imported, so a rename in the
    #: implementation cannot silently agree with itself.
    OPERATIONAL = ("WORKWEEK_TIMEZONE_ATTRIBUTION", "REQUIREMENT_FALLBACK",
                   "NETTING_DECISION", "BREAK_TREATMENT",
                   "ADMINISTRATIVE_RECIPIENT")

    def test_a_bare_tenant_cannot_enter_shadow(self, db):
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            _to_shadow(db, org=BARE_ORG)
        assert {g.gate.value for g in exc.value.gates} == set(self.OPERATIONAL)

    @pytest.mark.parametrize("withheld", OPERATIONAL)
    def test_each_operational_gate_individually_blocks_shadow_entry(
            self, db, withheld):
        """Four of five is not five. Parameterised so a gate that stops being
        enforced fails on its own name rather than hiding behind the others."""
        from tests import _eco_enablement as ready

        org, admin = PARTIAL_ORGS[withheld]
        builders = {
            "WORKWEEK_TIMEZONE_ATTRIBUTION": ready.workweek_evidence,
            "REQUIREMENT_FALLBACK": ready.requirement_evidence,
            "NETTING_DECISION": ready.netting_evidence,
            "BREAK_TREATMENT": ready.break_treatment_evidence,
        }
        for gate, build in builders.items():
            if gate != withheld:
                build(db, organization_id=org)
        if withheld != "ADMINISTRATIVE_RECIPIENT":
            ready.recipient_evidence(db, organization_id=org,
                                     user_id=admin)
        db.commit()

        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            _to_shadow(db, org=org)
        assert {g.gate.value for g in exc.value.gates} == {withheld}

    def test_the_five_operational_gates_are_satisfied_or_nothing(self):
        """These five never return NOT_APPLICABLE, which is why "passes" and
        "SATISFIED" mean the same thing for them.

        Asserted structurally: if a future edit gave one of them a
        NOT_APPLICABLE branch, "any status other than SATISFIED must block"
        would quietly stop being true for shadow entry.
        """
        import app.enablement_gates.service as gate_service

        source = pathlib.Path(gate_service.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        conditional = {"_assess_dpia", "_assess_consultation"}
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name.startswith("_assess_")
                   and n.name not in conditional]:
            statuses = {n.attr for n in ast.walk(fn)
                        if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name)
                        and n.value.id == "GateStatus"}
            assert "NOT_APPLICABLE" not in statuses, fn.name

    def test_shadow_entry_succeeds_without_any_compliance_evidence(self, db):
        """The point of the split. READY_NO_COMPLIANCE_ORG has the five
        operational gates and no DPIA, no attestation and no worker notice —
        and enters SHADOW, because D6 does not bind those here."""
        from app.compliance_evidence import (
            EcoConsultationAttestation, EcoDpiaEvidence,
            EcoWorkerNoticeConfirmation,
        )

        for model in (EcoDpiaEvidence, EcoConsultationAttestation,
                      EcoWorkerNoticeConfirmation):
            assert db.query(model).filter(
                model.organization_id == READY_NO_COMPLIANCE_ORG).count() == 0

        st = _to_shadow(db, org=READY_NO_COMPLIANCE_ORG)
        assert st.state.value == SHADOW
        _to_off(db, org=READY_NO_COMPLIANCE_ORG)

    def test_a_blocked_shadow_entry_writes_nothing(self, db):
        """No row, no version, no audit entry. The refusal happens before
        anything is applied."""
        before_audit = len(_audit_rows(db))
        before = get_status(db, organization_id=BARE_ORG, control_key=KEY)
        assert before.exists is False

        with pytest.raises(EnablementGatesNotSatisfied):
            _to_shadow(db, org=BARE_ORG)

        after = get_status(db, organization_id=BARE_ORG, control_key=KEY)
        assert after.exists is False
        assert after.state.value == OFF
        assert len(_audit_rows(db)) == before_audit

    def test_the_gate_set_is_a_lookup_not_a_side_effect_of_the_window(self):
        """The defect this class exists to prevent: the gate check used to be
        nested inside the observation-window branch, which only runs for
        SHADOW → ENABLED. Entering SHADOW was therefore ungated.

        Asserted structurally — the gate call must NOT be inside the function
        that guards the window.
        """
        import app.rollout.service as rollout_service

        source = pathlib.Path(rollout_service.__file__).read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "transition")
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            calls = {getattr(c.func, "id", None) for c in ast.walk(node)
                     if isinstance(c, ast.Call)}
            if "requires_observation_window" in {
                    getattr(c.func, "id", None) for c in ast.walk(node.test)
                    if isinstance(c, ast.Call)}:
                assert "_refuse_unless_gates_pass" not in calls, (
                    "the gate check is nested inside the window branch again")

    def test_re_shadowing_from_enabled_is_not_gated(self, db):
        """ENABLED → SHADOW reduces live exposure. D6 binds nothing to it."""
        org = REENTRY_ORG
        st = _to_shadow(db, org=org)
        enabled = _to_enabled(db, org=org, version=st.state_version)
        assert enabled.state.value == ENABLED

        # Break an operational gate, then re-shadow anyway.
        from app.recipient_authority import EcoRecipientDesignation
        rows = db.execute(sa.select(EcoRecipientDesignation).where(
            EcoRecipientDesignation.organization_id == org)).scalars().all()
        for row in rows:
            db.delete(row)
        db.commit()
        try:
            back = transition(
                db, organization_id=org, control_key=KEY,
                target=RolloutState.SHADOW,
                expected_version=enabled.state_version,
                now=T0 + timedelta(days=21), actor_label="admin")
            assert back.state.value == SHADOW
        finally:
            db.rollback()
            for row in rows:
                db.add(EcoRecipientDesignation(
                    organization_id=row.organization_id,
                    audience=row.audience, user_id=row.user_id))
            db.commit()
            _to_off(db, org=org)

    def test_nothing_promotes_a_control_on_its_own(self, db):
        """No scheduler, no background job, no implicit target. Every
        transition in this module was asked for by a caller."""
        for package in ("rollout", "enablement_gates", "jurisdiction",
                        "compliance_evidence"):
            for path in (BACKEND / "app" / package).glob("*.py"):
                source = path.read_text(encoding="utf-8")
                for banned in ("apscheduler", "add_job", "celery",
                               "BackgroundTasks"):
                    assert banned not in source, f"{path.name}: {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# The whole lifecycle, once, as one narrative (shadow pre-flight rehearsal)
# ═════════════════════════════════════════════════════════════════════════════
class TestShadowPreflightSimulation:
    """OFF → SHADOW → (observe) → ENABLED → SHADOW → OFF, end to end.

    The classes above test each transition in isolation, which is what makes a
    failure diagnosable. This one walks the sequence an operator will actually
    perform, in order, on a Tenant that starts with nothing — because a set of
    individually-correct steps can still not compose.

    ISOLATED BY CONSTRUCTION
    ------------------------
    Its own in-memory engine and its own session, not the module's shared
    database. Nothing here can touch a real Tenant, and the engine ceases to
    exist when the test returns.
    """

    ORG, ADMIN = 5150, 5151

    @pytest.fixture()
    def sim(self):
        from sqlalchemy.orm import Session as _S

        import database.models as dbm
        from tests import _eco_enablement as ready

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        ready.create_schema(eng)
        with _S(eng, expire_on_commit=False) as s:
            s.add(dbm.Organization(id=self.ORG, name="Preflight Org"))
            s.commit()
            s.add(dbm.User(id=self.ADMIN, name="Admin",
                           email="preflight@rollout.test",
                           organization_id=self.ORG, role="admin"))
            s.commit()
            yield s

    def _shadow(self, db, at, version=None):
        return transition(
            db, organization_id=self.ORG, control_key=KEY,
            target=RolloutState.SHADOW, expected_version=version, now=at,
            actor_label="admin")

    def _enable(self, db, at, version):
        return transition(
            db, organization_id=self.ORG, control_key=KEY,
            target=RolloutState.ENABLED, expected_version=version, now=at,
            actor_label="admin")

    def test_the_operator_walkthrough(self, sim):
        from tests import _eco_enablement as ready

        db = sim

        # ── 1. A Tenant with nothing cannot even begin observing ───────────
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            self._shadow(db, T0)
        assert len(exc.value.gates) == 5
        assert get_status(db, organization_id=self.ORG,
                          control_key=KEY).exists is False

        # ── 2. Four of the five is still not five ──────────────────────────
        ready.workweek_evidence(db, organization_id=self.ORG)
        ready.requirement_evidence(db, organization_id=self.ORG)
        ready.netting_evidence(db, organization_id=self.ORG)
        ready.break_treatment_evidence(db, organization_id=self.ORG)
        db.commit()
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            self._shadow(db, T0)
        assert {g.gate.value for g in exc.value.gates} == \
            {"ADMINISTRATIVE_RECIPIENT"}

        # ── 3. The fifth gate, and observation may begin ───────────────────
        ready.recipient_evidence(db, organization_id=self.ORG,
                                 user_id=self.ADMIN)
        db.commit()
        shadow = self._shadow(db, T0)
        assert shadow.state.value == SHADOW
        assert shadow.shadow_entered_at is not None

        # Entering SHADOW required NO compliance evidence, and there is none.
        from app.compliance_evidence import (
            EcoConsultationAttestation, EcoDpiaEvidence,
            EcoWorkerNoticeConfirmation,
        )
        for model in (EcoDpiaEvidence, EcoConsultationAttestation,
                      EcoWorkerNoticeConfirmation):
            assert db.query(model).count() == 0

        # ── 4. Promotion before two complete workweeks is refused ──────────
        with pytest.raises(ObservationWindowNotElapsed) as window:
            self._enable(db, T0 + timedelta(days=13),
                         shadow.state_version)
        assert window.value.days_remaining > 0

        # ── 5. Window elapsed, but compliance evidence is still absent ─────
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            self._enable(db, T0 + timedelta(days=14), shadow.state_version)
        assert {g.gate.value for g in exc.value.gates} == \
            {"DPIA", "CONSULTATION_ATTESTATION", "WORKER_NOTICE"}

        # ── 6. Jurisdiction first: without it, applicability is unresolved ──
        ready.classification_evidence(db)
        ready.jurisdiction_evidence(db, organization_id=self.ORG,
                                    countries=(ready.GERMANY,))
        db.commit()
        with pytest.raises(EnablementGatesNotSatisfied) as exc:
            self._enable(db, T0 + timedelta(days=14), shadow.state_version)
        # DE is EU and requires consultation, so all three still bind.
        assert {g.gate.value for g in exc.value.gates} == \
            {"DPIA", "CONSULTATION_ATTESTATION", "WORKER_NOTICE"}

        # ── 7. Record the evidence; promotion is allowed ───────────────────
        ready.compliance_evidence(db, organization_id=self.ORG,
                                  admin_user_id=self.ADMIN)
        db.commit()
        enabled = self._enable(db, T0 + timedelta(days=15),
                               shadow.state_version)
        assert enabled.state.value == ENABLED
        assert enabled.enabled_at is not None

        # ── 8. Rollback is never gated, and re-shadowing restarts the clock ─
        back = self._shadow(db, T0 + timedelta(days=16),
                            enabled.state_version)
        assert back.state.value == SHADOW
        assert back.shadow_entered_at.date() == (T0 + timedelta(days=16)).date()
        assert back.enabled_at is None

        # The fresh period must be observed again in full.
        with pytest.raises(ObservationWindowNotElapsed):
            self._enable(db, T0 + timedelta(days=17), back.state_version)

        off = transition(
            db, organization_id=self.ORG, control_key=KEY,
            target=RolloutState.OFF, expected_version=back.state_version,
            now=T0 + timedelta(days=18), actor_label="admin")
        assert off.state.value == OFF
        assert off.shadow_entered_at is None

    def test_the_simulation_touches_no_real_rollout_state(self, sim):
        """The rehearsal must not be capable of enrolling anybody.

        Its engine is created in the fixture above and is reachable from
        nothing else; this asserts the module's shared database — the nearest
        thing to a real one in this suite — is untouched by it.
        """
        assert sim.bind is not None
        assert str(sim.bind.url) == "sqlite:///:memory:"
        assert sim.query(RolloutControl).filter(
            RolloutControl.organization_id == self.ORG).count() == 0
