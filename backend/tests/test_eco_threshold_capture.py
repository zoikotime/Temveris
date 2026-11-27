"""
W5 — ECO threshold capture (write path).
========================================

Five things are tested here:

1. **The rulings**, one test per closed decision A–I: no default, explicit
   per-organization capture, admin/hr authority, role-not-capability, a
   mandatory actor, durable attribution, an audit record for every attempt,
   the effective-date boundary, and completed-workweek immutability.

2. **The complete snapshot** — the load-bearing one. ``version_as_of`` selects
   ONE configuration version and every domain resolves from that one, so a
   capture creates a new version carrying the whole snapshot in force forward
   and adds the threshold to it. A threshold-only version would starve
   requirement resolution from its effective date onward. Proved in both
   directions: what the new version holds, and what both readers then resolve.

3. **The version-attachment boundary.** A threshold row must not reach a
   configuration version whose ``effective_from`` predates the current open
   workweek, whether that version is created here or already exists. Proved
   against the stored rows, not only against the return value.

4. **Structural impossibility** where validation would be weaker: no
   per-Worker parameter, no tier parameter, no key parameter, no rank
   parameter, and no write method on the reader.

5. **Scope control** — nothing wired, one additive revision, no event or
   notification vocabulary, no golden vector claimed.

Literals are transcribed independently, as in the reader suite: importing the
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

from app.config_versioning import ConfigValue, ConfigVersion
from app.eco_threshold import (
    DatabaseThresholdProvider,
    ThresholdOutcome,
    resolve_threshold,
)
from app.eco_threshold_capture import (
    ActorContext,
    CaptureOutcome,
    RefusalReason,
    ThresholdCaptureResult,
    capture_threshold,
    current_open_workweek_start,
)
from app.eco_threshold_capture.service import (
    AUDIT_ACTION_CAPTURED,
    AUDIT_ACTION_REFUSED,
    AUDIT_ENTITY,
)
from app.requirement_resolution import (
    RequirementCandidate,
    ResolutionOutcome,
    resolve_requirement_strict,
)
from app.work_ledger import WorkDayLedger, WorkStateInterval

ORG = 11
OTHER_ORG = 12

BACKEND = pathlib.Path(__file__).resolve().parents[1]
READER_PACKAGE = BACKEND / "app" / "eco_threshold"
CAPTURE_PACKAGE = BACKEND / "app" / "eco_threshold_capture"

# ── Independently transcribed ────────────────────────────────────────────────
CANONICAL_KEY = "eco_weekly_attributable_shortfall_threshold_minutes"
CANONICAL_TIER = "ECO_THRESHOLD"
CANONICAL_RANK = 0
MIN_MINUTES = 1
MAX_MINUTES = 1440

REVISION = "e5f8c2a91d64"
ACTOR_LABEL_COLUMN = "captured_by_label"
EXPECTED_REVISION_COUNT = 94   # + b5f2a8c47d93 (ECO day record INV-4 break evidence)
#: 73 since the D-18 correction added e7c2b9d4f815, after d5f1a8c3b724, after §45.D-15's c4e9b7d2a583 (clock_session_boundary_event),
#: an ordinary single-parent child of b7c4e0a92d51.

FORBIDDEN_DEFAULTS = (30, 0, 28800, 31500)

# ── The snapshot every capture supersedes ────────────────────────────────────
# A capture writes a COMPLETE configuration version: it carries the snapshot in
# force forward and adds the threshold, because ``version_as_of`` selects one
# version and every domain resolves from that one. So an organization must
# already be configured before it can configure a threshold, and these tests
# arrange that explicitly rather than letting the service invent it.
#
# ``TestSnapshotPrerequisite`` proves the requirement is real by capturing for
# an organization that has none.
REQUIREMENT_KEY = "required_minutes"
BASELINE_ROWS = (("SHIFT", 2, 480), ("SCHEDULE", 3, 420))
BASELINE_OFFSET_DAYS = 91          # a quarter back: clear of every other date here


@pytest.fixture(scope="module", autouse=True)
def _capture_schema(engine):
    from database.models import AuditLog, Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    AuditLog.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)


# ═════════════════════════════════════════════════════════════════════════════
# Actors and helpers
# ═════════════════════════════════════════════════════════════════════════════
class _User:
    """The attributes ``ActorContext.for_user`` reads. Not a DB row: the service
    takes an actor context, never a session-bound user."""

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


def _audits(db, action=None):
    from database.models import AuditLog

    stmt = sa.select(AuditLog).where(AuditLog.entity == AUDIT_ENTITY)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return db.execute(stmt.order_by(AuditLog.id)).scalars().all()


def _threshold_rows(db, organization_id=ORG):
    return db.execute(
        sa.select(ConfigValue)
        .join(ConfigVersion, ConfigValue.config_version_id == ConfigVersion.id)
        .where(
            ConfigVersion.organization_id == organization_id,
            ConfigValue.key == CANONICAL_KEY,
        )
    ).scalars().all()


def _capture(db, minutes, effective_from=None, actor=None, organization_id=ORG):
    return capture_threshold(
        db,
        organization_id=organization_id,
        threshold_minutes=minutes,
        effective_from=effective_from if effective_from is not None else _open_week(db),
        actor=_admin() if actor is None else actor,
    )


def _snapshot(db, *, organization_id=ORG, effective_from, rows=BASELINE_ROWS,
              key=REQUIREMENT_KEY, version_key=None, label="SYSTEM"):
    """Write a configuration version directly, as another domain's capture would.

    Built through the models rather than through ``capture_version()`` so the
    arrangement is visible: these are the exact rows a later ECO capture must
    carry forward untouched. The digest is computed properly, not stubbed, so
    "the superseded version is unchanged" is a claim about a real hash.
    """
    from app.config_versioning import compute_content_hash

    vk = version_key or effective_from.isoformat()
    values = [
        {"scope_user_id": None, "tier": tier, "rank": rank, "key": key,
         "value_minutes": minutes, "source_ref": f"{key}:{tier.lower()}"}
        for tier, rank, minutes in rows
    ]
    version = ConfigVersion(
        organization_id=organization_id,
        version_key=vk,
        effective_from=effective_from,
        captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        captured_by_label=label,
        content_hash=compute_content_hash(
            organization_id=organization_id, version_key=vk,
            effective_from=effective_from, values=values,
        ),
    )
    db.add(version)
    db.flush()
    for value in values:
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=organization_id,
            **value,
        ))
    db.flush()
    return version


@pytest.fixture(autouse=True)
def baseline(db):
    """The organization is already configured before any threshold is captured.

    Autouse because it is a precondition of the capture path, not a detail of
    one test: a capture supersedes a snapshot, and there is nothing to supersede
    on an empty organization. ``OTHER_ORG`` is deliberately left unconfigured so
    the organization-isolation and no-snapshot tests have a clean subject.
    """
    return _snapshot(
        db, effective_from=_open_week(db) - timedelta(days=BASELINE_OFFSET_DAYS),
    )


def _versions(db, organization_id=ORG):
    return db.execute(
        sa.select(ConfigVersion)
        .where(ConfigVersion.organization_id == organization_id)
        .order_by(ConfigVersion.effective_from)
    ).scalars().all()


def _version_by_key(db, version_key, organization_id=ORG):
    return db.execute(
        sa.select(ConfigVersion).where(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.version_key == version_key,
        )
    ).scalars().one()


def _values_in(db, version):
    """Every value frozen into one version, as comparable tuples.

    Sorted with a sentinel for the nullable scope column: ``None`` is not
    comparable with ``int``, so an organization-wide row beside a
    Worker-scoped one cannot be sorted naively.
    """
    rows = db.execute(
        sa.select(ConfigValue).where(
            ConfigValue.config_version_id == version.id)
    ).scalars().all()
    return sorted(
        (
            (r.scope_user_id, r.tier, r.rank, r.key, r.value_minutes, r.source_ref)
            for r in rows
        ),
        key=lambda t: (t[0] if t[0] is not None else -1, t[1], t[2], t[3]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# A: valid capture, and the canonical row it writes
# ═════════════════════════════════════════════════════════════════════════════
class TestValidCapture:
    def test_a_valid_capture_is_recorded(self, db):
        week = _open_week(db)
        result = _capture(db, 30, week)

        assert result.outcome is CaptureOutcome.CAPTURED
        assert result.is_captured
        assert result.threshold_minutes == 30
        assert result.effective_from == week
        assert result.refusal_reason is None

    def test_it_writes_exactly_the_canonical_row(self, db):
        _capture(db, 30)
        rows = _threshold_rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.key == CANONICAL_KEY
        assert row.tier == CANONICAL_TIER
        assert row.rank == CANONICAL_RANK
        assert row.scope_user_id is None
        assert row.value_minutes == 30

    def test_the_reader_then_resolves_it(self, db):
        """The write path and the read path must agree — the whole point."""
        week = _open_week(db)
        _capture(db, 44, week)

        result = resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week,
            provider=DatabaseThresholdProvider(db),
        )
        assert result.outcome is ThresholdOutcome.RESOLVED
        assert result.threshold_minutes == 44

    def test_the_version_content_hash_describes_what_was_written(self, db):
        """INV-8: a captured version must verify against its own values.

        Its COMPLETE values — the carried snapshot as well as the threshold.
        A digest over the threshold alone would describe a version that does
        not exist.
        """
        from app.config_versioning import compute_content_hash

        week = _open_week(db)
        result = _capture(db, 30, week)
        version = _version_by_key(db, result.config_version_id)

        expected_values = [
            {"scope_user_id": None, "tier": tier, "rank": rank,
             "key": REQUIREMENT_KEY, "value_minutes": minutes,
             "source_ref": f"{REQUIREMENT_KEY}:{tier.lower()}"}
            for tier, rank, minutes in BASELINE_ROWS
        ] + [{
            "scope_user_id": None, "tier": CANONICAL_TIER,
            "rank": CANONICAL_RANK, "key": CANONICAL_KEY,
            "value_minutes": 30, "source_ref": None,
        }]

        assert version.content_hash == compute_content_hash(
            organization_id=ORG,
            version_key=result.config_version_id,
            effective_from=week,
            values=expected_values,
        )

    def test_the_result_names_the_snapshot_it_superseded(self, db):
        baseline_date = _open_week(db) - timedelta(days=BASELINE_OFFSET_DAYS)
        result = _capture(db, 30)
        assert result.superseded_config_version_id == baseline_date.isoformat()
        assert result.carried_value_count == len(BASELINE_ROWS)

    def test_the_version_key_is_derived_from_the_effective_date(self, db):
        """Derived, not invented: (organization, effective_from) is already
        unique, so a date-derived key needs no generator or registry."""
        week = _open_week(db)
        assert _capture(db, 30, week).config_version_id == week.isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# B-E: authority (rulings C, D) and the actor (ruling E)
# ═════════════════════════════════════════════════════════════════════════════
class TestAuthority:
    def test_admin_may_capture(self, db):
        assert _capture(db, 30, actor=_admin()).is_captured

    def test_hr_may_capture(self, db):
        assert _capture(db, 30, actor=_hr()).is_captured

    @pytest.mark.parametrize("role", [
        "employee", "manager", "owner", "billing_admin", "viewer", "", None,
    ])
    def test_every_other_role_is_refused(self, db, role):
        result = _capture(db, 30, actor=ActorContext.for_user(
            _User(703, role, email="x@example.test")))
        assert result.outcome is CaptureOutcome.REFUSED
        assert result.refusal_reason is RefusalReason.ACTOR_NOT_AUTHORIZED
        assert result.threshold_minutes is None
        assert _threshold_rows(db) == []

    def test_super_admin_is_refused(self, db):
        """Ruling C: platform staff must not record a tenant's own policy."""
        result = _capture(db, 30, actor=ActorContext.for_user(
            _User(999, "super_admin", email="staff@example.test")))
        assert result.refusal_reason is RefusalReason.ACTOR_NOT_AUTHORIZED
        assert _threshold_rows(db) == []

    def test_an_actor_from_another_organization_is_refused(self, db):
        result = _capture(db, 30, actor=ActorContext.for_user(
            _User(704, "admin", organization_id=OTHER_ORG, email="a@b.test")))
        assert result.refusal_reason is RefusalReason.WRONG_ORGANIZATION
        assert _threshold_rows(db) == []

    def test_no_actor_at_all_is_refused(self, db):
        result = capture_threshold(
            db, organization_id=ORG, threshold_minutes=30,
            effective_from=_open_week(db), actor=None,
        )
        assert result.refusal_reason is RefusalReason.ACTOR_REQUIRED
        assert _threshold_rows(db) == []

    def test_an_explicit_system_actor_is_supported(self, db):
        """Ruling E's second permitted actor, and it needs no role."""
        result = _capture(db, 30, actor=ActorContext.system())
        assert result.is_captured

        version = _version_by_key(db, result.config_version_id)
        assert version.captured_by_label == "SYSTEM"
        assert version.captured_by_user_id is None

    def test_an_actorless_context_cannot_be_constructed(self):
        """Ruling E is held by the type, not only by the service."""
        with pytest.raises(ValueError):
            ActorContext(label="")
        with pytest.raises(ValueError):
            ActorContext(label="   ")
        with pytest.raises(ValueError):
            ActorContext(label="someone", user_id=None)  # human without an id

    def test_a_system_context_carries_no_user_id(self):
        with pytest.raises(ValueError):
            ActorContext(label="SYSTEM", user_id=5, is_system=True)

    def test_no_capability_is_consulted(self):
        """Ruling D: a plan entitlement must not be able to stop an
        organization recording its own configuration decision.

        Read from the CODE, not the file text — these docstrings explain at
        length why no capability is consulted, and a text scan would fail on
        the explanation.
        """
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            assert not any("commercial" in m for m in imported), path.name

            tokens = _code_tokens(tree)
            for banned in ("require_capability", "time.attendance.policy",
                           "entitlements", "has_capability"):
                offenders = [t for t in tokens if banned in t]
                assert offenders == [], f"{path.name} uses {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# F-M: the value, and what the signature refuses to express
# ═════════════════════════════════════════════════════════════════════════════
class TestValidation:
    def test_the_service_accepts_no_worker_scope(self):
        """Ruling 5: organization scope held by the signature."""
        params = set(inspect.signature(capture_threshold).parameters)
        assert params == {
            "db", "organization_id", "threshold_minutes", "effective_from", "actor",
        }
        for banned in ("user_id", "scope_user_id", "worker_id", "tier", "key", "rank"):
            assert banned not in params

    @pytest.mark.parametrize("minutes", [MIN_MINUTES, 2, 30, 44, 1439, MAX_MINUTES])
    def test_the_whole_declared_range_is_accepted(self, db, minutes):
        result = _capture(db, minutes)
        assert result.is_captured
        assert result.threshold_minutes == minutes

    @pytest.mark.parametrize("minutes", [0, -1, MAX_MINUTES + 1, 28800, 31500])
    def test_out_of_range_values_are_refused(self, db, minutes):
        result = _capture(db, minutes)
        assert result.refusal_reason is RefusalReason.VALUE_OUT_OF_RANGE
        assert _threshold_rows(db) == []

    @pytest.mark.parametrize("minutes", [30.0, "30", None, True, False])
    def test_non_integer_values_are_refused(self, db, minutes):
        """A float is refused rather than rounded: rounding would invent a
        value nobody chose. ``bool`` is an int subclass and must not slip in."""
        result = _capture(db, minutes)
        assert result.refusal_reason is RefusalReason.VALUE_NOT_AN_INTEGER
        assert _threshold_rows(db) == []

    def test_no_default_or_seed_exists_anywhere(self, db):
        """Ruling A. Nothing is written until somebody captures something.

        The organization IS configured — it has a requirement snapshot — and
        still has no threshold. Fail-closed, and specifically
        ``THRESHOLD_NOT_CONFIGURED``: nobody chose one, as distinct from
        nothing having been captured at all.
        """
        assert _threshold_rows(db) == []
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=_open_week(db),
            provider=DatabaseThresholdProvider(db),
        ).outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED

    def test_an_organization_without_a_capture_stays_unresolved(self, db):
        """Ruling B: no implicit global threshold reaches an organization."""
        _capture(db, 30)                          # ORG captures
        assert resolve_threshold(
            organization_id=OTHER_ORG, as_of_workweek_start=_open_week(db),
            provider=DatabaseThresholdProvider(db),
        ).outcome is ThresholdOutcome.NO_CONFIG_VERSION

    def test_no_forbidden_numeric_default_is_assigned(self):
        exempt = {"ECO_THRESHOLD_RANK", "CANONICAL_RANK"}
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target])
                    if {t.id for t in targets if isinstance(t, ast.Name)} & exempt:
                        continue
                    value = node.value
                    if (isinstance(value, ast.Constant)
                            and value.value in FORBIDDEN_DEFAULTS
                            and value.value is not False):
                        pytest.fail(f"{path.name}:{node.lineno} assigns "
                                    f"{value.value!r}")

    def test_no_default_argument_supplies_a_threshold(self):
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in list(node.args.defaults) + list(node.args.kw_defaults):
                        if (isinstance(d, ast.Constant)
                                and d.value in FORBIDDEN_DEFAULTS
                                and d.value is not False):
                            pytest.fail(f"{path.name}:{node.lineno} defaults to "
                                        f"{d.value!r}")


# ═════════════════════════════════════════════════════════════════════════════
# P-T: ruling H, and the load-bearing version-attachment boundary
# ═════════════════════════════════════════════════════════════════════════════
class TestEffectiveDateBoundary:
    def test_the_current_open_workweek_start_is_accepted(self, db):
        assert _capture(db, 30, _open_week(db)).is_captured

    @pytest.mark.parametrize("offset_days", [1, 3, 7, 14, 90])
    def test_dates_after_the_open_workweek_start_are_accepted(self, db, offset_days):
        result = _capture(db, 30, _open_week(db) + timedelta(days=offset_days))
        assert result.is_captured

    @pytest.mark.parametrize("offset_days", [1, 2, 7, 30, 365])
    def test_dates_before_the_open_workweek_start_are_refused(self, db, offset_days):
        """Ruling H. A completed workweek must not change what it resolved to."""
        result = _capture(db, 30, _open_week(db) - timedelta(days=offset_days))
        assert result.refusal_reason is (
            RefusalReason.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK)
        assert result.threshold_minutes is None
        assert _threshold_rows(db) == []

    def test_a_historical_config_version_cannot_receive_a_threshold(self, db):
        """The INSERT loophole, closed.

        A version effective before the open workweek exists and carries
        unrelated configuration. Appending a threshold row to it would give
        every workweek from its effective date onward a threshold that was
        never in force — reaching into completed weeks with no backdated
        ``effective_from`` anywhere in the request.
        """
        historical = _open_week(db) - timedelta(days=30)
        version = ConfigVersion(
            organization_id=ORG, version_key=historical.isoformat(),
            effective_from=historical,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64, captured_by_label="SYSTEM",
        )
        db.add(version)
        db.flush()
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG, scope_user_id=None,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=480,
        ))
        db.flush()

        result = _capture(db, 30, historical)

        # Refused on the requested date first — the outer guard.
        assert result.refusal_reason is (
            RefusalReason.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK)
        # And nothing reached the historical version.
        assert _threshold_rows(db) == []
        remaining = db.execute(
            sa.select(ConfigValue.key).where(
                ConfigValue.config_version_id == version.id)
        ).scalars().all()
        assert remaining == ["required_minutes"]

    def test_the_version_boundary_is_enforced_independently(self, db):
        """Not only the requested date — the version actually receiving the row.

        Constructed so the outer date guard passes and only the version-level
        guard can refuse: the requested date is eligible, but a version already
        exists at that date whose own effective date is checked again.
        """
        week = _open_week(db)
        version = ConfigVersion(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64, captured_by_label="SYSTEM",
        )
        db.add(version)
        db.flush()

        result = _capture(db, 30, week)
        # The version is eligible by date, so the refusal is the structural one
        # (see TestExistingVersionLimit), never a silent append.
        assert result.outcome is CaptureOutcome.REFUSED
        assert result.refusal_reason is not (
            RefusalReason.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK)
        assert _threshold_rows(db) == []

    def test_completed_workweeks_keep_their_answer_after_a_later_capture(self, db):
        """Ruling I, proved through the reader.

        A completed week resolves against the snapshot that governed it, which
        held no threshold, and it must keep resolving that way; a capture
        effective this week cannot reach back and give it one.
        """
        week = _open_week(db)
        completed = week - timedelta(days=7)
        _capture(db, 30, week)

        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=completed,
            provider=DatabaseThresholdProvider(db),
        ).outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week,
            provider=DatabaseThresholdProvider(db),
        ).threshold_minutes == 30

    def test_a_future_capture_does_not_change_the_open_week(self, db):
        week = _open_week(db)
        _capture(db, 30, week)
        _capture(db, 44, week + timedelta(days=7))

        provider = DatabaseThresholdProvider(db)
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week, provider=provider,
        ).threshold_minutes == 30
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week + timedelta(days=7),
            provider=provider,
        ).threshold_minutes == 44

    def test_the_boundary_uses_the_organizations_own_week_start(self, db):
        """Not a hard-coded Monday: it comes from the canonical resolver."""
        week = _open_week(db)
        assert week <= _tz_today(db)
        assert (_tz_today(db) - week).days < 7

    def test_the_boundary_module_derives_nothing_itself(self):
        """It must call the canonical helpers, not reimplement them."""
        body = (CAPTURE_PACKAGE / "boundary.py").read_text(encoding="utf-8")
        assert "week_start_date" in body
        assert "local_date" in body
        for banned in ("isocalendar", "timedelta(days=", "weekday()",
                       "MONDAY", "SUNDAY", "utcnow"):
            assert banned not in body, f"boundary.py uses {banned}"


def _tz_today(db):
    from app._tz import local_date
    return local_date(db, ORG)


# ═════════════════════════════════════════════════════════════════════════════
# U-V: ConfigVersion behaviour, and the limit that needs a ruling
# ═════════════════════════════════════════════════════════════════════════════
class TestExistingVersionLimit:
    def test_a_second_capture_for_the_same_date_is_refused(self, db):
        """Configuration is immutable: correct it with a LATER version."""
        week = _open_week(db)
        assert _capture(db, 30, week).is_captured

        result = _capture(db, 44, week)
        assert result.refusal_reason is RefusalReason.THRESHOLD_ALREADY_CAPTURED
        assert result.threshold_minutes is None

    def test_no_duplicate_config_version_is_created(self, db):
        week = _open_week(db)
        _capture(db, 30, week)
        _capture(db, 44, week)

        versions = db.execute(
            sa.select(ConfigVersion).where(
                ConfigVersion.organization_id == ORG,
                ConfigVersion.effective_from == week,
            )
        ).scalars().all()
        assert len(versions) == 1

    def test_the_original_value_is_untouched_by_the_refused_second_capture(self, db):
        week = _open_week(db)
        _capture(db, 30, week)
        _capture(db, 44, week)
        rows = _threshold_rows(db)
        assert [r.value_minutes for r in rows] == [30]

    def test_an_existing_version_with_other_config_is_refused_not_appended(self, db):
        """Ruling 2: the same-date collision is permanent behaviour.

        A version already starts on the requested date.
        ``(organization_id, effective_from)`` is unique so a second cannot
        begin there, and a version is immutable so this one cannot receive a
        value. Refused with its own reason — never appended, never mutated,
        never duplicated. The administrator picks a later eligible date.
        """
        week = _open_week(db)
        version = _snapshot(db, effective_from=week, rows=(("SHIFT", 2, 480),))
        before = version.content_hash

        result = _capture(db, 30, week)
        assert result.refusal_reason is (
            RefusalReason.EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD)
        assert _threshold_rows(db) == []
        assert version.content_hash == before
        assert _values_in(db, version) == [
            (None, "SHIFT", 2, REQUIREMENT_KEY, 480, f"{REQUIREMENT_KEY}:shift"),
        ]

    def test_unrelated_config_values_are_preserved(self, db):
        """Ruling V: the refusal must not disturb what is already stored."""
        week = _open_week(db)
        version = ConfigVersion(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64, captured_by_label="SYSTEM",
        )
        db.add(version)
        db.flush()
        for tier, rank, minutes in (("SHIFT", 2, 480), ("SCHEDULE", 3, 420)):
            db.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                scope_user_id=None, tier=tier, rank=rank,
                key="required_minutes", value_minutes=minutes,
            ))
        db.flush()
        before = version.content_hash

        _capture(db, 30, week)

        rows = db.execute(
            sa.select(ConfigValue).where(
                ConfigValue.config_version_id == version.id)
        ).scalars().all()
        assert sorted((r.tier, r.value_minutes) for r in rows) == [
            ("SCHEDULE", 420), ("SHIFT", 480),
        ]
        assert version.content_hash == before

    def test_the_requirement_resolver_still_sees_only_requirement_rows(self, db):
        """Key isolation, within the one version a capture writes.

        The threshold now shares its version with the requirement rows it
        carried forward, which is exactly the arrangement the resolver's key
        filter has to survive: it must resolve the requirement and must not
        see ``ECO_THRESHOLD`` as a candidate tier, even though rank 0 would
        outrank every requirement tier if it leaked through.

        Cross-VERSION completeness is proved in ``TestCompleteSnapshot``; this
        one is about the filter.
        """
        from app.config_versioning import DatabaseConfigVersionProvider

        week = _open_week(db)
        assert _capture(db, 30, week).is_captured

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=501, as_of=week,
            provider=DatabaseConfigVersionProvider(db),
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 480          # SHIFT, rank 2
        assert CANONICAL_TIER not in result.chain_evaluated
        assert result.config_version_id == week.isoformat()


def _independent_hash(*, organization_id, version_key, effective_from, values):
    """The documented digest, transcribed rather than imported.

    Importing ``compute_content_hash`` would let a wrong implementation agree
    with itself. This follows the algorithm as specified: values sorted by
    ``(scope_user_id, tier, rank, key)`` with NULL scope ahead of every real
    user id, JSON with sorted keys, no insignificant whitespace and ASCII
    escaping, and identity inside the payload. Provenance — who captured it and
    when — appears nowhere.
    """
    canonical = sorted(
        (
            {
                "scope_user_id": v.get("scope_user_id"),
                # R6 (GV-27): the classification dimension, null when the row is
                # classification-independent. Transcribed here too — this is the
                # third independent reimplementation of the digest, and all
                # three must be changed deliberately or they disagree.
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


def _as_hash_values(tuples):
    """Value tuples from ``_values_in`` in the shape the digest consumes."""
    return [
        {"scope_user_id": s, "tier": t, "rank": r, "key": k,
         "value_minutes": m, "source_ref": ref}
        for (s, t, r, k, m, ref) in tuples
    ]


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 1 / 10: the superseding COMPLETE configuration snapshot
# ═════════════════════════════════════════════════════════════════════════════
class TestCompleteSnapshot:
    """The defect the audit found, and the ruling that closed it.

    ``version_as_of`` selects ONE version and every configuration domain
    resolves from that one, so a version that governs a date must carry every
    domain in force. A threshold-only version would starve requirement
    resolution from its effective date forward — fail-closed, but it would
    switch off day evaluation for an organization that merely set a threshold.
    """

    def test_the_new_version_carries_the_requirement_and_the_threshold(self, db):
        """MANDATORY, per ruling 10. R at D1, capture at D2 > D1."""
        d1 = _open_week(db) - timedelta(days=BASELINE_OFFSET_DAYS)
        d2 = _open_week(db)

        result = _capture(db, 30, d2)
        assert result.is_captured

        source = _version_by_key(db, d1.isoformat())
        target = _version_by_key(db, d2.isoformat())

        # R is untouched: same values, same digest, no threshold.
        assert _values_in(db, source) == [
            (None, "SCHEDULE", 3, REQUIREMENT_KEY, 420,
             f"{REQUIREMENT_KEY}:schedule"),
            (None, "SHIFT", 2, REQUIREMENT_KEY, 480, f"{REQUIREMENT_KEY}:shift"),
        ]
        assert CANONICAL_KEY not in {t[3] for t in _values_in(db, source)}

        # T carries both domains.
        assert {t[3] for t in _values_in(db, target)} == {
            REQUIREMENT_KEY, CANONICAL_KEY}

    @pytest.mark.parametrize("offset_days", [0, 1, 6, 20])
    def test_both_domains_still_resolve_after_the_capture(self, db, offset_days):
        """The starvation defect, directly. Neither domain may go unresolved."""
        from app.config_versioning import DatabaseConfigVersionProvider

        d2 = _open_week(db)
        assert _capture(db, 30, d2).is_captured
        as_of = d2 + timedelta(days=offset_days)

        requirement = resolve_requirement_strict(
            organization_id=ORG, user_id=501, as_of=as_of,
            provider=DatabaseConfigVersionProvider(db),
        )
        assert requirement.outcome is ResolutionOutcome.RESOLVED
        assert requirement.required_minutes == 480

        threshold = resolve_threshold(
            organization_id=ORG, as_of_workweek_start=as_of,
            provider=DatabaseThresholdProvider(db),
        )
        assert threshold.outcome is ThresholdOutcome.RESOLVED
        assert threshold.threshold_minutes == 30

    def test_every_field_of_every_carried_value_is_preserved(self, db):
        """Ruling 3: exact. Key, tier, rank, scope, value and source_ref."""
        d2 = _open_week(db)
        source = _version_by_key(
            db, (d2 - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat())
        before = _values_in(db, source)

        assert _capture(db, 30, d2).is_captured

        carried = [
            t for t in _values_in(db, _version_by_key(db, d2.isoformat()))
            if t[3] != CANONICAL_KEY
        ]
        assert carried == before

    def test_a_worker_scoped_row_is_carried_with_its_scope(self, db):
        """Carry-forward copies what is there; it does not normalise it.

        A Worker-scoped REQUIREMENT row is legitimate (§10.1) — only a
        Worker-scoped THRESHOLD is prohibited — so it must survive verbatim.
        """
        d2 = _open_week(db)
        source = _version_by_key(
            db, (d2 - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat())
        db.add(ConfigValue(
            config_version_id=source.id, organization_id=ORG,
            scope_user_id=822, tier="WORKER_OVERRIDE", rank=1,
            key=REQUIREMENT_KEY, value_minutes=300, source_ref="override:822",
        ))
        db.flush()

        assert _capture(db, 30, d2).is_captured

        carried = _values_in(db, _version_by_key(db, d2.isoformat()))
        assert (822, "WORKER_OVERRIDE", 1, REQUIREMENT_KEY, 300,
                "override:822") in carried

    def test_a_null_source_ref_is_carried_as_null(self, db):
        """Not coerced to an empty string: absent provenance is not blank
        provenance, and the digest distinguishes them."""
        d2 = _open_week(db)
        source = _version_by_key(
            db, (d2 - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat())
        # Rank 6, per owner ruling 1: TENANT_FALLBACK's position in the §10.1
        # ladder. This fixture previously used 5, which was stale.
        db.add(ConfigValue(
            config_version_id=source.id, organization_id=ORG, scope_user_id=None,
            tier="TENANT_FALLBACK", rank=6, key=REQUIREMENT_KEY,
            value_minutes=400, source_ref=None,
        ))
        db.flush()

        assert _capture(db, 30, d2).is_captured
        carried = _values_in(db, _version_by_key(db, d2.isoformat()))
        assert (None, "TENANT_FALLBACK", 6, REQUIREMENT_KEY, 400, None) in carried

    def test_a_later_capture_supersedes_the_threshold_without_duplicating_it(self, db):
        """A threshold CHANGE. The carried snapshot must not bring the old
        threshold row along beside the new one."""
        week = _open_week(db)
        later = week + timedelta(days=7)
        assert _capture(db, 30, week).is_captured
        assert _capture(db, 44, later).is_captured

        target = _version_by_key(db, later.isoformat())
        thresholds = [t for t in _values_in(db, target) if t[3] == CANONICAL_KEY]
        assert thresholds == [
            (None, CANONICAL_TIER, CANONICAL_RANK, CANONICAL_KEY, 44, None)]
        # and the requirement rows came along too
        assert {t[3] for t in _values_in(db, target)} == {
            REQUIREMENT_KEY, CANONICAL_KEY}

    def test_a_defective_threshold_row_is_superseded_not_carried(self, db):
        """A mis-tiered threshold in the source is a recorded defect.

        The new snapshot records the decision just made — one canonical row —
        rather than carrying the defect forward, which would leave the reader
        reporting ``THRESHOLD_INVALID`` for a threshold somebody just set. The
        superseded version keeps it forever, so nothing is erased.
        """
        d2 = _open_week(db)
        source = _version_by_key(
            db, (d2 - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat())
        db.add(ConfigValue(
            config_version_id=source.id, organization_id=ORG, scope_user_id=None,
            tier="SHIFT", rank=9, key=CANONICAL_KEY, value_minutes=99,
        ))
        db.flush()

        assert _capture(db, 30, d2).is_captured

        target_rows = [
            t for t in _values_in(db, _version_by_key(db, d2.isoformat()))
            if t[3] == CANONICAL_KEY
        ]
        assert target_rows == [
            (None, CANONICAL_TIER, CANONICAL_RANK, CANONICAL_KEY, 30, None)]
        # The defect survives where it was recorded.
        assert (None, "SHIFT", 9, CANONICAL_KEY, 99, None) in _values_in(db, source)

    def test_the_carried_count_is_reported(self, db):
        result = _capture(db, 30)
        assert result.carried_value_count == 2
        payload = _audits(db, AUDIT_ACTION_CAPTURED)[0].payload
        assert payload["carried_value_count"] == 2
        assert payload["superseded_config_version_id"] == (
            _open_week(db) - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat()

    def test_organization_isolation_holds_across_the_carry_forward(self, db):
        """Another organization's configuration is not a source for this one."""
        d2 = _open_week(db)
        _snapshot(db, organization_id=OTHER_ORG,
                  effective_from=d2 - timedelta(days=5),
                  rows=(("SHIFT", 2, 111),))

        assert _capture(db, 30, d2).is_captured
        carried = _values_in(db, _version_by_key(db, d2.isoformat()))
        assert 111 not in {t[4] for t in carried}
        assert _threshold_rows(db, OTHER_ORG) == []


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 11: hash integrity
# ═════════════════════════════════════════════════════════════════════════════
class TestHashIntegrity:
    def _source(self, db):
        return _version_by_key(
            db, (_open_week(db) - timedelta(days=BASELINE_OFFSET_DAYS)).isoformat())

    def test_the_superseded_version_hash_is_unchanged(self, db):
        source = self._source(db)
        before = source.content_hash
        assert _capture(db, 30).is_captured
        assert self._source(db).content_hash == before

    def test_the_new_hash_matches_an_independent_calculation(self, db):
        week = _open_week(db)
        result = _capture(db, 30, week)
        target = _version_by_key(db, result.config_version_id)

        assert target.content_hash == _independent_hash(
            organization_id=ORG,
            version_key=week.isoformat(),
            effective_from=week,
            values=_as_hash_values(_values_in(db, target)),
        )

    def test_the_hash_covers_the_threshold_and_not_only_the_carried_values(self, db):
        """Omitting the threshold from the digest must produce a DIFFERENT
        hash — otherwise the digest would not describe the version."""
        week = _open_week(db)
        target = _version_by_key(db, _capture(db, 30, week).config_version_id)
        carried_only = [
            t for t in _values_in(db, target) if t[3] != CANONICAL_KEY]

        assert target.content_hash != _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week, values=_as_hash_values(carried_only),
        )

    def test_the_hash_covers_the_carried_values_and_not_only_the_threshold(self, db):
        week = _open_week(db)
        target = _version_by_key(db, _capture(db, 30, week).config_version_id)
        threshold_only = [
            t for t in _values_in(db, target) if t[3] == CANONICAL_KEY]

        assert target.content_hash != _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week, values=_as_hash_values(threshold_only),
        )

    @pytest.mark.parametrize("actor_name", ["admin", "hr", "system"])
    def test_the_actor_is_outside_the_hash(self, db, actor_name):
        """Ruling 7: provenance is not configuration.

        The independent digest has no actor input at all. If the actor were
        inside it, the stored hash could not match for every actor — so this
        also settles "changing the actor alone would not alter the hash".
        """
        actor = {"admin": _admin(), "hr": _hr(),
                 "system": ActorContext.system()}[actor_name]
        week = _open_week(db)
        target = _version_by_key(
            db, _capture(db, 30, week, actor=actor).config_version_id)

        assert target.content_hash == _independent_hash(
            organization_id=ORG, version_key=week.isoformat(),
            effective_from=week,
            values=_as_hash_values(_values_in(db, target)),
        )
        assert target.captured_by_label == actor.label

    def test_the_superseded_version_remains_immutable(self, db):
        """The listeners still refuse, after a capture as before it."""
        from app.config_versioning import ImmutableConfigurationError

        source = self._source(db)
        assert _capture(db, 30).is_captured

        source.content_hash = "f" * 64
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_a_carried_value_remains_immutable_in_its_new_version(self, db):
        from app.config_versioning import ImmutableConfigurationError

        week = _open_week(db)
        target = _version_by_key(db, _capture(db, 30, week).config_version_id)
        row = db.execute(
            sa.select(ConfigValue).where(
                ConfigValue.config_version_id == target.id,
                ConfigValue.key == REQUIREMENT_KEY,
            )
        ).scalars().first()

        row.value_minutes = 999
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 12: three collisions, three distinct reasons
# ═════════════════════════════════════════════════════════════════════════════
class TestCollisions:
    def test_a_same_date_capture_with_a_threshold_present_is_refused(self, db):
        week = _open_week(db)
        assert _capture(db, 30, week).is_captured

        result = _capture(db, 44, week)
        assert result.refusal_reason is RefusalReason.THRESHOLD_ALREADY_CAPTURED
        assert [r.value_minutes for r in _threshold_rows(db)] == [30]

    def test_a_derived_version_key_held_at_another_date_is_refused(self, db):
        """Ruling 5's pre-check.

        ``version_key`` is caller-supplied elsewhere in Step 6, so a
        date-shaped key can already sit on a version effective on a different
        date. Without the pre-check the unique constraint raises and the
        refusal is reported as a concurrent capture, which it is not.
        """
        week = _open_week(db)
        _snapshot(db, effective_from=week - timedelta(days=14),
                  rows=(("SHIFT", 2, 480),), version_key=week.isoformat())

        result = _capture(db, 30, week)
        assert result.refusal_reason is RefusalReason.VERSION_KEY_ALREADY_IN_USE
        assert result.refusal_reason is not RefusalReason.CONCURRENT_CAPTURE
        assert _threshold_rows(db) == []

    def test_a_lost_race_is_refused_as_a_concurrent_capture(self, db, monkeypatch):
        """The unique constraints are the net behind the pre-checks.

        Simulated by blinding the pre-checks — which is exactly what a race
        does: the checking transaction sees nothing, the constraint sees the
        row. Fails closed, and does not silently reuse the winner's version.
        """
        from app.eco_threshold_capture import service as svc

        week = _open_week(db)
        _snapshot(db, effective_from=week, rows=(("SHIFT", 2, 480),))

        monkeypatch.setattr(svc, "_version_at", lambda *a, **k: None)
        monkeypatch.setattr(svc, "_version_with_key", lambda *a, **k: None)

        result = _capture(db, 30, week)
        assert result.refusal_reason is RefusalReason.CONCURRENT_CAPTURE
        assert result.threshold_minutes is None

    def test_the_three_reasons_are_distinct_tokens(self):
        tokens = {
            RefusalReason.THRESHOLD_ALREADY_CAPTURED.value,
            RefusalReason.VERSION_KEY_ALREADY_IN_USE.value,
            RefusalReason.CONCURRENT_CAPTURE.value,
            RefusalReason.EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD.value,
        }
        assert len(tokens) == 4


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 9: nothing to carry forward — refused, never invented
# ═════════════════════════════════════════════════════════════════════════════
class TestSnapshotPrerequisite:
    """``OTHER_ORG`` has no configuration at all, and gets no baseline here."""

    def test_an_organization_with_no_configuration_is_refused(self, db):
        result = _capture(
            db, 30, actor=ActorContext.for_user(
                _User(710, "admin", organization_id=OTHER_ORG,
                      email="o@example.test")),
            organization_id=OTHER_ORG,
        )
        assert result.outcome is CaptureOutcome.REFUSED
        assert result.refusal_reason is (
            RefusalReason.NO_CONFIGURATION_TO_CARRY_FORWARD)
        assert result.threshold_minutes is None
        assert result.superseded_config_version_id is None

    def test_no_version_is_invented_for_it(self, db):
        _capture(
            db, 30, actor=ActorContext.for_user(
                _User(710, "admin", organization_id=OTHER_ORG,
                      email="o@example.test")),
            organization_id=OTHER_ORG,
        )
        assert _versions(db, OTHER_ORG) == []
        assert _threshold_rows(db, OTHER_ORG) == []

    def test_the_refusal_is_audited(self, db):
        _capture(
            db, 30, actor=ActorContext.for_user(
                _User(710, "admin", organization_id=OTHER_ORG,
                      email="o@example.test")),
            organization_id=OTHER_ORG,
        )
        rows = _audits(db, AUDIT_ACTION_REFUSED)
        assert len(rows) == 1
        assert rows[0].payload["refusal_reason"] == (
            RefusalReason.NO_CONFIGURATION_TO_CARRY_FORWARD.value)
        assert rows[0].organization_id == OTHER_ORG

    def test_requirement_diagnosis_is_not_degraded_by_the_refusal(self, db):
        """WHY it is refused rather than allowed to write a threshold-only
        version.

        With no version at all the resolver reports ``NO_VERSION`` — "nothing
        was captured". A threshold-only version would flip that to
        ``REQUIREMENT_UNRESOLVED`` — "configuration exists but no rank
        answered" — sending an administrator after a broken tier that was
        never configured. Both fail closed; only one tells the truth.
        """
        from app.config_versioning import DatabaseConfigVersionProvider

        _capture(
            db, 30, actor=ActorContext.for_user(
                _User(710, "admin", organization_id=OTHER_ORG,
                      email="o@example.test")),
            organization_id=OTHER_ORG,
        )
        result = resolve_requirement_strict(
            organization_id=OTHER_ORG, user_id=501, as_of=_open_week(db),
            provider=DatabaseConfigVersionProvider(db),
        )
        assert result.outcome is ResolutionOutcome.NO_VERSION


# ═════════════════════════════════════════════════════════════════════════════
# The remaining boundary, recorded as it actually behaves
# ═════════════════════════════════════════════════════════════════════════════
class TestRequirementWritePathBoundary:
    def test_a_later_requirement_capture_does_not_carry_the_threshold_forward(
        self, db
    ):
        """DOCUMENTS A REPORTED BOUNDARY — NOT APPROVED BEHAVIOUR.

        ``capture_version()`` writes exactly the candidates it is given, so a
        requirement version captured after a threshold is domain-partial and
        starves the threshold from its effective date onward — the mirror image
        of the defect ruling 1 closed for the ECO direction.

        Fixing it means changing Step 6's requirement write path, which this
        increment is not authorized to touch, and which cannot be reached in
        production today because nothing constructs that provider. Asserted as
        it currently behaves so the boundary is visible and so this test fails
        loudly the moment somebody changes it.
        """
        from app.config_versioning import DatabaseConfigVersionProvider

        week = _open_week(db)
        later = week + timedelta(days=7)
        assert _capture(db, 30, week).is_captured

        DatabaseConfigVersionProvider(db).capture_version(
            organization_id=ORG, config_version_id="req-later",
            effective_from=later,
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=480)],
        )

        provider = DatabaseThresholdProvider(db)
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=week, provider=provider,
        ).threshold_minutes == 30
        # The boundary: from ``later`` onward the threshold is not configured.
        assert resolve_threshold(
            organization_id=ORG, as_of_workweek_start=later, provider=provider,
        ).outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED


# ═════════════════════════════════════════════════════════════════════════════
# W-Z: actor provenance (rulings E, F)
# ═════════════════════════════════════════════════════════════════════════════
class TestActorProvenance:
    def _version(self, db):
        result = _capture(db, 30, actor=_admin())
        assert result.is_captured
        return _version_by_key(db, result.config_version_id)

    def test_the_live_foreign_key_is_recorded(self, db):
        assert self._version(db).captured_by_user_id == 701

    def test_a_durable_label_is_recorded_beside_it(self, db):
        version = self._version(db)
        assert version.captured_by_label == "admin@example.test"
        assert version.captured_by_label

    def test_the_label_column_is_not_nullable(self):
        column = ConfigVersion.__table__.c[ACTOR_LABEL_COLUMN]
        assert column.nullable is False
        assert column.server_default is not None

    def test_the_label_survives_deletion_of_the_user_row(self, db):
        """Ruling F, in an isolated database with foreign keys ENFORCED.

        The shared test engine does not enable ``PRAGMA foreign_keys``, so
        ``ondelete=SET NULL`` would never fire there and the test would pass
        without proving anything. This one enables it, deletes the user, and
        checks what is left.
        """
        from sqlalchemy import event as sa_event
        from sqlalchemy.orm import Session
        from database.models import Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _fk_on(dbapi_connection, _record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        ConfigVersion.__table__.create(eng, checkfirst=True)
        ConfigValue.__table__.create(eng, checkfirst=True)

        with Session(eng) as session:
            session.add(Organization(id=ORG, name="Org"))
            session.add(User(
                id=701, email="admin@example.test", name="A",
                password="x", role="admin", organization_id=ORG,
            ))
            session.flush()
            session.add(ConfigVersion(
                organization_id=ORG, version_key="2026-09-07",
                effective_from=date(2026, 9, 7),
                captured_at_utc=datetime(2026, 9, 7, tzinfo=timezone.utc),
                content_hash="0" * 64,
                captured_by_user_id=701,
                captured_by_label="admin@example.test",
            ))
            session.commit()

            session.execute(sa.text("DELETE FROM users WHERE id = 701"))
            session.commit()
            session.expire_all()

            version = session.execute(sa.select(ConfigVersion)).scalars().one()
            # The live FK is gone — that is the documented SET NULL behaviour...
            assert version.captured_by_user_id is None
            # ...and the attribution survived it, which is ruling F.
            assert version.captured_by_label == "admin@example.test"

    def test_the_label_cannot_be_rewritten_after_capture(self, db):
        from app.config_versioning import ImmutableConfigurationError

        version = self._version(db)
        version.captured_by_label = "someone@else.test"
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_label_is_outside_the_configuration_content_hash(self, db):
        """Provenance is not configuration: an identical configuration must not
        hash differently because a different person recorded it."""
        from app.config_versioning import compute_content_hash

        assert "captured_by_label" not in inspect.signature(
            compute_content_hash).parameters

    def test_a_system_capture_records_the_sentinel_not_a_blank(self, db):
        result = _capture(db, 30, actor=ActorContext.system())
        assert _version_by_key(db, result.config_version_id).captured_by_label == (
            "SYSTEM")


# ═════════════════════════════════════════════════════════════════════════════
# AA-AC: audit (ruling G)
# ═════════════════════════════════════════════════════════════════════════════
class TestAudit:
    def test_a_capture_is_audited(self, db):
        _capture(db, 30)
        rows = _audits(db, AUDIT_ACTION_CAPTURED)
        assert len(rows) == 1
        assert rows[0].organization_id == ORG
        assert rows[0].actor_id == 701

    def test_a_refusal_is_audited(self, db):
        _capture(db, 99999)
        rows = _audits(db, AUDIT_ACTION_REFUSED)
        assert len(rows) == 1
        assert rows[0].payload["refusal_reason"] == (
            RefusalReason.VALUE_OUT_OF_RANGE.value)

    @pytest.mark.parametrize("scenario", [
        ("no-actor", None, 30),
        ("bad-role", "employee", 30),
        ("out-of-range", "admin", 0),
        ("not-an-integer", "admin", "30"),
    ])
    def test_every_refusal_path_is_audited(self, db, scenario):
        name, role, minutes = scenario
        actor = None if role is None else ActorContext.for_user(
            _User(705, role, email="p@example.test"))
        result = capture_threshold(
            db, organization_id=ORG, threshold_minutes=minutes,
            effective_from=_open_week(db), actor=actor,
        )
        assert result.outcome is CaptureOutcome.REFUSED, name
        assert len(_audits(db, AUDIT_ACTION_REFUSED)) == 1, name

    def test_the_capture_audit_carries_the_required_provenance(self, db):
        week = _open_week(db)
        _capture(db, 30, week)
        payload = _audits(db, AUDIT_ACTION_CAPTURED)[0].payload

        for field in ("key", "tier", "rank", "scope", "outcome",
                      "threshold_minutes", "previous_threshold_minutes",
                      "previous_config_version_id", "config_version_id",
                      "effective_from", "captured_at_utc", "content_hash",
                      "actor_label", "actor_user_id", "actor_role",
                      "actor_is_system", "open_workweek_start"):
            assert field in payload, field

        assert payload["threshold_minutes"] == 30
        assert payload["previous_threshold_minutes"] is None
        assert payload["effective_from"] == week.isoformat()
        assert payload["actor_label"] == "admin@example.test"
        assert payload["key"] == CANONICAL_KEY
        assert payload["tier"] == CANONICAL_TIER

    def test_the_audit_records_the_before_value_on_a_change(self, db):
        """Ruling G's before/after provenance, across two workweeks."""
        week = _open_week(db)
        _capture(db, 30, week)
        _capture(db, 44, week + timedelta(days=7))

        payload = _audits(db, AUDIT_ACTION_CAPTURED)[1].payload
        assert payload["previous_threshold_minutes"] == 30
        assert payload["previous_config_version_id"] == week.isoformat()
        assert payload["threshold_minutes"] == 44

    def test_durable_attribution_is_in_the_payload_not_only_the_fk(self, db):
        """``AuditLog.actor_id`` is itself SET NULL, so an FK-bound actor
        vanishes from the trail exactly when it matters."""
        _capture(db, 30)
        payload = _audits(db, AUDIT_ACTION_CAPTURED)[0].payload
        assert payload["actor_label"] == "admin@example.test"

    def test_the_audit_vocabulary_is_configuration_not_events(self):
        """Configuration audit must never be mistaken for a crossing event."""
        for token in (AUDIT_ENTITY, AUDIT_ACTION_CAPTURED, AUDIT_ACTION_REFUSED):
            for banned in ("REACHED", "WITHDRAWN", "PRIMARY", "CROSS",
                           "SEQUENCE", "NOTIF"):
                assert banned not in token.upper(), token


# ═════════════════════════════════════════════════════════════════════════════
# AD: concurrency
# ═════════════════════════════════════════════════════════════════════════════
class TestConcurrency:
    def test_the_unique_constraint_backs_the_pre_check(self, db):
        """Two versions cannot claim one organization and effective date."""
        week = _open_week(db)
        _capture(db, 30, week)

        db.add(ConfigVersion(
            organization_id=ORG, version_key="racer",
            effective_from=week,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64, captured_by_label="SYSTEM",
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_race_does_not_produce_two_threshold_rows(self, db):
        week = _open_week(db)
        _capture(db, 30, week)
        _capture(db, 30, week)
        _capture(db, 44, week)
        assert len(_threshold_rows(db)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# The revision
# ═════════════════════════════════════════════════════════════════════════════
class TestRevision:
    PATH = (BACKEND / "alembic" / "versions"
            / f"{REVISION}_config_version_actor_label.py")

    def _tree(self):
        return ast.parse(self.PATH.read_text(encoding="utf-8"))

    def test_the_revision_exists_and_is_a_single_parent_child(self):
        body = self.PATH.read_text(encoding="utf-8")
        assert f"revision: str = '{REVISION}'" in body
        assert "down_revision: Union[str, Sequence[str], None] = 'c9d4a7e2f581'" in body

    def test_the_graph_has_one_root_and_one_head(self):
        """Resolved by Alembic itself, not by a parser of my own.

        The graph contains a MERGE revision, whose ``down_revision`` is a tuple
        of two parents. Any hand-rolled reader that expects a single string
        misreads it and reports a fork that does not exist — which is exactly
        the false alarm this programme has already chased once. Alembic's
        ``ScriptDirectory`` is the authority; it reads scripts only, with no
        ``env.py`` and no database.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == (
            EXPECTED_REVISION_COUNT)

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)

        # Head re-baselined by §45.D-15: c4e9b7d2a583 is an ordinary
        # single-parent child of b7c4e0a92d51. Still one base, one head.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"], sd.get_heads()
        assert list(sd.get_bases()) == ["81c63dee733a"], sd.get_bases()

    def test_it_adds_exactly_one_column(self):
        calls = [
            n for n in ast.walk(self._tree())
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "add_column"
        ]
        assert len(calls) == 1

    def test_it_creates_no_table_and_alters_nothing_else(self):
        banned = {"create_table", "alter_column", "drop_column", "rename_table",
                  "drop_constraint", "drop_index", "create_index",
                  "bulk_insert", "execute"}
        called = {
            getattr(n.func, "attr", None) for n in ast.walk(self._tree())
            if isinstance(n, ast.Call)
        }
        # drop_column appears in downgrade() only, which is expected.
        assert (called & banned) <= {"drop_column"}, sorted(called & banned)

    def test_it_targets_only_config_version(self):
        body = self.PATH.read_text(encoding="utf-8")
        for table in ("work_day_ledger", "work_state_interval", "config_value",
                      "eco_day_record", "app_settings", "rollout_control"):
            assert f'"{table}"' not in body and f"'{table}'" not in body, table

    def test_it_contains_no_backfill_and_opens_no_connection(self):
        body = self.PATH.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv",
                       "INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert banned not in body, banned

    def test_the_column_is_not_null_with_a_server_default(self):
        """So it can be added to a table that already holds rows."""
        body = self.PATH.read_text(encoding="utf-8")
        assert "nullable=False" in body
        assert 'server_default="SYSTEM"' in body

    def test_it_does_not_mention_the_threshold_key_or_tier(self):
        """The value lives in configuration rows, never in DDL."""
        body = self.PATH.read_text(encoding="utf-8")
        assert CANONICAL_KEY not in body
        assert CANONICAL_TIER not in body

    def test_applying_it_twice_is_a_no_op(self):
        """A create_all-built database already has the column."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        ConfigVersion.__table__.create(eng, checkfirst=True)

        import importlib.util
        spec = importlib.util.spec_from_file_location("_rev_actor", self.PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for _ in range(2):
            with eng.begin() as connection:
                ctx = MigrationContext.configure(connection)
                with Operations.context(ctx):
                    module.upgrade()

        columns = {c["name"] for c in sa.inspect(eng).get_columns("config_version")}
        assert ACTOR_LABEL_COLUMN in columns

    def test_it_is_a_no_op_when_the_table_does_not_exist(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location("_rev_actor2", self.PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with eng.begin() as connection:
            ctx = MigrationContext.configure(connection)
            with Operations.context(ctx):
                module.upgrade()
        assert "config_version" not in set(sa.inspect(eng).get_table_names())


# ═════════════════════════════════════════════════════════════════════════════
# AE-AK: scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_neither_package_is_wired_to_the_application(self):
        foundation = (
            "eco_threshold", "eco_threshold_capture", "config_versioning",
            # First capture composes this package's boundary helper and actor
            # type under owner ruling 7, so it is foundation, not application.
            "config_first_capture",
            "requirement_resolution", "work_ledger", "leave_intervals",
            "worker_context", "rollout", "eval_day", "eval_week",
            # EVAL-EVENT reads T to perform the comparison the reader package
            # explicitly defers to it. Foundation, not application.
            "eval_event",
            # Both compose AUTHORIZED_ROLES -- the one ECO governance
            # authorization vocabulary -- rather than restating it.
            # They record governance evidence, not application state.
            "jurisdiction", "compliance_evidence",
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
                if module and "eco_threshold" in module:
                    offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_wiring_guard_would_notice(self):
        scanned = [
            p for p in (BACKEND / "app").rglob("*.py")
            if "eco_threshold" not in p.parts
            and "eco_threshold_capture" not in p.parts
        ]
        assert len(scanned) > 100

    def test_the_reader_gained_no_write_method(self):
        """Ruling 4: capture logic must not be in the reader."""
        for path in sorted(READER_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for banned in ("capture_threshold", "capture", "capture_version",
                           "set_threshold", "update_threshold", "save"):
                assert banned not in names, f"{path.name} defines {banned}"

    def test_the_reader_still_derives_no_week(self):
        """Its AST guard was not loosened to let capture reuse it."""
        banned_calls = {"week_start_date", "isocalendar", "today", "utcnow", "now"}
        for path in sorted(READER_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    assert name not in banned_calls, f"{path.name}:{node.lineno}"

    def test_no_event_or_notification_vocabulary_in_the_capture_package(self):
        banned_identifiers = (
            "sequence", "crossed", "crossing", "withdraw", "withdrawn",
            "withdrawal", "notification", "notify", "weekly_total_minutes",
        )
        banned_names = ("ECO_THRESHOLD_REACHED", "ECO_THRESHOLD_WITHDRAWN")
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for term in banned_identifiers:
                offenders = [t for t in tokens
                             if t == term or t.startswith(term + "_")]
                assert offenders == [], f"{path.name} uses {offenders}"
            for term in banned_names:
                assert [t for t in tokens if term in t] == [], path.name

    def test_no_rollout_or_eco_v1_change(self):
        rollout = (BACKEND / "app" / "rollout" / "models.py").read_text(
            encoding="utf-8")
        assert 'default="SYSTEM"' in rollout          # untouched precedent
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("rollout", "EARLY_CLOCK_OUT_FEATURE_ENABLED"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_no_route_schema_or_scheduler_was_added(self):
        """No HTTP surface, no schema class, no scheduler entry.

        Read from the code: the docstrings say "no route, service or scheduler
        calls it" precisely to record the boundary, and a text scan would fail
        on that sentence.
        """
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
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
            for banned in ("APIRouter", "BaseModel", "Depends", "add_job",
                           "APScheduler"):
                assert banned not in tokens, f"{path.name} uses {banned}"

            # A route is a decorator, so the decorators present are pinned to
            # the handful of language-level ones. Anything else — including
            # anything attribute-shaped like ``@router.post`` — fails.
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
                        f"with {name!r}"
                    )

    def test_the_golden_vectors_are_untouched_and_still_all_blocked(self):
        import json

        fixture = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert data["_vector_count"] == 28
        assert len(data["vectors"]) == 28
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

    def test_the_terminology_guard_is_satisfied_by_the_capture_package(self):
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        for token in (AUDIT_ENTITY, AUDIT_ACTION_CAPTURED, AUDIT_ACTION_REFUSED):
            assert find_prohibited_terms(token) == [], token
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            hits = scan_text(path.read_text(encoding="utf-8"))
            assert hits == [], format_hits(hits, source=path.name)


def _code_tokens(tree: ast.Module) -> set:
    """Identifiers and runtime strings — docstrings excluded.

    Same helper as the reader suite: a text scan cannot tell "implements X"
    from "deliberately does not implement X", and these docstrings do a lot of
    the second.
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
# The result type
# ═════════════════════════════════════════════════════════════════════════════
class TestResultType:
    def test_a_refusal_cannot_carry_a_threshold(self):
        with pytest.raises(ValueError):
            ThresholdCaptureResult(
                outcome=CaptureOutcome.REFUSED, organization_id=ORG,
                refusal_reason=RefusalReason.VALUE_OUT_OF_RANGE,
                threshold_minutes=30,
            )

    def test_a_refusal_must_state_a_reason(self):
        with pytest.raises(ValueError):
            ThresholdCaptureResult(
                outcome=CaptureOutcome.REFUSED, organization_id=ORG)

    def test_a_capture_must_carry_a_value_and_a_version(self):
        with pytest.raises(ValueError):
            ThresholdCaptureResult(
                outcome=CaptureOutcome.CAPTURED, organization_id=ORG)
        with pytest.raises(ValueError):
            ThresholdCaptureResult(
                outcome=CaptureOutcome.CAPTURED, organization_id=ORG,
                threshold_minutes=30,
            )

    def test_a_capture_must_name_the_snapshot_it_superseded(self):
        """There is always one: a capture with nothing to carry forward is
        refused, so a CAPTURED result that names none is a bug."""
        base = dict(
            outcome=CaptureOutcome.CAPTURED, organization_id=ORG,
            threshold_minutes=30, effective_from=date(2026, 9, 7),
            config_version_id="2026-09-07",
        )
        with pytest.raises(ValueError):
            ThresholdCaptureResult(**base)
        with pytest.raises(ValueError):
            ThresholdCaptureResult(**base, superseded_config_version_id="x")
        # Complete, and therefore accepted.
        assert ThresholdCaptureResult(
            **base, superseded_config_version_id="x", carried_value_count=0,
        ).is_captured

    def test_a_refusal_superseded_nothing(self):
        for extra in ({"superseded_config_version_id": "x"},
                      {"carried_value_count": 0}):
            with pytest.raises(ValueError):
                ThresholdCaptureResult(
                    outcome=CaptureOutcome.REFUSED, organization_id=ORG,
                    refusal_reason=RefusalReason.CONCURRENT_CAPTURE, **extra,
                )

    def test_the_result_is_frozen(self):
        result = ThresholdCaptureResult(
            outcome=CaptureOutcome.REFUSED, organization_id=ORG,
            refusal_reason=RefusalReason.ACTOR_REQUIRED,
        )
        with pytest.raises(Exception):
            result.refusal_reason = None        # type: ignore[misc]
