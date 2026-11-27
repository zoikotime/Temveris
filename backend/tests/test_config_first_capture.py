"""
An organization's FIRST configuration version.
==============================================

The precondition every other configuration path depends on, and the one that
had no implementation. Four things are tested:

1. **The two authorized shapes** — ``required_minutes`` alone, and
   ``required_minutes`` with an ECO threshold — and that the two prohibited
   shapes cannot be expressed at the service boundary at all, rather than being
   refused inside it.

2. **What it refuses to invent.** No live setting, no employment-type policy,
   no schedule default, no risk threshold, no 8-hour fallback, no inferred
   date. Asserted by scanning the package's code, not its prose.

3. **Integrity** — the digest describes the complete stored snapshot,
   independently recomputed here; the actor is outside it; the rows are
   immutable; and what was stored is what was supplied.

4. **First means first** — an organization that already holds a version is
   refused, and the supersession mechanism is not invoked.

Literals are transcribed independently rather than imported: importing the
production constant would let a wrong value agree with itself.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.config_first_capture import (
    InitialCaptureOutcome,
    InitialCaptureRefusal,
    InitialCaptureResult,
    SnapshotShape,
    capture_initial_configuration,
)
from app.config_first_capture.service import (
    AUDIT_ACTION_CAPTURED,
    AUDIT_ACTION_REFUSED,
    AUDIT_ENTITY,
)
from app.config_versioning import ConfigValue, ConfigVersion
from app.eco_threshold import (
    DatabaseThresholdProvider,
    ThresholdOutcome,
    resolve_threshold,
)
from app.eco_threshold_capture import ActorContext, current_open_workweek_start
from app.requirement_resolution import ResolutionOutcome, resolve_requirement_strict

ORG = 11
OTHER_ORG = 12

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "config_first_capture"

# ── Independently transcribed ────────────────────────────────────────────────
REQUIREMENT_KEY = "required_minutes"
REQUIREMENT_TIER = "TENANT_FALLBACK"
REQUIREMENT_RANK = 6

THRESHOLD_KEY = "eco_weekly_attributable_shortfall_threshold_minutes"
THRESHOLD_TIER = "ECO_THRESHOLD"
THRESHOLD_RANK = 0

MIN_MINUTES = 1
MAX_MINUTES = 1440

AUDIT_ENTITY_EXPECTED = "ConfigurationVersion"
AUDIT_CAPTURED_EXPECTED = "InitialConfigurationCaptured"
AUDIT_REFUSED_EXPECTED = "InitialConfigurationCaptureRefused"

#: Runtime fallbacks that must never become captured configuration.
FORBIDDEN_RUNTIME_VALUES = (28800, 31500, 30)

EXPECTED_REVISION_COUNT = 94   # + b5f2a8c47d93 (ECO day record INV-4 break evidence)
#: 73 since the D-18 correction added e7c2b9d4f815, after d5f1a8c3b724, after §45.D-15's c4e9b7d2a583 (clock_session_boundary_event),
#: an ordinary single-parent child of b7c4e0a92d51.


@pytest.fixture(scope="module", autouse=True)
def _first_capture_schema(engine):
    from database.models import AuditLog, Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    AuditLog.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)


# ═════════════════════════════════════════════════════════════════════════════
# Actors and helpers
# ═════════════════════════════════════════════════════════════════════════════
class _User:
    """The attributes ``ActorContext.for_user`` reads. Not a DB row."""

    def __init__(self, id, role, organization_id=ORG, email=None):
        self.id = id
        self.role = role
        self.organization_id = organization_id
        self.email = email


def _admin(**kw):
    return ActorContext.for_user(_User(701, "admin", email="admin@example.test", **kw))


def _hr(**kw):
    return ActorContext.for_user(_User(702, "hr", email="hr@example.test", **kw))


def _open_week(db):
    return current_open_workweek_start(db, organization_id=ORG)


def _capture(db, required=480, effective_from=None, actor=None,
             organization_id=ORG, **kw):
    return capture_initial_configuration(
        db,
        organization_id=organization_id,
        required_minutes=required,
        effective_from=(effective_from if effective_from is not None
                        else _open_week(db)),
        actor=_admin() if actor is None else actor,
        **kw,
    )


def _audits(db, action=None):
    from database.models import AuditLog

    stmt = sa.select(AuditLog).where(AuditLog.entity == AUDIT_ENTITY)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return db.execute(stmt.order_by(AuditLog.id)).scalars().all()


def _versions(db, organization_id=ORG):
    return db.execute(
        sa.select(ConfigVersion)
        .where(ConfigVersion.organization_id == organization_id)
        .order_by(ConfigVersion.effective_from)
    ).scalars().all()


def _values(db, organization_id=ORG):
    rows = db.execute(
        sa.select(ConfigValue)
        .join(ConfigVersion, ConfigValue.config_version_id == ConfigVersion.id)
        .where(ConfigVersion.organization_id == organization_id)
    ).scalars().all()
    return sorted(
        (
            (r.scope_user_id, r.tier, r.rank, r.key, r.value_minutes, r.source_ref)
            for r in rows
        ),
        key=lambda t: (t[0] if t[0] is not None else -1, t[1], t[2], t[3]),
    )


def _independent_hash(*, organization_id, version_key, effective_from, values):
    """The documented digest, transcribed rather than imported.

    Provenance — who captured it and when — appears nowhere in it, which is
    what makes the actor-exclusion test load-bearing.
    """
    canonical = sorted(
        (
            {
                "scope_user_id": v.get("scope_user_id"),
                # R6 (GV-27): the classification dimension, null when the row is
                # classification-independent. Transcribed here too, deliberately
                # — this reimplementation exists to disagree with the production
                # one unless both are changed on purpose.
                "worker_classification": v.get("worker_classification"),
                "tier": v["tier"],
                "rank": int(v["rank"]),
                "key": v["key"],
                "value_minutes": v.get("value_minutes"),
                "source_ref": v.get("source_ref"),
            }
            for v in values
        ),
        key=lambda v: (
            v["scope_user_id"] if v["scope_user_id"] is not None else -1,
            v["worker_classification"] or "",
            v["tier"], v["rank"], v["key"],
        ),
    )
    encoded = json.dumps(
        {
            "organization_id": int(organization_id),
            "version_key": version_key,
            "effective_from": effective_from.isoformat(),
            "values": canonical,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _code_tokens(tree: ast.Module) -> set:
    """Identifiers and runtime strings — docstrings excluded.

    These docstrings explain at length what the package deliberately does NOT
    read, and a text scan cannot tell that from what it does read.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    tokens = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tokens.add(node.name)
        elif isinstance(node, ast.arg):
            tokens.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.add(node.arg)
        elif isinstance(node, ast.alias):
            tokens.add(node.asname or node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                tokens.add(node.value)
    return tokens


# ═════════════════════════════════════════════════════════════════════════════
# 1-2: the two authorized shapes
# ═════════════════════════════════════════════════════════════════════════════
class TestAuthorizedShapes:
    def test_a_requirement_only_first_capture(self, db):
        week = _open_week(db)
        result = _capture(db, 480, week)

        assert result.outcome is InitialCaptureOutcome.CAPTURED
        assert result.is_captured
        assert result.shape is SnapshotShape.REQUIREMENT_ONLY
        assert result.required_minutes == 480
        assert result.threshold_minutes is None
        assert result.effective_from == week
        assert result.config_version_id == week.isoformat()
        assert result.value_count == 1
        assert result.refusal_reason is None

    def test_it_writes_exactly_the_ruled_requirement_row(self, db):
        """Owner ruling 1: TENANT_FALLBACK at rank 6, organization scope."""
        _capture(db, 480)
        assert _values(db) == [
            (None, REQUIREMENT_TIER, REQUIREMENT_RANK, REQUIREMENT_KEY, 480, None),
        ]

    def test_a_requirement_and_threshold_first_capture(self, db):
        week = _open_week(db)
        result = _capture(db, 480, week, threshold_minutes=45)

        assert result.shape is SnapshotShape.REQUIREMENT_AND_THRESHOLD
        assert result.required_minutes == 480
        assert result.threshold_minutes == 45
        assert result.value_count == 2

    def test_it_writes_exactly_the_ruled_threshold_row(self, db):
        """ECO_THRESHOLD at rank 0, organization scope, canonical key."""
        _capture(db, 480, threshold_minutes=45)
        assert _values(db) == [
            (None, THRESHOLD_TIER, THRESHOLD_RANK, THRESHOLD_KEY, 45, None),
            (None, REQUIREMENT_TIER, REQUIREMENT_RANK, REQUIREMENT_KEY, 480, None),
        ]

    def test_exactly_one_version_is_created(self, db):
        _capture(db, 480, threshold_minutes=45)
        versions = _versions(db)
        assert len(versions) == 1
        assert versions[0].version_key == _open_week(db).isoformat()

    def test_source_ref_is_null_on_both_rows(self, db):
        """Provenance for a first capture is the administrative action, which
        lives in ``captured_by_label`` and the audit row — not in a string
        invented here. NULL, not an empty string: the digest tells them apart.
        """
        _capture(db, 480, threshold_minutes=45)
        assert [t[5] for t in _values(db)] == [None, None]

    @pytest.mark.parametrize("minutes", [MIN_MINUTES, 2, 240, 480, 1439, MAX_MINUTES])
    def test_the_whole_declared_requirement_range_is_accepted(self, db, minutes):
        assert _capture(db, minutes).required_minutes == minutes

    @pytest.mark.parametrize("threshold", [MIN_MINUTES, 30, 45, MAX_MINUTES])
    def test_the_whole_declared_threshold_range_is_accepted(self, db, threshold):
        result = _capture(db, 480, threshold_minutes=threshold)
        assert result.threshold_minutes == threshold


# ═════════════════════════════════════════════════════════════════════════════
# 3-4: the prohibited shapes are unexpressible, not merely refused
# ═════════════════════════════════════════════════════════════════════════════
class TestProhibitedShapesAreUnexpressible:
    def test_an_empty_capture_cannot_be_called(self, db):
        """No default for ``required_minutes``, so an empty snapshot is a
        TypeError at the call site — stronger than a refusal inside."""
        with pytest.raises(TypeError):
            capture_initial_configuration(
                db, organization_id=ORG,
                effective_from=_open_week(db), actor=_admin(),
            )
        assert _versions(db) == []

    def test_a_threshold_only_capture_cannot_be_called(self, db):
        with pytest.raises(TypeError):
            capture_initial_configuration(
                db, organization_id=ORG, threshold_minutes=30,
                effective_from=_open_week(db), actor=_admin(),
            )
        assert _versions(db) == []

    def test_required_minutes_has_no_default(self):
        signature = inspect.signature(capture_initial_configuration)
        assert signature.parameters["required_minutes"].default is (
            inspect.Parameter.empty)
        assert signature.parameters["effective_from"].default is (
            inspect.Parameter.empty)
        assert signature.parameters["actor"].default is inspect.Parameter.empty
        # The threshold IS optional, and its absence must be None — never a
        # number, which would seed a threshold nobody chose.
        assert signature.parameters["threshold_minutes"].default is None

    def test_the_service_accepts_no_scope_tier_key_or_rank(self):
        """Owner ruling 3: those are controlled by the service."""
        params = set(inspect.signature(capture_initial_configuration).parameters)
        assert params == {
            "db", "organization_id", "required_minutes", "effective_from",
            "actor", "threshold_minutes",
        }
        for banned in ("user_id", "scope_user_id", "worker_id", "tier", "rank",
                       "key", "version_key"):
            assert banned not in params

    def test_no_refusal_reason_exists_for_the_prohibited_shapes(self):
        """They are structurally impossible, so naming them would imply a
        runtime path that does not exist."""
        tokens = {r.value for r in InitialCaptureRefusal}
        for absent in ("EMPTY_SNAPSHOT", "THRESHOLD_ONLY",
                       "REQUIRED_MINUTES_MISSING"):
            assert absent not in tokens


# ═════════════════════════════════════════════════════════════════════════════
# 5-10: value validation
# ═════════════════════════════════════════════════════════════════════════════
class TestValueValidation:
    @pytest.mark.parametrize("minutes", [True, False])
    def test_a_bool_requirement_is_refused(self, db, minutes):
        """``bool`` is an int subclass; True would otherwise mean 1 minute."""
        result = _capture(db, minutes)
        assert result.refusal_reason is (
            InitialCaptureRefusal.REQUIRED_MINUTES_NOT_AN_INTEGER)
        assert _versions(db) == []

    @pytest.mark.parametrize("minutes", [480.0, "480", None, [480], {"m": 1}])
    def test_a_non_integer_requirement_is_refused(self, db, minutes):
        result = _capture(db, minutes)
        assert result.refusal_reason is (
            InitialCaptureRefusal.REQUIRED_MINUTES_NOT_AN_INTEGER)
        assert _versions(db) == []

    @pytest.mark.parametrize("minutes", [0, -1, -480, MAX_MINUTES + 1, 28800, 31500])
    def test_an_out_of_range_requirement_is_refused(self, db, minutes):
        result = _capture(db, minutes)
        assert result.refusal_reason is (
            InitialCaptureRefusal.REQUIRED_MINUTES_OUT_OF_RANGE)
        assert _versions(db) == []

    @pytest.mark.parametrize("threshold", [True, False])
    def test_a_bool_threshold_is_refused(self, db, threshold):
        result = _capture(db, 480, threshold_minutes=threshold)
        assert result.refusal_reason is (
            InitialCaptureRefusal.THRESHOLD_NOT_AN_INTEGER)
        assert _versions(db) == []

    @pytest.mark.parametrize("threshold", [30.0, "30", [30]])
    def test_a_non_integer_threshold_is_refused(self, db, threshold):
        result = _capture(db, 480, threshold_minutes=threshold)
        assert result.refusal_reason is (
            InitialCaptureRefusal.THRESHOLD_NOT_AN_INTEGER)
        assert _versions(db) == []

    @pytest.mark.parametrize("threshold", [0, -1, MAX_MINUTES + 1, 28800])
    def test_an_out_of_range_threshold_is_refused(self, db, threshold):
        result = _capture(db, 480, threshold_minutes=threshold)
        assert result.refusal_reason is (
            InitialCaptureRefusal.THRESHOLD_OUT_OF_RANGE)
        assert _versions(db) == []

    def test_an_absent_threshold_is_not_an_invalid_one(self, db):
        """``None`` selects shape A. It is not a refusal."""
        result = _capture(db, 480, threshold_minutes=None)
        assert result.is_captured
        assert result.shape is SnapshotShape.REQUIREMENT_ONLY

    def test_an_invalid_threshold_refuses_the_whole_capture(self, db):
        """Not partially applied: the requirement is not written on its own
        when the threshold in the same action is unusable."""
        assert _capture(db, 480, threshold_minutes=0).outcome is (
            InitialCaptureOutcome.REFUSED)
        assert _versions(db) == []
        assert _values(db) == []


# ═════════════════════════════════════════════════════════════════════════════
# 11-14: actor and authority
# ═════════════════════════════════════════════════════════════════════════════
class TestAuthority:
    def test_admin_may_capture(self, db):
        assert _capture(db, 480, actor=_admin()).is_captured

    def test_hr_may_capture(self, db):
        assert _capture(db, 480, actor=_hr()).is_captured

    def test_no_actor_is_refused(self, db):
        result = capture_initial_configuration(
            db, organization_id=ORG, required_minutes=480,
            effective_from=_open_week(db), actor=None,
        )
        assert result.refusal_reason is InitialCaptureRefusal.ACTOR_REQUIRED
        assert _versions(db) == []

    @pytest.mark.parametrize("role", [
        "employee", "manager", "owner", "billing_admin", "viewer", "", None,
    ])
    def test_every_other_role_is_refused(self, db, role):
        result = _capture(db, 480, actor=ActorContext.for_user(
            _User(703, role, email="x@example.test")))
        assert result.refusal_reason is (
            InitialCaptureRefusal.ACTOR_NOT_AUTHORIZED)
        assert _versions(db) == []

    def test_super_admin_is_refused(self, db):
        """Platform staff must not author a tenant's configuration baseline."""
        result = _capture(db, 480, actor=ActorContext.for_user(
            _User(999, "super_admin", email="staff@example.test")))
        assert result.refusal_reason is (
            InitialCaptureRefusal.ACTOR_NOT_AUTHORIZED)
        assert _versions(db) == []

    def test_a_system_actor_is_refused(self, db):
        """STRICTER than ECO threshold capture, deliberately.

        Owner decision 1 requires an explicit administrator-initiated action
        and prohibits creation by any implicit background process — which is
        precisely what a SYSTEM capture is. The threshold path supports SYSTEM
        under ruling E; first capture does not.
        """
        result = _capture(db, 480, actor=ActorContext.system())
        assert result.refusal_reason is (
            InitialCaptureRefusal.ACTOR_NOT_AUTHORIZED)
        assert _versions(db) == []

    def test_an_actor_from_another_organization_is_refused(self, db):
        result = _capture(db, 480, actor=ActorContext.for_user(
            _User(704, "admin", organization_id=OTHER_ORG, email="a@b.test")))
        assert result.refusal_reason is (
            InitialCaptureRefusal.WRONG_ORGANIZATION)
        assert _versions(db) == []

    def test_an_actor_with_no_organization_is_refused(self, db):
        """Stricter than the threshold path, which tolerates a NULL
        organization to admit a SYSTEM actor. This version fixes a tenant's
        historical floor, so its author must belong to that tenant."""
        result = _capture(db, 480, actor=ActorContext.for_user(
            _User(705, "admin", organization_id=None, email="c@d.test")))
        assert result.refusal_reason is (
            InitialCaptureRefusal.WRONG_ORGANIZATION)
        assert _versions(db) == []

    def test_the_authorized_roles_are_exactly_admin_and_hr(self):
        from app.config_first_capture import service

        assert set(service.AUTHORIZED_ROLES) == {"admin", "hr"}

    def test_no_capability_is_consulted(self):
        """A plan entitlement must not stop an organization configuring
        itself. Read from the code, not the prose."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            assert not any("commercial" in m for m in imported), path.name

            tokens = _code_tokens(tree)
            for banned in ("require_capability", "has_capability",
                           "entitlements", "time.attendance.policy"):
                assert [t for t in tokens if banned in t] == [], path.name


# ═════════════════════════════════════════════════════════════════════════════
# 15-17: the effective date
# ═════════════════════════════════════════════════════════════════════════════
class TestEffectiveDate:
    def test_the_open_workweek_start_is_accepted(self, db):
        week = _open_week(db)
        assert _capture(db, 480, week).effective_from == week

    @pytest.mark.parametrize("offset_days", [1, 3, 7, 30, 180])
    def test_later_dates_are_accepted(self, db, offset_days):
        target = _open_week(db) + timedelta(days=offset_days)
        assert _capture(db, 480, target).effective_from == target

    @pytest.mark.parametrize("offset_days", [1, 2, 7, 30, 365])
    def test_earlier_dates_are_refused(self, db, offset_days):
        """Owner decision 2: no automatic historical backdating. A backdated
        first version asserts configuration for weeks nobody captured it for.
        """
        result = _capture(db, 480, _open_week(db) - timedelta(days=offset_days))
        assert result.refusal_reason is (
            InitialCaptureRefusal.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK)
        assert _versions(db) == []

    @pytest.mark.parametrize("bad", [None, "2026-09-07", 20260907, True, 1.5])
    def test_a_missing_or_unusable_date_is_refused(self, db, bad):
        result = capture_initial_configuration(
            db, organization_id=ORG, required_minutes=480,
            effective_from=bad, actor=_admin(),
        )
        assert result.refusal_reason is (
            InitialCaptureRefusal.EFFECTIVE_FROM_REQUIRED)
        assert _versions(db) == []

    def test_a_datetime_narrows_to_its_date(self, db):
        week = _open_week(db)
        result = _capture(
            db, 480, datetime(week.year, week.month, week.day, 14, 30,
                              tzinfo=timezone.utc))
        assert result.effective_from == week

    def test_the_date_is_never_inferred(self):
        """No fallback to today, org creation, first login or attendance."""
        tokens = set()
        for path in sorted(PACKAGE.glob("*.py")):
            tokens |= _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
        for banned in ("utcnow", "today", "created_at", "date_joined",
                       "first_login", "isocalendar"):
            assert banned not in tokens, banned

    def test_the_boundary_is_the_one_canonical_calculation(self):
        """Owner decision 2 / ruling 7: no second workweek calculation.

        It reuses the approved helper rather than calling week_start_date
        itself, which is also why this package needs no schedule-policy
        allowlist entry.
        """
        body = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "current_open_workweek_start" in body
        for banned in ("week_start_date", "isocalendar", "weekday()",
                       "MONDAY", "SUNDAY", "timedelta(days="):
            assert banned not in body, f"service.py uses {banned}"

    def test_the_historical_floor_is_where_the_capture_put_it(self, db):
        """Everything before the first version resolves to no configuration,
        forever — which is why the date is a decision, not a derivation."""
        from app.config_versioning import DatabaseConfigVersionProvider

        week = _open_week(db)
        _capture(db, 480, week)

        assert resolve_requirement_strict(
            organization_id=ORG, user_id=501, as_of=week - timedelta(days=1),
            provider=DatabaseConfigVersionProvider(db),
        ).outcome is ResolutionOutcome.NO_VERSION


# ═════════════════════════════════════════════════════════════════════════════
# 18-21: first means first, and collisions
# ═════════════════════════════════════════════════════════════════════════════
class TestFirstMeansFirst:
    def test_a_second_first_capture_is_refused(self, db):
        week = _open_week(db)
        assert _capture(db, 480, week).is_captured

        result = _capture(db, 420, week + timedelta(days=7))
        assert result.refusal_reason is (
            InitialCaptureRefusal.ORGANIZATION_ALREADY_CONFIGURED)
        assert result.required_minutes is None
        assert len(_versions(db)) == 1

    def test_an_existing_version_from_any_source_refuses_it(self, db):
        """Not only versions this service wrote."""
        from app.config_versioning import DatabaseConfigVersionProvider
        from app.requirement_resolution import RequirementCandidate

        DatabaseConfigVersionProvider(db).capture_version(
            organization_id=ORG, config_version_id="pre-existing",
            effective_from=_open_week(db) - timedelta(days=30),
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=480)],
        )
        result = _capture(db, 420)
        assert result.refusal_reason is (
            InitialCaptureRefusal.ORGANIZATION_ALREADY_CONFIGURED)
        assert len(_versions(db)) == 1

    def test_it_does_not_supersede_or_carry_anything_forward(self, db):
        """The complete-snapshot supersession mechanism belongs to threshold
        capture and must not be invoked here."""
        assert _capture(db, 480).is_captured
        before = _values(db)

        result = _capture(db, 420, _open_week(db) + timedelta(days=7))
        assert result.outcome is InitialCaptureOutcome.REFUSED
        assert _values(db) == before
        assert len(_versions(db)) == 1

        body = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "capture_threshold" not in body
        assert "_carry_forward" not in body

    def test_a_version_key_collision_is_refused(self, db, monkeypatch):
        """Defence in depth, proved to fire.

        Unreachable through the public path — a key can only be taken if a
        version exists, which ``ORGANIZATION_ALREADY_CONFIGURED`` refuses
        first. So the subsuming probe is blinded, which is exactly the state a
        lost race produces, and the guard behind it must still refuse.
        """
        from app.config_first_capture import service as svc

        week = _open_week(db)
        _capture(db, 480, week)
        monkeypatch.setattr(svc, "_any_version", lambda *a, **k: None)

        result = _capture(db, 420, week)
        assert result.refusal_reason is (
            InitialCaptureRefusal.VERSION_KEY_ALREADY_IN_USE)
        assert len(_versions(db)) == 1

    def test_an_effective_date_collision_is_refused(self, db, monkeypatch):
        """The date guard behind both checks above it.

        Reached by blinding the organization probe and the key probe — a
        version whose key differs but whose date collides is what remains.
        """
        from app.config_first_capture import service as svc

        week = _open_week(db)
        _capture(db, 480, week)
        monkeypatch.setattr(svc, "_any_version", lambda *a, **k: None)
        monkeypatch.setattr(svc, "_version_with_key", lambda *a, **k: None)

        result = _capture(db, 420, week)
        assert result.refusal_reason is (
            InitialCaptureRefusal.EFFECTIVE_DATE_ALREADY_CLAIMED)
        assert len(_versions(db)) == 1

    def test_the_three_collision_reasons_are_distinct(self):
        assert len({
            InitialCaptureRefusal.ORGANIZATION_ALREADY_CONFIGURED.value,
            InitialCaptureRefusal.VERSION_KEY_ALREADY_IN_USE.value,
            InitialCaptureRefusal.EFFECTIVE_DATE_ALREADY_CLAIMED.value,
            InitialCaptureRefusal.CONCURRENT_CAPTURE.value,
        }) == 4

    def test_the_unique_constraints_back_the_pre_checks(self, db):
        """What actually protects the invariant if every guard were bypassed."""
        week = _open_week(db)
        _capture(db, 480, week)

        db.add(ConfigVersion(
            organization_id=ORG, version_key="racer", effective_from=week,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64, captured_by_label="SYSTEM",
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_lost_race_is_refused_as_a_concurrent_capture(self, db, monkeypatch):
        """The constraint catches what the pre-check could not see.

        Simulated by blinding every existence probe, which is exactly what a
        race does: the checking transaction sees nothing, the constraint sees
        the row.
        """
        from app.config_first_capture import service as svc

        week = _open_week(db)
        _capture(db, 480, week)
        for probe in ("_any_version", "_version_with_key", "_version_at"):
            monkeypatch.setattr(svc, probe, lambda *a, **k: None)

        result = _capture(db, 420, week)
        assert result.refusal_reason is (
            InitialCaptureRefusal.CONCURRENT_CAPTURE)
        assert result.required_minutes is None
        # The rollback discards the caller's uncommitted work with ours — the
        # trade-off accepted under ruling W5-2 and restated here. In this test
        # the winning capture was never committed, so it goes too. What matters
        # is that the loser wrote nothing and did not reuse the winner's
        # version: 420 exists nowhere.
        assert 420 not in {t[4] for t in _values(db)}


# ═════════════════════════════════════════════════════════════════════════════
# 22-24, 26-27: hash, immutability, persisted verification
# ═════════════════════════════════════════════════════════════════════════════
class TestIntegrity:
    def test_the_hash_matches_an_independent_calculation(self, db):
        week = _open_week(db)
        result = _capture(db, 480, week, threshold_minutes=45)

        assert result.content_hash == _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            values=[
                {"scope_user_id": None, "tier": REQUIREMENT_TIER,
                 "rank": REQUIREMENT_RANK, "key": REQUIREMENT_KEY,
                 "value_minutes": 480, "source_ref": None},
                {"scope_user_id": None, "tier": THRESHOLD_TIER,
                 "rank": THRESHOLD_RANK, "key": THRESHOLD_KEY,
                 "value_minutes": 45, "source_ref": None},
            ],
        )

    def test_the_stored_hash_is_the_returned_hash(self, db):
        result = _capture(db, 480)
        assert _versions(db)[0].content_hash == result.content_hash

    def test_the_hash_covers_the_threshold_when_one_is_supplied(self, db):
        """Shape B must not hash like shape A."""
        week = _open_week(db)
        with_threshold = _capture(db, 480, week, threshold_minutes=45)

        requirement_only = _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            values=[{"scope_user_id": None, "tier": REQUIREMENT_TIER,
                     "rank": REQUIREMENT_RANK, "key": REQUIREMENT_KEY,
                     "value_minutes": 480, "source_ref": None}],
        )
        assert with_threshold.content_hash != requirement_only

    @pytest.mark.parametrize("actor_name", ["admin", "hr"])
    def test_the_actor_is_outside_the_hash(self, db, actor_name):
        """The independent digest takes no actor input at all, so a match for
        both actors settles that changing the actor alone cannot change it."""
        actor = {"admin": _admin(), "hr": _hr()}[actor_name]
        week = _open_week(db)
        result = _capture(db, 480, week, actor=actor)

        assert result.content_hash == _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            values=[{"scope_user_id": None, "tier": REQUIREMENT_TIER,
                     "rank": REQUIREMENT_RANK, "key": REQUIREMENT_KEY,
                     "value_minutes": 480, "source_ref": None}],
        )
        assert _versions(db)[0].captured_by_label == actor.label

    def test_the_actor_label_is_recorded_and_immutable(self, db):
        from app.config_versioning import ImmutableConfigurationError

        _capture(db, 480, actor=_admin())
        version = _versions(db)[0]
        assert version.captured_by_label == "admin@example.test"
        assert version.captured_by_user_id == 701

        version.captured_by_label = "someone@else.test"
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_version_is_immutable(self, db):
        from app.config_versioning import ImmutableConfigurationError

        _capture(db, 480)
        version = _versions(db)[0]
        version.content_hash = "f" * 64
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_values_are_immutable(self, db):
        from app.config_versioning import ImmutableConfigurationError

        _capture(db, 480)
        row = db.execute(
            sa.select(ConfigValue).where(ConfigValue.key == REQUIREMENT_KEY)
        ).scalars().one()
        row.value_minutes = 999
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_version_cannot_be_deleted(self, db):
        from app.config_versioning import ImmutableConfigurationError

        _capture(db, 480)
        db.delete(_versions(db)[0])
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_persisted_snapshot_is_verified_before_the_audit(self, db):
        """Non-vacuity: the verification compares re-read rows, so a write
        that stored something other than what was supplied is caught."""
        body = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert "populate_existing" in body
        assert "SNAPSHOT_VERIFICATION_FAILED" in body

        result = _capture(db, 480)
        assert result.is_captured
        assert len(_audits(db, AUDIT_ACTION_CAPTURED)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 25, 32: isolation and downstream resolution
# ═════════════════════════════════════════════════════════════════════════════
class TestIsolationAndResolution:
    def test_one_organizations_capture_does_not_configure_another(self, db):
        _capture(db, 480, threshold_minutes=45)
        assert _versions(db, OTHER_ORG) == []
        assert _values(db, OTHER_ORG) == []

    def test_each_organization_gets_its_own_first_capture(self, db):
        week = _open_week(db)
        assert _capture(db, 480, week).is_captured
        other = capture_initial_configuration(
            db, organization_id=OTHER_ORG, required_minutes=300,
            effective_from=week,
            actor=ActorContext.for_user(_User(
                710, "admin", organization_id=OTHER_ORG, email="o@e.test")),
        )
        assert other.is_captured
        assert _values(db, OTHER_ORG) == [
            (None, REQUIREMENT_TIER, REQUIREMENT_RANK, REQUIREMENT_KEY, 300, None),
        ]

    def test_the_requirement_resolves_from_the_first_version(self, db):
        from app.config_versioning import DatabaseConfigVersionProvider

        week = _open_week(db)
        _capture(db, 480, week)

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=501, as_of=week,
            provider=DatabaseConfigVersionProvider(db),
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 480
        assert result.source_tier == REQUIREMENT_TIER
        assert result.source_rank == REQUIREMENT_RANK
        assert result.config_version_id == week.isoformat()

    def test_both_domains_resolve_from_a_shape_b_version(self, db):
        """The completeness rule, at the very first version."""
        from app.config_versioning import DatabaseConfigVersionProvider

        week = _open_week(db)
        _capture(db, 480, week, threshold_minutes=45)

        requirement = resolve_requirement_strict(
            organization_id=ORG, user_id=501, as_of=week,
            provider=DatabaseConfigVersionProvider(db),
        )
        assert requirement.outcome is ResolutionOutcome.RESOLVED
        assert requirement.required_minutes == 480
        assert THRESHOLD_TIER not in requirement.chain_evaluated

        threshold = resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week,
            provider=DatabaseThresholdProvider(db),
        )
        assert threshold.outcome is ThresholdOutcome.RESOLVED
        assert threshold.threshold_minutes == 45

    def test_shape_a_leaves_the_threshold_unconfigured_not_defaulted(self, db):
        week = _open_week(db)
        _capture(db, 480, week)

        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week,
            provider=DatabaseThresholdProvider(db),
        ).outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED

    def test_a_threshold_capture_can_then_supersede_the_first_version(self, db):
        """The two paths compose: first capture establishes the baseline, and
        the W5 supersession mechanism carries it forward."""
        from app.eco_threshold_capture import capture_threshold

        week = _open_week(db)
        _capture(db, 480, week)

        later = week + timedelta(days=7)
        captured = capture_threshold(
            db, organization_id=ORG, threshold_minutes=30,
            effective_from=later, actor=_admin(),
        )
        assert captured.is_captured
        assert captured.superseded_config_version_id == week.isoformat()
        assert captured.carried_value_count == 1

        # The carried requirement kept its ruled tier and rank.
        assert (None, REQUIREMENT_TIER, REQUIREMENT_RANK, REQUIREMENT_KEY,
                480, None) in _values(db)


# ═════════════════════════════════════════════════════════════════════════════
# 28-29: audit
# ═════════════════════════════════════════════════════════════════════════════
class TestAudit:
    def test_the_ratified_tokens_are_used(self):
        assert AUDIT_ENTITY == AUDIT_ENTITY_EXPECTED
        assert AUDIT_ACTION_CAPTURED == AUDIT_CAPTURED_EXPECTED
        assert AUDIT_ACTION_REFUSED == AUDIT_REFUSED_EXPECTED

    def test_the_tokens_carry_no_event_vocabulary(self):
        for token in (AUDIT_ENTITY, AUDIT_ACTION_CAPTURED, AUDIT_ACTION_REFUSED):
            for banned in ("REACHED", "WITHDRAWN", "PRIMARY", "CROSS",
                           "SEQUENCE", "NOTIF"):
                assert banned not in token.upper(), token

    def test_the_tokens_are_distinct_from_the_threshold_paths(self):
        from app.eco_threshold_capture import service as threshold_service

        assert AUDIT_ENTITY != threshold_service.AUDIT_ENTITY
        assert AUDIT_ACTION_CAPTURED != threshold_service.AUDIT_ACTION_CAPTURED
        assert AUDIT_ACTION_REFUSED != threshold_service.AUDIT_ACTION_REFUSED

    def test_a_capture_is_audited_with_the_required_fields(self, db):
        week = _open_week(db)
        _capture(db, 480, week, threshold_minutes=45)

        rows = _audits(db, AUDIT_ACTION_CAPTURED)
        assert len(rows) == 1
        assert rows[0].organization_id == ORG
        assert rows[0].actor_id == 701

        payload = rows[0].payload
        for field in ("capture", "outcome", "snapshot_shape", "value_count",
                      "effective_from", "open_workweek_start",
                      "config_version_id", "content_hash", "captured_at_utc",
                      "required_minutes", "required_minutes_tier",
                      "required_minutes_rank", "threshold_supplied",
                      "threshold_minutes", "actor_label", "actor_user_id",
                      "actor_role", "actor_is_system"):
            assert field in payload, field

        assert payload["required_minutes"] == 480
        assert payload["required_minutes_tier"] == REQUIREMENT_TIER
        assert payload["required_minutes_rank"] == REQUIREMENT_RANK
        assert payload["threshold_supplied"] is True
        assert payload["threshold_minutes"] == 45
        assert payload["snapshot_shape"] == "REQUIREMENT_AND_THRESHOLD"
        assert payload["effective_from"] == week.isoformat()
        assert payload["actor_label"] == "admin@example.test"
        assert payload["actor_role"] == "admin"

    def test_a_shape_a_capture_records_that_no_threshold_was_supplied(self, db):
        """"Not supplied" must be distinguishable from a null value."""
        _capture(db, 480)
        payload = _audits(db, AUDIT_ACTION_CAPTURED)[0].payload
        assert payload["threshold_supplied"] is False
        assert payload["threshold_minutes"] is None
        assert payload["snapshot_shape"] == "REQUIREMENT_ONLY"

    @pytest.mark.parametrize("scenario", [
        ("no-actor", None, 480, None),
        ("bad-role", "employee", 480, None),
        ("bad-value", "admin", 0, None),
        ("bad-type", "admin", "480", None),
        ("bad-threshold", "admin", 480, 0),
    ])
    def test_every_refusal_path_is_audited(self, db, scenario):
        name, role, minutes, threshold = scenario
        actor = None if role is None else ActorContext.for_user(
            _User(706, role, email="p@example.test"))
        result = capture_initial_configuration(
            db, organization_id=ORG, required_minutes=minutes,
            effective_from=_open_week(db), actor=actor,
            threshold_minutes=threshold,
        )
        assert result.outcome is InitialCaptureOutcome.REFUSED, name
        rows = _audits(db, AUDIT_ACTION_REFUSED)
        assert len(rows) == 1, name
        assert rows[0].payload["refusal_reason"] == result.refusal_reason.value

    def test_the_refusal_audit_carries_the_required_fields(self, db):
        week = _open_week(db)
        _capture(db, 0, week, threshold_minutes=45)
        payload = _audits(db, AUDIT_ACTION_REFUSED)[0].payload

        for field in ("capture", "outcome", "refusal_reason", "detail",
                      "attempted_required_minutes", "attempted_threshold_minutes",
                      "attempted_effective_from", "actor_label", "actor_user_id",
                      "actor_role", "actor_is_system", "attempted_at_utc"):
            assert field in payload, field
        assert payload["attempted_required_minutes"] == 0
        assert payload["attempted_threshold_minutes"] == 45
        assert payload["attempted_effective_from"] == week.isoformat()

    def test_durable_attribution_is_in_the_payload_not_only_the_fk(self, db):
        """``AuditLog.actor_id`` is SET NULL, so an FK-bound actor vanishes
        from the trail exactly when it matters."""
        _capture(db, 480)
        assert _audits(db, AUDIT_ACTION_CAPTURED)[0].payload["actor_label"] == (
            "admin@example.test")

    def test_the_typed_outcome_not_the_detail_string_classifies(self, db):
        """Detail is prose for an administrator and must not be parsed."""
        result = _capture(db, 0)
        assert result.refusal_reason is (
            InitialCaptureRefusal.REQUIRED_MINUTES_OUT_OF_RANGE)
        assert isinstance(result.detail, str) and result.detail


# ═════════════════════════════════════════════════════════════════════════════
# What it must never read, and what it must never become
# ═════════════════════════════════════════════════════════════════════════════
class TestNoInventedConfiguration:
    def test_no_runtime_fallback_is_read(self):
        """Owner ruling 4: no live settings, no 28800, no risk defaults, no
        schedule defaults, no employment-type inference."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            for banned in ("app.settings", "app.attendance_policy",
                           "app.schedule_policy", "app.risk",
                           "app.intelligence", "app.payroll"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), f"{path.name}: {banned}"

            tokens = _code_tokens(tree)
            for banned in ("app_settings", "AppSettings", "resolve_effective",
                           "resolve_work_schedule", "employment_type",
                           "expected_work_seconds", "work_hours_threshold",
                           "DEFAULT_EXPECTED_WORK_SECONDS"):
                assert banned not in tokens, f"{path.name} reads {banned}"

    def test_no_forbidden_runtime_value_is_assigned(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if (isinstance(value, ast.Constant)
                            and value.value in FORBIDDEN_RUNTIME_VALUES
                            and value.value is not False):
                        pytest.fail(f"{path.name}:{node.lineno} assigns "
                                    f"{value.value!r}")

    def test_no_default_argument_supplies_a_configuration_value(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in list(node.args.defaults) + list(node.args.kw_defaults):
                        if (isinstance(d, ast.Constant)
                                and d.value in FORBIDDEN_RUNTIME_VALUES
                                and d.value is not False):
                            pytest.fail(f"{path.name}:{node.lineno} defaults "
                                        f"to {d.value!r}")

    def test_the_ruled_tier_and_rank_are_exactly_as_authorized(self):
        from app.config_first_capture import REQUIREMENT_RANK as rank
        from app.config_first_capture import REQUIREMENT_TIER as tier

        assert tier == "TENANT_FALLBACK"
        assert rank == 6
        assert rank != 5, "rank 5 was explicitly excluded by owner ruling 1"

    def test_the_requirement_key_is_the_canonical_one(self):
        """Reused from the adapter that owns the column, never re-declared —
        a write under one key and a read under another would resolve to
        nothing."""
        from app.config_versioning.adapter import REQUIREMENT_KEY as canonical

        assert canonical == REQUIREMENT_KEY

        # Imported from the adapter that owns the column, not re-declared.
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        imports_it = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "app.config_versioning.adapter"
            and any(a.name == "REQUIREMENT_KEY" for a in n.names)
            for n in ast.walk(tree)
        )
        assert imports_it, "service.py must import the canonical REQUIREMENT_KEY"

        # And no module in the package assigns the literal to anything.
        for path in sorted(PACKAGE.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if (isinstance(value, ast.Constant)
                            and value.value == REQUIREMENT_KEY):
                        pytest.fail(f"{path.name}:{node.lineno} re-declares the "
                                    f"requirement key")

    def test_no_other_tier_token_is_defined_here(self):
        """This increment maps ONE ladder position and invents no token."""
        tokens = set()
        for path in sorted(PACKAGE.glob("*.py")):
            tokens |= _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
        for other in ("WORKER_OVERRIDE", "SHIFT", "SCHEDULE",
                      "EMPLOYMENT_POLICY", "TENANT_TYPE_DEFAULT"):
            assert other not in tokens, f"defines {other}"


# ═════════════════════════════════════════════════════════════════════════════
# 34: scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_the_package_is_not_wired_to_the_application(self):
        foundation = (
            "config_first_capture", "eco_threshold", "eco_threshold_capture",
            "config_versioning", "requirement_resolution", "work_ledger",
            "leave_intervals", "worker_context", "rollout", "eval_day",
            "eval_week",
            # The §44.3 enablement-gate assessment. §44.3's second gate is
            # "Tenant fallback policy exists at rank 6 (§10)", so the checker
            # needs the rank and tier this package DEFINES. It imports the two
            # constants from ``outcomes.py`` and nothing else -- asserted
            # directly below, because "reads the vocabulary" and "is wired to
            # the capture path" are different claims and only the first is
            # authorized.
            "enablement_gates",
        )
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if any(part in foundation for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                if module and "config_first_capture" in module:
                    offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_gate_checker_reads_only_the_vocabulary(self):
        """The one exemption above, held to exactly what it was granted.

        ``enablement_gates`` may import the rank and tier constants from
        ``outcomes.py``. It may NOT reach the capture service, which would make
        a read-only readiness assessment a consumer of a write path.
        """
        checker = BACKEND / "app" / "enablement_gates"
        modules = set()
        for path in sorted(checker.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module                         and "config_first_capture" in node.module:
                    modules.add(node.module)
        assert modules == {"app.config_first_capture.outcomes"}, modules

    def test_the_wiring_guard_would_notice(self):
        scanned = [
            p for p in (BACKEND / "app").rglob("*.py")
            if "config_first_capture" not in p.parts
        ]
        assert len(scanned) > 100

    def test_no_route_schema_or_scheduler_was_added(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            for banned in ("fastapi", "pydantic", "starlette", "apscheduler"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), f"{path.name} imports {banned}"

            tokens = _code_tokens(tree)
            for banned in ("APIRouter", "BaseModel", "Depends", "add_job"):
                assert banned not in tokens, f"{path.name} uses {banned}"

            allowed = {"classmethod", "staticmethod", "property", "dataclass"}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                    continue
                for decorator in node.decorator_list:
                    name = (getattr(decorator, "id", None)
                            or getattr(decorator, "attr", None)
                            or getattr(getattr(decorator, "func", None), "id", None)
                            or getattr(getattr(decorator, "func", None), "attr", None))
                    assert name in allowed, (
                        f"{path.name}:{node.lineno} decorates {node.name} "
                        f"with {name!r}")

    def test_this_increment_added_no_migration_and_the_graph_stays_linear(self):
        """Owner ruling 8: THIS increment requires no schema change.

        The count and head are re-baselined as later authorized revisions land —
        that is not this guard being relaxed. What it actually protects is the
        shape: ONE base and ONE head, so no increment can fork the graph. The
        current head is GV-27's classification revision, added under owner
        rulings R1/R4/R5 with the schema change explicitly authorized.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == (
            EXPECTED_REVISION_COUNT)

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # Head re-baselined by §45.D-15, then by GV-27's a9d3f6b21c58. Still
        # one base, one head — the property that matters.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_no_generic_step6_writer_change(self):
        """Owner ruling 6: ``capture_version()`` keeps its semantics."""
        adapter = (BACKEND / "app" / "config_versioning" / "adapter.py").read_text(
            encoding="utf-8")
        for banned in ("AUTHORIZED_ROLES", "current_open_workweek_start",
                       "AuditLog", "InitialCapture", "carry_forward"):
            assert banned not in adapter, f"adapter.py gained {banned}"

    def test_no_event_or_notification_vocabulary(self):
        banned_identifiers = (
            "sequence", "crossed", "crossing", "withdraw", "withdrawn",
            "withdrawal", "notification", "notify", "weekly_total_minutes",
        )
        for path in sorted(PACKAGE.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for term in banned_identifiers:
                offenders = [t for t in tokens
                             if t == term or t.startswith(term + "_")]
                assert offenders == [], f"{path.name} uses {offenders}"

    def test_no_rollout_or_eco_v1_change(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("rollout", "EARLY_CLOCK_OUT_FEATURE_ENABLED"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_the_golden_vectors_are_untouched(self):
        fixture = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert data["_vector_count"] == 28
        # Owner-ruled split: exactly these eighteen execute, the other 10 do
        # not. GV-04, GV-05 and GV-08 joined when EVAL-EVENT was implemented;
        # GV-06 and GV-22 joined when ALERT's record/render layer was; GV-17
        # joined when VISIBILITY-SURFACE and VIEW-AUTHORITY closed; GV-27 when
        # WORKER-CONTEXT-WIRING did; GV-25 when POLICY-VERSION was consumed.
        # GV-26 joined on §24.15's platform incident (owner ruling D-9.1):
        # STATE-CONTRACT was satisfied by the existing D-8.1 contract and
        # ALERT was a STALE citation, an incident being operational
        # evidence rather than a notification. The set is now COMPLETE,
        # which is asserted as an exact set so a vector leaving it fails.
        executable = {"GV-01", "GV-02", "GV-03", "GV-04", "GV-05", "GV-06",
                      "GV-07", "GV-08", "GV-09", "GV-10", "GV-11", "GV-12", "GV-13",
                      "GV-14", "GV-15", "GV-16", "GV-17", "GV-18", "GV-19", "GV-20",
                      "GV-21", "GV-22", "GV-23", "GV-24", "GV-25", "GV-26", "GV-27",
                      "GV-28"}
        for vector in data["vectors"]:
            expected = ("executable" if vector["id"] in executable
                        else "blocked_pending_platform_primitive")
            assert vector["harness"]["execution"] == expected, vector["id"]
        assert sum(1 for v in data["vectors"]
                   if v["harness"]["execution"] == "executable") == 28

    def test_no_production_database_is_reachable_from_here(self):
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                           "sessionmaker", "load_dotenv", "psycopg"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_the_terminology_guard_is_satisfied(self):
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        for token in (AUDIT_ENTITY, AUDIT_ACTION_CAPTURED, AUDIT_ACTION_REFUSED):
            assert find_prohibited_terms(token) == [], token
        for path in sorted(PACKAGE.glob("*.py")):
            hits = scan_text(path.read_text(encoding="utf-8"))
            assert hits == [], format_hits(hits, source=path.name)


# ═════════════════════════════════════════════════════════════════════════════
# The result type
# ═════════════════════════════════════════════════════════════════════════════
class TestResultType:
    def test_a_refusal_cannot_carry_a_captured_value(self):
        for extra in ({"required_minutes": 480}, {"threshold_minutes": 30},
                      {"config_version_id": "x"}, {"content_hash": "y"},
                      {"value_count": 1},
                      {"shape": SnapshotShape.REQUIREMENT_ONLY}):
            with pytest.raises(ValueError):
                InitialCaptureResult(
                    outcome=InitialCaptureOutcome.REFUSED, organization_id=ORG,
                    refusal_reason=InitialCaptureRefusal.ACTOR_REQUIRED,
                    **extra,
                )

    def test_a_refusal_must_state_a_reason(self):
        with pytest.raises(ValueError):
            InitialCaptureResult(
                outcome=InitialCaptureOutcome.REFUSED, organization_id=ORG)

    def test_a_capture_must_be_complete(self):
        base = dict(
            outcome=InitialCaptureOutcome.CAPTURED, organization_id=ORG,
            required_minutes=480, effective_from=date(2026, 9, 7),
            config_version_id="2026-09-07",
        )
        with pytest.raises(ValueError):
            InitialCaptureResult(**base)                       # no shape/hash
        assert InitialCaptureResult(
            **base, shape=SnapshotShape.REQUIREMENT_ONLY,
            content_hash="h", value_count=1,
        ).is_captured

    def test_the_shape_must_agree_with_the_threshold(self):
        base = dict(
            outcome=InitialCaptureOutcome.CAPTURED, organization_id=ORG,
            required_minutes=480, effective_from=date(2026, 9, 7),
            config_version_id="2026-09-07", content_hash="h", value_count=1,
        )
        with pytest.raises(ValueError):
            InitialCaptureResult(
                **base, shape=SnapshotShape.REQUIREMENT_AND_THRESHOLD)
        with pytest.raises(ValueError):
            InitialCaptureResult(
                **base, shape=SnapshotShape.REQUIREMENT_ONLY,
                threshold_minutes=30)

    def test_the_result_is_frozen(self):
        result = InitialCaptureResult(
            outcome=InitialCaptureOutcome.REFUSED, organization_id=ORG,
            refusal_reason=InitialCaptureRefusal.ACTOR_REQUIRED,
        )
        with pytest.raises(Exception):
            result.refusal_reason = None        # type: ignore[misc]
