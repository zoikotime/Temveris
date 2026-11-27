"""
W5 — the ECO threshold reader (read path only).
===============================================

Three things are tested here:

1. **The reader contract**, run against BOTH provider implementations. The
   in-memory reference provider is the executable specification; the
   database-backed adapter must reproduce it exactly. Same technique as Step 6:
   the semantics were settled in the reference first, so the stored
   implementation is held to them rather than to tests written afterwards to
   match whatever it happened to do.

2. **Isolation from requirement resolution**, in both directions — the threshold
   reader must not return a requirement value, and the requirement resolver must
   not see a threshold value, even when both live in one ``ConfigVersion``.

3. **Scope control** — nothing was wired, no migration was added, no golden
   vector became executable, and no numeric default was introduced.

Expected values here are written out independently. The canonical key, tier and
range are transcribed literally rather than imported, so a wrong production
constant cannot agree with itself — the convention the Step 6 suite already
follows.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.config_versioning import (
    ConfigValue,
    ConfigVersion,
    DatabaseConfigVersionProvider,
)
from app.eco_threshold import (
    DatabaseThresholdProvider,
    InMemoryThresholdProvider,
    StoredThresholdVersion,
    ThresholdCandidate,
    ThresholdOutcome,
    ThresholdProvider,
    ThresholdResolution,
    resolve_threshold,
)
from app.eco_threshold.reader import (
    DEFECT_AMBIGUOUS_ROWS,
    DEFECT_TIER_NOT_CANONICAL,
    DEFECT_VALUE_ABSENT,
    DEFECT_VALUE_OUT_OF_RANGE,
    DEFECT_WORKER_SCOPED,
)
from app.requirement_resolution import (
    RequirementCandidate,
    ResolutionOutcome,
    resolve_requirement_strict,
)
from app.work_ledger import WorkDayLedger, WorkStateInterval

ORG = 11
OTHER_ORG = 12
USER = 501

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eco_threshold"

# ── Independently transcribed literals ───────────────────────────────────────
#: The owner-ruled canonical key. Written out, NOT imported: importing the
#: production constant would make this test agree with any value at all.
CANONICAL_KEY = "eco_weekly_attributable_shortfall_threshold_minutes"
CANONICAL_TIER = "ECO_THRESHOLD"
CANONICAL_RANK = 0
MIN_MINUTES = 1
MAX_MINUTES = 1440

#: The §10.1 requirement-precedence labels. The threshold tier is none of them.
REQUIREMENT_TIERS = (
    "WORKER_OVERRIDE", "SHIFT", "SCHEDULE",
    "EMPLOYMENT_POLICY", "TENANT_TYPE_DEFAULT", "TENANT_FALLBACK",
)

#: The requirement key, transcribed for the isolation tests.
REQUIREMENT_KEY_LITERAL = "required_minutes"

#: Figures that must never become a threshold. 30 is a GV-04/GV-05 scenario
#: value; the rest are the daily/risk quantities this threshold is not.
FORBIDDEN_DEFAULTS = (30, 0, 28800, 31500)


def _code_tokens(tree: ast.Module) -> set:
    """Identifiers and runtime strings in a module — docstrings excluded.

    A text scan cannot distinguish "this module implements X" from "this module
    deliberately does not implement X", and the docstrings here do a lot of the
    second. So the guards that assert an absence read the code instead.
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


@pytest.fixture(scope="module", autouse=True)
def _threshold_schema(engine):
    """The Step 6 tables plus their FK targets. Additive, per-model, like Step 6."""
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)


# ═════════════════════════════════════════════════════════════════════════════
# Seeding — one shape, both providers
# ═════════════════════════════════════════════════════════════════════════════
def _candidate(
    *, key=CANONICAL_KEY, tier=CANONICAL_TIER, scope_user_id=None, value_minutes=30
):
    return ThresholdCandidate(
        key=key, tier=tier, scope_user_id=scope_user_id, value_minutes=value_minutes,
    )


class _Seeder:
    """Writes the same logical configuration into either provider.

    Exists so one test body can assert on both implementations. The database
    seeder writes ``ConfigValue`` rows directly: there is no threshold capture
    path, deliberately — who may set a threshold is unruled — so a test cannot
    and must not go through one.
    """

    def __init__(self, kind, db):
        self.kind = kind
        self.db = db
        self._versions = []          # (version_key, effective_from, [candidates])

    def version(self, version_key, effective_from, candidates=(), *, rank=CANONICAL_RANK):
        self._versions.append((version_key, effective_from, list(candidates), rank))
        return self

    def build(self) -> ThresholdProvider:
        if self.kind == "in-memory-reference":
            return InMemoryThresholdProvider({
                ORG: [
                    StoredThresholdVersion(
                        config_version_id=vk,
                        effective_from=eff,
                        candidates=tuple(cands),
                    )
                    for vk, eff, cands, _rank in self._versions
                ]
            })

        for vk, eff, cands, rank in self._versions:
            row = ConfigVersion(
                organization_id=ORG, version_key=vk, effective_from=eff,
                captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                content_hash="0" * 64,
            )
            self.db.add(row)
            self.db.flush()
            for c in cands:
                self.db.add(ConfigValue(
                    config_version_id=row.id, organization_id=ORG,
                    scope_user_id=c.scope_user_id, tier=c.tier, rank=rank,
                    key=c.key, value_minutes=c.value_minutes,
                ))
            self.db.flush()
        return DatabaseThresholdProvider(self.db)


PROVIDER_IDS = ["in-memory-reference", "database-backed"]


@pytest.fixture(params=PROVIDER_IDS)
def seeder(request, db):
    return _Seeder(request.param, db)


def _resolve(provider, as_of, organization_id=ORG):
    return resolve_threshold(
        organization_id=organization_id,
        as_of_workweek_start=as_of,
        provider=provider,
    )


# ═════════════════════════════════════════════════════════════════════════════
# A-F: the canonical row and the reader's shape
# ═════════════════════════════════════════════════════════════════════════════
class TestCanonicalIdentity:
    def test_the_canonical_key_is_exactly_as_ruled(self):
        from app.eco_threshold import ECO_THRESHOLD_KEY
        assert ECO_THRESHOLD_KEY == CANONICAL_KEY

    def test_the_canonical_tier_is_exactly_as_ruled(self):
        from app.eco_threshold import ECO_THRESHOLD_TIER
        assert ECO_THRESHOLD_TIER == CANONICAL_TIER

    def test_the_canonical_tier_is_not_a_requirement_precedence_tier(self):
        """The two axes that both use the word "tier" must not overlap."""
        assert CANONICAL_TIER not in REQUIREMENT_TIERS

    def test_the_canonical_tier_is_not_the_eco_event_tier(self):
        """``PRIMARY`` is the EVENT tier and must never reach config_value."""
        assert CANONICAL_TIER != "PRIMARY"
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            assert '"PRIMARY"' not in body and "'PRIMARY'" not in body, path.name

    def test_the_canonical_rank_is_zero(self):
        from app.eco_threshold import ECO_THRESHOLD_RANK
        assert ECO_THRESHOLD_RANK == CANONICAL_RANK

    def test_the_declared_range_is_one_to_one_thousand_four_hundred_and_forty(self):
        from app.eco_threshold import MAX_THRESHOLD_MINUTES, MIN_THRESHOLD_MINUTES
        assert (MIN_THRESHOLD_MINUTES, MAX_THRESHOLD_MINUTES) == (MIN_MINUTES, MAX_MINUTES)

    def test_the_literals_fit_the_existing_columns_so_no_migration_is_needed(self):
        assert len(CANONICAL_KEY) <= ConfigValue.__table__.c.key.type.length
        assert len(CANONICAL_TIER) <= ConfigValue.__table__.c.tier.type.length


class TestReaderShape:
    def test_the_reader_takes_no_user_id(self):
        """Organization-scoped BY CONSTRUCTION, not by convention.

        A per-Worker lookup must be unexpressible, so the worker-scope
        replacement semantics of requirement resolution cannot be inherited by
        accident. Same technique as the ledger dropping ``workweek_key``.
        """
        params = set(inspect.signature(resolve_threshold).parameters)
        assert "user_id" not in params
        assert params == {"organization_id", "as_of_workweek_start", "provider"}

    def test_the_candidate_type_carries_no_rank(self):
        """Resolving ambiguity by rank must be impossible, not merely banned."""
        assert "rank" not in ThresholdCandidate.__dataclass_fields__
        assert set(ThresholdCandidate.__dataclass_fields__) == {
            "key", "tier", "scope_user_id", "value_minutes",
        }

    def test_both_implementations_satisfy_the_port(self, seeder):
        assert isinstance(seeder.build(), ThresholdProvider)

    def test_the_reader_has_no_write_methods(self):
        """Read-only. No capture, update or delete anywhere in the package."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for banned in ("capture", "capture_version", "set_threshold",
                           "update_threshold", "delete_threshold", "save"):
                assert banned not in names, f"{path.name} defines {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# G-I: isolation from requirement resolution, both directions
# ═════════════════════════════════════════════════════════════════════════════
class TestRequirementIsolation:
    """One ``ConfigVersion`` holding a requirement row and a threshold row.

    Database-backed specifically: the two reference providers each hold one
    domain and cannot mix them, which is the type-level half of the guarantee.
    """

    def _mixed_version(self, db, *, worker_scoped_requirement=False):
        config = DatabaseConfigVersionProvider(db)
        kwargs = dict(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=480)],
        )
        if worker_scoped_requirement:
            kwargs["candidates_by_user"] = {
                USER: [RequirementCandidate(
                    tier="WORKER_OVERRIDE", rank=1, minutes=300)],
            }
        config.capture_version(**kwargs)

        version = db.execute(
            sa.select(ConfigVersion).where(
                ConfigVersion.organization_id == ORG,
                ConfigVersion.version_key == "v1",
            )
        ).scalars().one()
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG, scope_user_id=None,
            tier=CANONICAL_TIER, rank=CANONICAL_RANK,
            key=CANONICAL_KEY, value_minutes=45,
        ))
        db.flush()
        return config, DatabaseThresholdProvider(db), version.id

    def test_both_rows_coexist_in_one_version(self, db):
        """Guard the guard: the assertions below must not pass vacuously."""
        _, _, pk = self._mixed_version(db)
        keys = sorted(db.execute(
            sa.select(ConfigValue.key).where(ConfigValue.config_version_id == pk)
        ).scalars().all())
        assert keys == sorted([CANONICAL_KEY, REQUIREMENT_KEY_LITERAL])

    def test_the_threshold_reader_does_not_return_the_requirement(self, db):
        _, threshold, _ = self._mixed_version(db)
        result = _resolve(threshold, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.RESOLVED
        assert result.threshold_minutes == 45
        assert not hasattr(result, "required_minutes")

    def test_the_requirement_resolver_does_not_see_the_threshold(self, db):
        config, _, _ = self._mixed_version(db)
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=config,
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 480
        assert result.source_tier == "SHIFT"
        assert CANONICAL_TIER not in result.chain_evaluated
        assert result.invalid_candidates == ()

    def test_an_org_threshold_resolves_despite_worker_scoped_requirement_config(
        self, db
    ):
        """The regression the whole design exists to prevent.

        Requirement resolution REPLACES the organization-wide candidate set with
        a Worker's own when one exists. If the threshold reader inherited that,
        this organization's threshold would vanish for this Worker.
        """
        config, threshold, _ = self._mixed_version(
            db, worker_scoped_requirement=True)

        # The Worker's requirement really does come from their own override,
        # so the replacement behaviour is genuinely active here.
        requirement = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=config,
        )
        assert requirement.source_tier == "WORKER_OVERRIDE"
        assert requirement.required_minutes == 300

        # And the threshold is unaffected, because it never asked about a Worker.
        result = _resolve(threshold, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.RESOLVED
        assert result.threshold_minutes == 45


# ═════════════════════════════════════════════════════════════════════════════
# J-O: resolution and the fail-closed defects
# ═════════════════════════════════════════════════════════════════════════════
class TestResolution:
    def test_a_canonical_row_resolves(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=30)]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.RESOLVED
        assert result.threshold_minutes == 30
        assert result.config_version_id == "v1"
        assert result.effective_from == date(2026, 1, 1)
        assert result.defect is None
        assert result.is_evaluable
        assert not result.requires_administrator_alert

    @pytest.mark.parametrize("minutes", [MIN_MINUTES, 30, 44, MAX_MINUTES])
    def test_the_whole_declared_range_resolves(self, seeder, minutes):
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=minutes)]).build()
        assert _resolve(provider, date(2026, 6, 1)).threshold_minutes == minutes

    def test_rank_does_not_influence_resolution(self, seeder):
        """A row stored at any rank resolves identically. Rank is structural."""
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=30)], rank=99).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.RESOLVED
        assert result.threshold_minutes == 30

    def test_a_wrong_tier_is_invalid_not_unconfigured(self, seeder):
        """Somebody configured a threshold and it cannot be used — a different
        fact from nobody having configured one, and it must not be hidden."""
        provider = seeder.version(
            "v1", date(2026, 1, 1),
            [_candidate(tier="SHIFT", value_minutes=30)]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_INVALID
        assert result.outcome is not ThresholdOutcome.THRESHOLD_NOT_CONFIGURED
        assert result.defect == DEFECT_TIER_NOT_CANONICAL
        assert result.threshold_minutes is None

    @pytest.mark.parametrize("tier", REQUIREMENT_TIERS)
    def test_no_requirement_precedence_tier_can_carry_the_threshold(
        self, seeder, tier
    ):
        provider = seeder.version(
            "v1", date(2026, 1, 1),
            [_candidate(tier=tier, value_minutes=30)]).build()
        assert _resolve(provider, date(2026, 6, 1)).outcome is (
            ThresholdOutcome.THRESHOLD_INVALID)

    def test_two_rows_differing_only_in_tier_are_ambiguous(self, seeder):
        """The gap the unique index leaves open, and the reader closes.

        ``uq_config_value_scope`` covers (version, scope, tier, key), so two
        rows under DIFFERENT tiers are storable. Neither is chosen: not by rank,
        and not by preferring the correctly-tiered one, because "which did the
        administrator mean" has no answer this code may invent.
        """
        provider = seeder.version("v1", date(2026, 1, 1), [
            _candidate(value_minutes=30),
            _candidate(tier="SHIFT", value_minutes=600),
        ]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_INVALID
        assert result.defect == DEFECT_AMBIGUOUS_ROWS
        assert result.threshold_minutes is None

    def test_a_null_value_is_invalid(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=None)]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_INVALID
        assert result.defect == DEFECT_VALUE_ABSENT

    @pytest.mark.parametrize("minutes", [0, -1, MAX_MINUTES + 1, 28800])
    def test_an_out_of_range_value_is_invalid(self, minutes):
        """Validated in the READER, not only by the database CHECK.

        Deliberately not run through the database provider: the CHECK there
        refuses the write outright, so that path cannot reach this branch at all
        (``TestStorage.test_out_of_range_values_are_also_refused_at_rest``
        covers it). The reference provider has no constraint, which makes it the
        only way to prove the reader is fail-closed against storage whose
        constraints differ from this schema's — a ``create_all`` database and an
        Alembic one must not be assumed to agree.
        """
        provider = InMemoryThresholdProvider({ORG: [StoredThresholdVersion(
            config_version_id="v1", effective_from=date(2026, 1, 1),
            candidates=(_candidate(value_minutes=minutes),),
        )]})
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_INVALID
        assert result.defect == DEFECT_VALUE_OUT_OF_RANGE
        assert result.threshold_minutes is None

    def test_a_worker_scoped_threshold_is_prohibited(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 1, 1),
            [_candidate(scope_user_id=USER, value_minutes=30)]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.WORKER_SCOPED_THRESHOLD_PROHIBITED
        assert result.defect == DEFECT_WORKER_SCOPED
        assert result.threshold_minutes is None

    def test_a_worker_scoped_row_beside_a_valid_org_row_still_fails_closed(
        self, seeder
    ):
        """Deliberate: the organization row is NOT preferred.

        Its presence means someone configured a dimension no ruling permits, so
        the configuration is not trustworthy. Silently using the organization
        row would leave someone believing a per-Worker threshold applied when it
        never did.
        """
        provider = seeder.version("v1", date(2026, 1, 1), [
            _candidate(value_minutes=30),
            _candidate(scope_user_id=USER, value_minutes=600),
        ]).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.WORKER_SCOPED_THRESHOLD_PROHIBITED
        assert result.threshold_minutes is None

    def test_a_version_with_no_threshold_row_is_not_configured(self, seeder):
        provider = seeder.version("v1", date(2026, 1, 1), []).build()
        result = _resolve(provider, date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED
        assert result.threshold_minutes is None
        assert result.config_version_id == "v1"
        assert result.requires_administrator_alert

    def test_a_version_holding_only_a_requirement_row_is_not_configured(self, db):
        """A requirement value is not a threshold, however plausible its number."""
        DatabaseConfigVersionProvider(db).capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=480)],
        )
        result = _resolve(DatabaseThresholdProvider(db), date(2026, 6, 1))
        assert result.outcome is ThresholdOutcome.THRESHOLD_NOT_CONFIGURED
        assert result.threshold_minutes is None


# ═════════════════════════════════════════════════════════════════════════════
# P-T: effective dating against the workweek START date
# ═════════════════════════════════════════════════════════════════════════════
class TestEffectiveDating:
    def test_below_the_historical_floor_there_is_no_version(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 6, 1), [_candidate(value_minutes=30)]).build()
        result = _resolve(provider, date(2026, 5, 31))
        assert result.outcome is ThresholdOutcome.NO_CONFIG_VERSION
        assert result.threshold_minutes is None
        assert result.config_version_id is None
        # Not an administrator defect: history simply does not reach back there.
        assert not result.requires_administrator_alert

    def test_no_versions_at_all_is_no_config_version(self, seeder):
        assert _resolve(seeder.build(), date(2026, 6, 1)).outcome is (
            ThresholdOutcome.NO_CONFIG_VERSION)

    def test_effective_exactly_on_the_workweek_start_governs_it(self, seeder):
        """Inclusive lower bound: a version effective on the 31st governs the
        week beginning the 31st."""
        provider = seeder.version(
            "v1", date(2026, 8, 31), [_candidate(value_minutes=30)]).build()
        assert _resolve(provider, date(2026, 8, 31)).threshold_minutes == 30

    def test_effective_the_next_day_does_not_govern_that_workweek(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 9, 1), [_candidate(value_minutes=30)]).build()
        assert _resolve(provider, date(2026, 8, 31)).outcome is (
            ThresholdOutcome.NO_CONFIG_VERSION)

    def test_a_change_effective_mid_week_does_not_change_that_workweek(self, seeder):
        """The as-of anchor is the START date, so a mid-week change cannot reach
        back into the week in progress — not by special-casing, but because a
        version effective after the start is never the latest at or before it."""
        provider = (
            seeder
            .version("v1", date(2026, 8, 24), [_candidate(value_minutes=30)])
            .version("v2", date(2026, 9, 2), [_candidate(value_minutes=44)])
            .build()
        )
        result = _resolve(provider, date(2026, 8, 31))
        assert result.threshold_minutes == 30
        assert result.config_version_id == "v1"

    def test_the_next_workweek_receives_the_newly_effective_value(self, seeder):
        provider = (
            seeder
            .version("v1", date(2026, 8, 24), [_candidate(value_minutes=30)])
            .version("v2", date(2026, 9, 2), [_candidate(value_minutes=44)])
            .build()
        )
        # Week beginning 2026-09-07 is the first whose start is on/after v2.
        result = _resolve(provider, date(2026, 9, 7))
        assert result.threshold_minutes == 44
        assert result.config_version_id == "v2"

    def test_the_latest_applicable_version_wins_not_the_first(self, seeder):
        provider = (
            seeder
            .version("v1", date(2026, 1, 1), [_candidate(value_minutes=30)])
            .version("v2", date(2026, 3, 1), [_candidate(value_minutes=44)])
            .version("v3", date(2026, 5, 1), [_candidate(value_minutes=60)])
            .build()
        )
        assert _resolve(provider, date(2026, 4, 1)).threshold_minutes == 44

    def test_a_later_capture_does_not_move_an_earlier_workweeks_answer(self, seeder):
        """§17/§30.1: a completed workweek re-derives to the same number."""
        provider = (
            seeder
            .version("v1", date(2026, 1, 1), [_candidate(value_minutes=30)])
            .version("v2", date(2026, 6, 1), [_candidate(value_minutes=44)])
            .build()
        )
        assert _resolve(provider, date(2026, 3, 2)).threshold_minutes == 30

    def test_a_datetime_as_of_is_reduced_to_its_date(self, seeder):
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=30)]).build()
        assert _resolve(
            provider, datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
        ).threshold_minutes == 30


# ═════════════════════════════════════════════════════════════════════════════
# U-X: one threshold per persisted workweek key, on real week boundaries
# ═════════════════════════════════════════════════════════════════════════════
class TestOneThresholdPerWorkweek:
    """Driven by the persisted W4 key, parsed — never by a recomputed week.

    ``workweek_key`` is the local workweek START date in ``YYYY-MM-DD``, so
    ``date.fromisoformat`` is the whole derivation and every day of a week
    shares one as-of date by construction.
    """

    #: 2026-08-31 is a Monday. Monday-start and Sunday-start organizations
    #: describe the same physical week with different keys — W4-3's example.
    MONDAY_START_KEY = "2026-08-31"
    SUNDAY_START_KEY = "2026-08-30"
    #: The British DST transition week. Date arithmetic is unaffected by a
    #: clock change, which is the point.
    DST_WEEK_KEY = "2026-10-25"

    @pytest.mark.parametrize(
        "workweek_key",
        [MONDAY_START_KEY, SUNDAY_START_KEY, DST_WEEK_KEY],
        ids=["monday-start", "sunday-start", "dst-week-2026-10-25"],
    )
    def test_all_seven_days_of_a_week_share_one_threshold(self, seeder, workweek_key):
        provider = seeder.version(
            "v1", date(2026, 1, 1), [_candidate(value_minutes=30)]).build()

        as_of = date.fromisoformat(workweek_key)
        assert as_of.isoformat() == workweek_key      # round-trips exactly

        # Every day of that week carries the SAME persisted key, so every day
        # resolves the same threshold from the same version.
        results = [_resolve(provider, as_of) for _ in range(7)]
        assert {r.threshold_minutes for r in results} == {30}
        assert {r.config_version_id for r in results} == {"v1"}
        assert {r.as_of for r in results} == {as_of}

    def test_adjacent_weeks_can_carry_different_thresholds(self, seeder):
        provider = (
            seeder
            .version("v1", date(2026, 8, 24), [_candidate(value_minutes=30)])
            .version("v2", date(2026, 8, 31), [_candidate(value_minutes=44)])
            .build()
        )
        assert _resolve(provider, date.fromisoformat("2026-08-24")).threshold_minutes == 30
        assert _resolve(provider, date.fromisoformat("2026-08-31")).threshold_minutes == 44

    def test_the_reader_never_derives_a_week_itself(self):
        """It must not read current schedule configuration, or the clock.

        Recomputing a week here would read CURRENT configuration and undo W4:
        a sealed day's week identity is what was persisted, not what would be
        computed now.
        """
        banned_calls = {"week_start_date", "isocalendar", "today", "utcnow", "now"}
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    assert name not in banned_calls, (
                        f"{path.name}:{node.lineno} calls {name}()"
                    )


# ═════════════════════════════════════════════════════════════════════════════
# Y-AA: no live fallback, and the typed-outcome invariant
# ═════════════════════════════════════════════════════════════════════════════
class TestFailClosed:
    @pytest.mark.parametrize("forbidden", [
        "app.settings",           # the live, mutate-in-place settings resolver
        "database.models",        # where AppSettings itself is declared
        "app.attendance_policy",  # the fail-open clock-out resolver (D-10)
        "app.schedule_policy",    # would recompute the week from current config
    ])
    def test_no_live_configuration_dependency(self, forbidden):
        """A live read here would defeat the historical floor entirely."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            offenders = {
                m for m in imported
                if m == forbidden or m.startswith(forbidden + ".")
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_forbidden_modules_actually_exist(self):
        """Guard the guard: a typo'd name can never be imported, so the test
        above would pass for the wrong reason."""
        import importlib.util
        for name in ("app.settings", "database.models",
                     "app.attendance_policy", "app.schedule_policy"):
            assert importlib.util.find_spec(name) is not None, name

    def test_the_reference_provider_is_database_free(self):
        """It must be usable by callers that should not need a database."""
        tree = ast.parse((PACKAGE / "reference.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("sqlalchemy", "database", "database.database"):
            assert not any(
                m == banned or m.startswith(banned + ".") for m in imported
            ), f"reference.py imports {banned}"

    @pytest.mark.parametrize("outcome", [
        o for o in ThresholdOutcome if o is not ThresholdOutcome.RESOLVED
    ])
    def test_an_unresolved_outcome_cannot_carry_a_value(self, outcome):
        with pytest.raises(ValueError):
            ThresholdResolution(
                outcome=outcome, organization_id=ORG,
                as_of=date(2026, 6, 1), threshold_minutes=30,
            )

    def test_resolved_requires_a_value(self):
        with pytest.raises(ValueError):
            ThresholdResolution(
                outcome=ThresholdOutcome.RESOLVED, organization_id=ORG,
                as_of=date(2026, 6, 1),
            )

    def test_every_unresolved_path_returns_no_number(self, seeder):
        """Whatever goes wrong, no caller can read a threshold out of it."""
        provider = seeder.version("v1", date(2026, 6, 1), [
            _candidate(scope_user_id=USER, value_minutes=30),
        ]).build()
        for as_of in (date(2026, 1, 1), date(2026, 7, 1)):
            result = _resolve(provider, as_of)
            assert not result.is_resolved
            assert result.threshold_minutes is None
            assert not result.is_evaluable

    def test_the_result_is_frozen(self):
        result = ThresholdResolution(
            outcome=ThresholdOutcome.RESOLVED, organization_id=ORG,
            as_of=date(2026, 6, 1), threshold_minutes=30,
        )
        with pytest.raises(Exception):
            result.threshold_minutes = 44          # type: ignore[misc]

    def test_no_forbidden_numeric_default_is_assigned_anywhere(self):
        """No initial value exists, by ruling. 30 is a GV scenario value.

        ``ECO_THRESHOLD_RANK`` is exempt and named explicitly rather than
        excluded by dropping 0 from the list: 0 is precisely one of the values
        that must never become a threshold, and the rank constant is 0 for an
        unrelated structural reason (``ConfigValue.rank`` is NOT NULL).
        """
        exempt = {"ECO_THRESHOLD_RANK"}
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    names = {t.id for t in targets if isinstance(t, ast.Name)}
                    if names & exempt:
                        continue
                    value = node.value
                    if (isinstance(value, ast.Constant)
                            and value.value in FORBIDDEN_DEFAULTS
                            and value.value is not False):
                        pytest.fail(
                            f"{path.name}:{node.lineno} assigns {value.value!r}, "
                            f"which could become a default threshold"
                        )

    def test_the_rank_exemption_is_the_only_one_and_is_still_zero(self):
        """Guard the guard: the exemption above must not hide a real default."""
        from app.eco_threshold import ECO_THRESHOLD_RANK
        assert ECO_THRESHOLD_RANK == 0

    def test_no_default_argument_supplies_a_threshold(self):
        """A default parameter value is the other way a default sneaks in."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for default in list(node.args.defaults) + list(
                            node.args.kw_defaults):
                        if (isinstance(default, ast.Constant)
                                and default.value in FORBIDDEN_DEFAULTS
                                and default.value is not False):
                            pytest.fail(
                                f"{path.name}:{node.lineno} defaults to "
                                f"{default.value!r}"
                            )


# ═════════════════════════════════════════════════════════════════════════════
# AB: the two implementations agree
# ═════════════════════════════════════════════════════════════════════════════
class TestProviderParity:
    """One configuration described once, resolved by both implementations.

    Built without the ``seeder`` fixture so both providers exist in one test and
    can be compared directly, rather than compared across two parametrized runs.
    """

    SCENARIOS = {
        "resolved": [_candidate(value_minutes=30)],
        "not-configured": [],
        "wrong-tier": [_candidate(tier="SHIFT", value_minutes=30)],
        "null-value": [_candidate(value_minutes=None)],
        "worker-scoped": [_candidate(scope_user_id=USER, value_minutes=30)],
        "ambiguous": [
            _candidate(value_minutes=30),
            _candidate(tier="SCHEDULE", value_minutes=600),
        ],
    }

    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    def test_both_providers_agree(self, db, scenario):
        candidates = self.SCENARIOS[scenario]

        memory = InMemoryThresholdProvider({ORG: [StoredThresholdVersion(
            config_version_id="v1", effective_from=date(2026, 1, 1),
            candidates=tuple(candidates),
        )]})

        version = ConfigVersion(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64,
        )
        db.add(version)
        db.flush()
        for c in candidates:
            db.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                scope_user_id=c.scope_user_id, tier=c.tier,
                rank=CANONICAL_RANK, key=c.key, value_minutes=c.value_minutes,
            ))
        db.flush()
        database = DatabaseThresholdProvider(db)

        as_of = date(2026, 6, 1)
        from_memory = _resolve(memory, as_of)
        from_database = _resolve(database, as_of)

        assert from_memory.outcome is from_database.outcome
        assert from_memory.threshold_minutes == from_database.threshold_minutes
        assert from_memory.defect == from_database.defect
        assert from_memory.config_version_id == from_database.config_version_id
        assert from_memory.effective_from == from_database.effective_from

    def test_both_providers_agree_below_the_floor(self, db):
        memory = InMemoryThresholdProvider({ORG: [StoredThresholdVersion(
            config_version_id="v1", effective_from=date(2026, 6, 1),
            candidates=(_candidate(value_minutes=30),),
        )]})
        db.add(ConfigVersion(
            organization_id=ORG, version_key="v1", effective_from=date(2026, 6, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64,
        ))
        db.flush()
        database = DatabaseThresholdProvider(db)

        for provider in (memory, database):
            assert _resolve(provider, date(2026, 5, 31)).outcome is (
                ThresholdOutcome.NO_CONFIG_VERSION)


# ═════════════════════════════════════════════════════════════════════════════
# Organization isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestOrganizationIsolation:
    def test_one_organizations_threshold_is_invisible_to_another(self, db):
        version = ConfigVersion(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64,
        )
        db.add(version)
        db.flush()
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG, scope_user_id=None,
            tier=CANONICAL_TIER, rank=CANONICAL_RANK,
            key=CANONICAL_KEY, value_minutes=30,
        ))
        db.flush()
        provider = DatabaseThresholdProvider(db)

        assert _resolve(provider, date(2026, 6, 1)).threshold_minutes == 30
        assert _resolve(
            provider, date(2026, 6, 1), organization_id=OTHER_ORG
        ).outcome is ThresholdOutcome.NO_CONFIG_VERSION


# ═════════════════════════════════════════════════════════════════════════════
# Storage-level guarantees, and immutability
# ═════════════════════════════════════════════════════════════════════════════
class TestStorage:
    def _version(self, db):
        row = ConfigVersion(
            organization_id=ORG, version_key="vX", effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64,
        )
        db.add(row)
        db.flush()
        return row

    def test_two_identical_canonical_rows_cannot_be_stored(self, db):
        """``uq_config_value_scope`` already makes the exactly-one guarantee for
        rows identical in (version, org scope, tier, key)."""
        version = self._version(db)
        for _ in range(2):
            db.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                scope_user_id=None, tier=CANONICAL_TIER, rank=CANONICAL_RANK,
                key=CANONICAL_KEY, value_minutes=30,
            ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    @pytest.mark.parametrize("minutes", [0, -1, MAX_MINUTES + 1])
    def test_out_of_range_values_are_also_refused_at_rest(self, db, minutes):
        version = self._version(db)
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG, scope_user_id=None,
            tier=CANONICAL_TIER, rank=CANONICAL_RANK,
            key=CANONICAL_KEY, value_minutes=minutes,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_a_stored_threshold_value_cannot_be_rewritten(self, db):
        from app.config_versioning import ImmutableConfigurationError

        version = self._version(db)
        value = ConfigValue(
            config_version_id=version.id, organization_id=ORG, scope_user_id=None,
            tier=CANONICAL_TIER, rank=CANONICAL_RANK,
            key=CANONICAL_KEY, value_minutes=30,
        )
        db.add(value)
        db.flush()
        value.value_minutes = 44
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_adapter_does_not_read_the_values_relationship(self):
        """``ConfigVersion.values`` returns every value in a version regardless
        of configuration domain — a third way past a key-filtered read."""
        tree = ast.parse((PACKAGE / "adapter.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "values":
                pytest.fail(f"adapter.py:{node.lineno} reads a .values attribute")


# ═════════════════════════════════════════════════════════════════════════════
# AC-AF: scope held — nothing wired, no migration, no vector claimed
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_the_package_is_not_wired_to_the_application(self):
        """Not imported by any route, service or scheduler."""
        foundation = (
            "eco_threshold", "eco_threshold_capture", "config_versioning",
            "config_first_capture",
            "requirement_resolution", "work_ledger", "leave_intervals",
            "worker_context", "rollout", "eval_day", "eval_week",
            # EVAL-EVENT is the package this reader's docstring names as the
            # owner of ``W >= T``. Consuming T here is the authorised
            # composition, not application wiring.
            "eval_event",
            # Both import AUTHORIZED_ROLES from the capture package, not
            # a threshold. They record ECO governance evidence; neither reads
            # T nor reaches a route, service or scheduler.
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
        """Guard the guard: a skip list swallowing everything would pass."""
        scanned = [
            p for p in (BACKEND / "app").rglob("*.py")
            if "eco_threshold" not in p.parts
        ]
        assert len(scanned) > 100

    def test_no_migration_was_added_for_the_reader(self):
        """The threshold VALUE is a row in tables that already exist.

        70, not 66: the capture increment added ``e5f8c2a91d64`` for durable
        actor attribution, which is provenance about who recorded a
        configuration decision — nothing the reader needs or reads. The count
        is still pinned, so a further revision cannot appear unremarked, and no
        revision may name the threshold key or tier: the value lives in
        configuration rows, never in DDL.
        """
        versions = BACKEND / "alembic" / "versions"
        # 74 since EVAL-EVENT added f3d8a1c65b29 (eco_threshold_event), after the D-18 correction's e7c2b9d4f815.
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list(versions.glob("*.py"))) == 94
        for path in versions.glob("*.py"):
            body = path.read_text(encoding="utf-8")
            assert CANONICAL_KEY not in body, f"{path.name} mentions the key"
            assert CANONICAL_TIER not in body, f"{path.name} mentions the tier"

    def test_no_threshold_table_was_created(self):
        for path in (BACKEND / "alembic" / "versions").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) == "create_table"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    assert "threshold" not in str(node.args[0].value).lower()

    def test_no_comparison_or_event_work_exists_in_this_package(self):
        """``W >= T`` and the crossing lifecycle belong to EVAL-EVENT.

        Scanned from the CODE — identifiers and runtime strings — not from the
        file text. The docstrings in this package name these concepts
        repeatedly in order to say they are absent, and a text scan would fail
        on the very documentation that records the boundary.
        """
        # Exact, case-sensitive tokens (plus a ``token_`` prefix, to catch
        # ``sequence_number`` and the like). Case-sensitive on purpose:
        # ``typing.Sequence`` is not an ECO event sequence, and a substring
        # match would conflate them.
        banned_identifiers = (
            "sequence", "crossed", "crossing", "withdraw", "withdrawn",
            "withdrawal", "notification", "notify", "weekly_total_minutes",
        )
        banned_names = ("ECO_THRESHOLD_REACHED", "ECO_THRESHOLD_WITHDRAWN")

        for path in sorted(PACKAGE.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for term in banned_identifiers:
                offenders = [
                    t for t in tokens
                    if t == term or t.startswith(term + "_")
                ]
                assert offenders == [], f"{path.name} uses {offenders}"
            for term in banned_names:
                offenders = [t for t in tokens if term in t]
                assert offenders == [], f"{path.name} uses {offenders}"

    def test_the_code_token_scanner_actually_sees_code(self):
        """Guard the guard: a scanner returning nothing would pass vacuously."""
        tokens = _code_tokens(
            ast.parse((PACKAGE / "reader.py").read_text(encoding="utf-8")))
        assert "resolve_threshold" in tokens
        assert "THRESHOLD_INVALID" in tokens or "ThresholdOutcome" in tokens
        assert len(tokens) > 30

    def test_the_golden_vectors_are_untouched_and_still_all_blocked(self):
        """No vector becomes executable from a threshold reader alone."""
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

    def test_the_terminology_guard_is_satisfied_by_this_package(self):
        """§31.1, applied to the new module and the canonical key."""
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        assert find_prohibited_terms(CANONICAL_KEY) == []
        assert find_prohibited_terms(CANONICAL_TIER) == []
        for path in sorted(PACKAGE.glob("*.py")):
            hits = scan_text(path.read_text(encoding="utf-8"))
            assert hits == [], format_hits(hits, source=path.name)
