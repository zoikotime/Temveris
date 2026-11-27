"""
STEP 6 — immutable, effective-dated configuration.
==================================================

Four things are tested here:

1. **The provider contract**, run against BOTH implementations. The in-memory
   reference provider is the executable specification; the database-backed
   adapter is required to reproduce it exactly. Adding the adapter to
   ``PROVIDER_IDS`` was the whole point of writing the contract first — the
   stored implementation is held to semantics that were settled before any
   schema existed, rather than to tests written afterwards to match whatever it
   happened to do.

2. **Storage-level guarantees** the reference provider cannot have: check
   constraints, expression-index uniqueness, the composite tenant foreign key,
   and ORM-level immutability.

3. **Migration/model agreement.** The migration's DDL is compared against the
   ORM models' actual definitions, so ``create_all`` (which the test suite uses)
   and Alembic (which a real database uses) cannot silently diverge.

4. **Migration safety** — parent revision, single head, additive-only, no
   backfill, no production execution logic.

Expected values here are written out independently. Where a test needs to know
what the migration should say, it says so literally rather than importing the
constant the implementation used, so a wrong constant cannot agree with itself.

ON FOREIGN-KEY ENFORCEMENT: the shared test engine does not set
``PRAGMA foreign_keys=ON``, so SQLite does not enforce foreign keys there.
Rather than assert an enforcement that would not actually run, the composite
tenant FK is exercised on a dedicated engine with the pragma enabled — see
``TestCompositeTenantForeignKey``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config_versioning import (
    ConfigValue,
    ConfigVersion,
    DatabaseConfigVersionProvider,
    EffectiveDateCollision,
    ImmutableConfigurationError,
    ImmutableVersionError,
    InMemoryConfigVersionProvider,
    compute_content_hash,
)
from app.requirement_resolution import (
    ConfigVersionProvider,
    ConfigVersionRef,
    RequirementCandidate,
    ResolutionOutcome,
    resolve_requirement_strict,
)
from app.work_ledger import WorkDayLedger, WorkStateInterval

ORG = 11
OTHER_ORG = 12
USER = 501

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "config_versioning"
MIGRATION = (
    BACKEND / "alembic" / "versions"
    / "d4b7e2f9a615_phase2_foundation_ledger_and_config.py"
)

#: Written literally, not imported: this is the fact the migration must state.
EXPECTED_PARENT_REVISION = "e4f7a2b91c60"
EXPECTED_REVISION = "d4b7e2f9a615"


@pytest.fixture(scope="module", autouse=True)
def _step6_schema(engine):
    """Materialise the four Phase-2 tables plus their FK targets.

    conftest builds the test schema per-model rather than via
    ``Base.metadata.create_all``, so new tables opt in explicitly.
    """
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)


# ── The provider matrix. One suite, two implementations. ─────────────────────
PROVIDER_IDS = ["in-memory-reference", "database-backed"]


@pytest.fixture(params=PROVIDER_IDS)
def provider(request, db):
    if request.param == "in-memory-reference":
        return InMemoryConfigVersionProvider()
    return DatabaseConfigVersionProvider(db)


def _cand(tier, rank, minutes=None, source_ref=None):
    return RequirementCandidate(
        tier=tier, rank=rank, minutes=minutes, source_ref=source_ref
    )


# ═════════════════════════════════════════════════════════════════════════════
# The port contract
# ═════════════════════════════════════════════════════════════════════════════
class TestPortConformance:
    def test_provider_satisfies_the_step5_port(self, provider):
        assert isinstance(provider, ConfigVersionProvider)

    def test_resolver_accepts_it_without_adaptation(self, provider):
        """The point of the seam: Step 5's resolver runs unmodified."""
        provider.capture_version(
            organization_id=ORG,
            config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 480
        assert result.config_version_id == "v1"

    def test_precedence_is_the_resolvers_not_the_stores(self, provider):
        """Lowest rank wins, and the store must not reorder or pre-filter."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[
                _cand("TENANT_FALLBACK", rank=6, minutes=480),
                _cand("WORKER_OVERRIDE", rank=1, minutes=300),
                _cand("SHIFT", rank=3, minutes=420),
            ],
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.required_minutes == 300
        assert result.source_tier == "WORKER_OVERRIDE"

    def test_specification_tier_labels_are_stored_verbatim(self, provider):
        """Owner ruling: §10.1 labels are carried through unchanged.

        Not renamed to ORG_*, no substitute tokens invented.
        """
        labels = [
            "WORKER_OVERRIDE", "SHIFT", "SCHEDULE",
            "EMPLOYMENT_POLICY", "TENANT_TYPE_DEFAULT", "TENANT_FALLBACK",
        ]
        ref = provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand(t, rank=i + 1) for i, t in enumerate(labels)],
        )
        stored = provider.requirement_candidates(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), version=ref,
        )
        assert sorted(c.tier for c in stored) == sorted(labels)


# ═════════════════════════════════════════════════════════════════════════════
# Effective dating
# ═════════════════════════════════════════════════════════════════════════════
class TestEffectiveDating:
    def test_lower_bound_is_inclusive(self, provider):
        """A version effective on the 1st governs the 1st, not the 2nd."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 3, 1),
        )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 3, 1)
        ).config_version_id == "v1"

    def test_the_day_before_resolves_to_the_previous_version(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
        )
        provider.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 3, 1),
        )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 2, 28)
        ).config_version_id == "v1"
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 3, 1)
        ).config_version_id == "v2"

    def test_latest_applicable_version_wins_not_the_first(self, provider):
        for n, month in ((1, 1), (2, 3), (3, 6)):
            provider.capture_version(
                organization_id=ORG, config_version_id=f"v{n}",
                effective_from=date(2026, month, 1),
            )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 12, 31)
        ).config_version_id == "v3"

    def test_capture_order_cannot_change_the_answer(self, provider, db):
        """INV-8: determinism must not depend on the order versions arrived."""
        rows = [
            ("v1", date(2026, 1, 1)),
            ("v2", date(2026, 3, 1)),
            ("v3", date(2026, 6, 1)),
        ]
        for vid, eff in reversed(rows):
            provider.capture_version(
                organization_id=ORG, config_version_id=vid, effective_from=eff
            )
        # Expected answers derived by hand, not by a second provider.
        expected = {
            date(2026, 1, 1): "v1",
            date(2026, 2, 28): "v1",
            date(2026, 3, 1): "v2",
            date(2026, 5, 31): "v2",
            date(2026, 6, 1): "v3",
            date(2026, 12, 31): "v3",
        }
        for probe, want in expected.items():
            got = provider.version_as_of(organization_id=ORG, as_of=probe)
            assert got.config_version_id == want, f"wrong version at {probe}"

    def test_datetime_as_of_is_accepted_and_reduced_to_its_date(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 3, 1),
        )
        as_dt = datetime(2026, 3, 1, 23, 59, 59, tzinfo=timezone.utc)
        assert provider.version_as_of(
            organization_id=ORG, as_of=as_dt
        ).config_version_id == "v1"


# ═════════════════════════════════════════════════════════════════════════════
# The historical floor — the reason this layer exists
# ═════════════════════════════════════════════════════════════════════════════
class TestHistoricalFloor:
    def test_date_below_the_floor_returns_none(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 3, 1),
        )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 2, 28)
        ) is None

    def test_no_versions_at_all_returns_none(self, provider):
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 6, 1)
        ) is None

    def test_below_the_floor_the_resolver_refuses_to_evaluate(self, provider):
        """Not a default, not zero, not live settings — NO_VERSION (§10.2)."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 3, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 2, 28), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.NO_VERSION
        assert result.required_minutes is None
        assert not result.is_evaluable

    def test_no_version_is_distinguishable_from_captured_but_unresolved(
        self, provider
    ):
        """Two different failures that must never collapse into one.

        NO_VERSION means "nobody captured configuration covering this date".
        REQUIREMENT_UNRESOLVED means "configuration exists and answered
        nothing". They call for different operational responses, so they are
        different outcomes rather than one generic failure.
        """
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 3, 1),
        )
        below = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 1, 1), provider=provider,
        )
        above = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert below.outcome is ResolutionOutcome.NO_VERSION
        assert above.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
        assert below.outcome is not above.outcome
        assert below.config_version_id is None
        assert above.config_version_id == "v1"

    def test_adding_an_earlier_version_later_lowers_the_floor(self, provider):
        """Backfill of a version is legitimate; rewriting one is not."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 3, 1),
        )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 1, 15)
        ) is None

        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
        )
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 1, 15)
        ).config_version_id == "v1"
        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 3, 1)
        ).config_version_id == "v2"


# ═════════════════════════════════════════════════════════════════════════════
# Immutability (INV-3, §30.1)
# ═════════════════════════════════════════════════════════════════════════════
class TestImmutability:
    def test_reusing_a_version_id_is_refused(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
        )
        with pytest.raises(ImmutableVersionError):
            provider.capture_version(
                organization_id=ORG, config_version_id="v1",
                effective_from=date(2026, 5, 1),
            )

    def test_two_versions_cannot_share_an_effective_date(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
        )
        with pytest.raises(EffectiveDateCollision):
            provider.capture_version(
                organization_id=ORG, config_version_id="v2",
                effective_from=date(2026, 1, 1),
            )

    def test_a_refused_capture_leaves_the_store_unchanged(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        with pytest.raises(EffectiveDateCollision):
            provider.capture_version(
                organization_id=ORG, config_version_id="v2",
                effective_from=date(2026, 1, 1),
                candidates=[_cand("SHIFT", rank=2, minutes=999)],
            )
        assert provider.version_count(organization_id=ORG) == 1
        ref = provider.version_as_of(organization_id=ORG, as_of=date(2026, 6, 1))
        assert ref.config_version_id == "v1"
        rows = provider.requirement_candidates(
            organization_id=ORG, user_id=USER, as_of=date(2026, 6, 1), version=ref
        )
        assert [r.minutes for r in rows] == [480]

    def test_a_later_correction_does_not_move_an_earlier_answer(self, provider):
        """The §30.1 guarantee, stated as a test.

        A completed day resolved against v1 must still resolve against v1 after
        v2 is published — otherwise a historical figure moves under the Worker.
        """
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        completed_day = date(2026, 2, 10)
        before = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=completed_day, provider=provider,
        )

        provider.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 4, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=420)],
        )
        after = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=completed_day, provider=provider,
        )

        assert before.required_minutes == after.required_minutes == 480
        assert before.config_version_id == after.config_version_id == "v1"


# ═════════════════════════════════════════════════════════════════════════════
# Candidates are frozen into ONE version, never blended
# ═════════════════════════════════════════════════════════════════════════════
class TestCandidateBinding:
    def test_candidates_come_from_the_named_version_only(self, provider):
        v1 = provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        v2 = provider.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 6, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=420)],
        )
        common = dict(organization_id=ORG, user_id=USER, as_of=date(2026, 8, 1))
        assert [c.minutes for c in provider.requirement_candidates(version=v1, **common)] == [480]
        assert [c.minutes for c in provider.requirement_candidates(version=v2, **common)] == [420]

    def test_a_per_user_set_replaces_the_org_default_set(self, provider):
        ref = provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
            candidates_by_user={USER: [_cand("WORKER_OVERRIDE", rank=1, minutes=360)]},
        )
        mine = provider.requirement_candidates(
            organization_id=ORG, user_id=USER, as_of=date(2026, 6, 1), version=ref
        )
        theirs = provider.requirement_candidates(
            organization_id=ORG, user_id=USER + 1, as_of=date(2026, 6, 1), version=ref
        )
        assert [c.minutes for c in mine] == [360]
        assert [c.minutes for c in theirs] == [480]

    def test_an_unknown_version_yields_no_candidates(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        ghost = ConfigVersionRef(config_version_id="nope", effective_from=date(2026, 1, 1))
        assert provider.requirement_candidates(
            organization_id=ORG, user_id=USER, as_of=date(2026, 6, 1), version=ghost
        ) == ()

    def test_a_version_with_no_candidates_is_unresolved_not_defaulted(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
        assert result.required_minutes is None
        assert result.requires_administrator_alert

    def test_a_silent_tier_defers_to_the_next_rank(self, provider):
        """NULL minutes means "this tier did not answer", not "zero"."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[
                _cand("WORKER_OVERRIDE", rank=1, minutes=None),
                _cand("SHIFT", rank=2, minutes=420),
            ],
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.required_minutes == 420
        assert result.source_tier == "SHIFT"


# ═════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestOrganizationIsolation:
    def test_one_organizations_versions_are_invisible_to_another(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        assert provider.version_as_of(
            organization_id=OTHER_ORG, as_of=date(2026, 6, 1)
        ) is None

    def test_the_same_version_id_may_exist_in_two_organizations(self, provider):
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        provider.capture_version(
            organization_id=OTHER_ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=420)],
        )
        a = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        b = resolve_requirement_strict(
            organization_id=OTHER_ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert (a.required_minutes, b.required_minutes) == (480, 420)

    def test_candidates_cannot_be_read_across_organizations(self, provider):
        ref = provider.capture_version(
            organization_id=ORG, config_version_id="shared-key",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        assert provider.requirement_candidates(
            organization_id=OTHER_ORG, user_id=USER,
            as_of=date(2026, 6, 1), version=ref,
        ) == ()


# ═════════════════════════════════════════════════════════════════════════════
# Content hash — INV-8
# ═════════════════════════════════════════════════════════════════════════════
class TestContentHash:
    @staticmethod
    def _independent_hash(organization_id, version_key, effective_from, values):
        """Recompute the digest from an independently written canonical form.

        Deliberately does NOT call ``compute_content_hash``: a test that used
        the implementation to produce its own expected value would agree with
        any bug the implementation contained.
        """
        rows = sorted(
            (
                {
                    "key": v["key"],
                    "rank": int(v["rank"]),
                    "scope_user_id": v.get("scope_user_id"),
                    "source_ref": v.get("source_ref"),
                    "tier": v["tier"],
                    "value_minutes": v.get("value_minutes"),
                    # Owner ruling R6 (GV-27): the classification dimension is
                    # inside the digest, serialised as null when the row is not
                    # classification-scoped. Kept in this independent form too —
                    # the point of the reimplementation is that it has to be
                    # updated deliberately, not inherit the change for free.
                    "worker_classification": v.get("worker_classification"),
                }
                for v in values
            ),
            key=lambda v: (
                -1 if v["scope_user_id"] is None else v["scope_user_id"],
                v["worker_classification"] or "",
                v["tier"], v["rank"], v["key"],
            ),
        )
        blob = json.dumps(
            {
                "effective_from": effective_from.isoformat(),
                "organization_id": int(organization_id),
                "values": rows,
                "version_key": version_key,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    VALUES = [
        {"scope_user_id": None, "tier": "SHIFT", "rank": 2, "key": "required_minutes",
         "value_minutes": 480, "source_ref": "shift:7"},
        {"scope_user_id": 501, "tier": "WORKER_OVERRIDE", "rank": 1,
         "key": "required_minutes", "value_minutes": 360, "source_ref": None},
    ]

    def test_matches_an_independently_computed_digest(self):
        assert compute_content_hash(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1), values=self.VALUES,
        ) == self._independent_hash(ORG, "v1", date(2026, 1, 1), self.VALUES)

    def test_is_stable_across_repeated_computation(self):
        args = dict(organization_id=ORG, version_key="v1",
                    effective_from=date(2026, 1, 1), values=self.VALUES)
        assert compute_content_hash(**args) == compute_content_hash(**args)

    def test_is_independent_of_value_order(self):
        forward = compute_content_hash(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1), values=self.VALUES,
        )
        backward = compute_content_hash(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1), values=list(reversed(self.VALUES)),
        )
        assert forward == backward

    @pytest.mark.parametrize("field,changed", [
        ("value_minutes", 481),
        ("rank", 9),
        ("tier", "SCHEDULE"),
        ("source_ref", "shift:8"),
    ])
    def test_changing_any_value_field_changes_the_digest(self, field, changed):
        mutated = [dict(self.VALUES[0]), dict(self.VALUES[1])]
        mutated[0][field] = changed
        assert compute_content_hash(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1), values=self.VALUES,
        ) != compute_content_hash(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1), values=mutated,
        )

    @pytest.mark.parametrize("kwargs", [
        {"organization_id": OTHER_ORG},
        {"version_key": "v2"},
        {"effective_from": date(2026, 1, 2)},
    ])
    def test_identity_is_bound_into_the_digest(self, kwargs):
        """A value set cannot be moved to another version or org and verify."""
        base = dict(organization_id=ORG, version_key="v1",
                    effective_from=date(2026, 1, 1), values=self.VALUES)
        assert compute_content_hash(**base) != compute_content_hash(**{**base, **kwargs})

    def test_the_adapter_stores_the_digest_of_what_it_wrote(self, db):
        adapter = DatabaseConfigVersionProvider(db)
        adapter.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480, source_ref="shift:7")],
            candidates_by_user={USER: [_cand("WORKER_OVERRIDE", rank=1, minutes=360)]},
        )
        row = db.execute(
            sa.select(ConfigVersion).where(ConfigVersion.version_key == "v1")
        ).scalars().one()
        stored_values = db.execute(
            sa.select(ConfigValue).where(ConfigValue.config_version_id == row.id)
        ).scalars().all()

        assert row.content_hash == self._independent_hash(
            ORG, "v1", date(2026, 1, 1),
            [
                {
                    "scope_user_id": v.scope_user_id, "tier": v.tier,
                    "rank": v.rank, "key": v.key,
                    "value_minutes": v.value_minutes, "source_ref": v.source_ref,
                    "worker_classification": v.worker_classification,
                }
                for v in stored_values
            ],
        )


# ═════════════════════════════════════════════════════════════════════════════
# Storage-level guarantees the reference provider cannot have
# ═════════════════════════════════════════════════════════════════════════════
class TestStorageConstraints:
    def _version(self, db, **overrides):
        row = ConfigVersion(
            organization_id=ORG, version_key="vX",
            effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="0" * 64,
        )
        for k, v in overrides.items():
            setattr(row, k, v)
        db.add(row)
        db.flush()
        return row

    @pytest.mark.parametrize("minutes", [0, -1, 1441, 100000])
    def test_out_of_range_minutes_are_refused_by_the_database(self, db, minutes):
        """The Step 5 bounds, enforced at rest rather than only at read time."""
        version = self._version(db)
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=minutes,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    @pytest.mark.parametrize("minutes", [1, 480, 1440, None])
    def test_in_range_and_null_minutes_are_accepted(self, db, minutes):
        version = self._version(db)
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=minutes,
        ))
        db.flush()

    def test_negative_rank_is_refused(self, db):
        version = self._version(db)
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="SHIFT", rank=-1, key="required_minutes", value_minutes=480,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_empty_tier_is_refused(self, db):
        version = self._version(db)
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="", rank=2, key="required_minutes", value_minutes=480,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_empty_version_key_is_refused(self, db):
        with pytest.raises(IntegrityError):
            self._version(db, version_key="")
        db.rollback()

    def test_two_versions_cannot_share_an_effective_date_at_the_database(self, db):
        """The constraint, not the adapter's pre-check, doing the work."""
        self._version(db, version_key="a", effective_from=date(2026, 1, 1))
        with pytest.raises(IntegrityError):
            self._version(db, version_key="b", effective_from=date(2026, 1, 1))
        db.rollback()

    def test_two_org_wide_rows_for_one_tier_and_key_collide(self, db):
        """The NULL-scope case a plain UNIQUE misses on BOTH backends.

        NULL never equals NULL in a unique index, so a declarative
        UniqueConstraint over (version, scope_user_id, tier, key) would let two
        organization-wide rows through on PostgreSQL and on SQLite alike. Only
        the COALESCE expression index catches it — which is why that index is
        declared on the model as well as in the migration, rather than the
        migration being the sole authority.
        """
        version = self._version(db)
        for _ in range(2):
            db.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                scope_user_id=None, tier="SHIFT", rank=2,
                key="required_minutes", value_minutes=480,
            ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_the_same_tier_may_be_scoped_to_different_users(self, db):
        version = self._version(db)
        for uid in (USER, USER + 1):
            db.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                scope_user_id=uid, tier="SHIFT", rank=2,
                key="required_minutes", value_minutes=480,
            ))
        db.flush()


class TestOrmImmutability:
    """Owner-approved ORM-level enforcement. No database trigger at this stage.

    The documented limitation is unchanged: a Core-level bulk UPDATE that
    bypasses the ORM is not intercepted here.
    """

    def _stored(self, db):
        row = ConfigVersion(
            organization_id=ORG, version_key="frozen",
            effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="a" * 64,
        )
        db.add(row)
        db.flush()
        return row

    @pytest.mark.parametrize("field,value", [
        ("effective_from", date(2026, 5, 1)),
        ("version_key", "renamed"),
        ("organization_id", OTHER_ORG),
        ("content_hash", "b" * 64),
    ])
    def test_a_captured_version_cannot_be_rewritten(self, db, field, value):
        row = self._stored(db)
        setattr(row, field, value)
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_a_captured_version_cannot_be_deleted(self, db):
        row = self._stored(db)
        db.delete(row)
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_a_stored_value_cannot_be_rewritten(self, db):
        version = self._stored(db)
        value = ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=480,
        )
        db.add(value)
        db.flush()

        value.value_minutes = 420
        with pytest.raises(ImmutableConfigurationError):
            db.flush()
        db.rollback()

    def test_the_note_is_the_one_field_that_is_not_frozen(self, db):
        """Deliberate: ``note`` is administrator prose, not configuration.

        Nothing resolves against it and it is outside the content hash, so
        correcting a typo cannot change what any date resolves to.
        """
        row = self._stored(db)
        row.note = "corrected wording"
        db.flush()
        assert row.note == "corrected wording"


class TestCompositeTenantForeignKey:
    """The tenant boundary, exercised where foreign keys are actually enforced.

    The shared test engine leaves ``PRAGMA foreign_keys`` off, so this builds a
    dedicated engine with enforcement on rather than asserting a rule that would
    not run.
    """

    @pytest.fixture()
    def fk_session(self):
        from database.database import Base  # noqa: F401  (metadata registration)
        from database.models import Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        ConfigVersion.__table__.create(eng, checkfirst=True)
        ConfigValue.__table__.create(eng, checkfirst=True)

        with Session(eng) as s:
            s.add(Organization(id=ORG, name="Org A"))
            s.add(Organization(id=OTHER_ORG, name="Org B"))
            s.commit()
            yield s

    def test_foreign_key_enforcement_is_actually_on(self, fk_session):
        """Guard the guard: without the pragma every test below passes vacuously."""
        enabled = fk_session.execute(sa.text("PRAGMA foreign_keys")).scalar()
        assert enabled == 1

    def test_a_value_cannot_be_attached_to_another_organizations_version(
        self, fk_session
    ):
        version = ConfigVersion(
            organization_id=ORG, version_key="v1",
            effective_from=date(2026, 1, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="c" * 64,
        )
        fk_session.add(version)
        fk_session.flush()

        # Same version, but claiming the OTHER organization: the composite FK
        # has no matching (id, organization_id) pair to point at.
        fk_session.add(ConfigValue(
            config_version_id=version.id, organization_id=OTHER_ORG,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=480,
        ))
        with pytest.raises(IntegrityError):
            fk_session.flush()
        fk_session.rollback()

    def test_a_value_with_the_matching_organization_is_accepted(self, fk_session):
        version = ConfigVersion(
            organization_id=ORG, version_key="v2",
            effective_from=date(2026, 2, 1),
            captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="d" * 64,
        )
        fk_session.add(version)
        fk_session.flush()
        fk_session.add(ConfigValue(
            config_version_id=version.id, organization_id=ORG,
            tier="SHIFT", rank=2, key="required_minutes", value_minutes=480,
        ))
        fk_session.flush()


# ═════════════════════════════════════════════════════════════════════════════
# Migration ↔ model agreement
# ═════════════════════════════════════════════════════════════════════════════
def _migration_tree() -> ast.Module:
    return ast.parse(MIGRATION.read_text(encoding="utf-8"))


def _create_table_calls(tree: ast.Module) -> dict:
    """``{table_name: [column names...]}`` read from the migration's DDL."""
    tables = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"):
            continue
        table = node.args[0].value
        columns = [
            arg.args[0].value
            for arg in node.args[1:]
            if isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "Column"
        ]
        tables[table] = columns
    return tables


def _named_constraints(tree: ast.Module, table: str) -> set:
    """Constraint/index names the migration declares for one table."""
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"
                and node.args[0].value == table):
            continue
        for kw in ast.walk(node):
            if isinstance(kw, ast.keyword) and kw.arg == "name":
                if isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
    return names


class TestMigrationMatchesModels:
    """``create_all`` (tests) and Alembic (real databases) must agree.

    Comparing the migration's DDL against the ORM models is the only thing that
    catches the failure mode where the two drift and every test still passes
    because the tests never run the migration.
    """

    #: Columns added to a Step 6 table by a LATER revision.
    #:
    #: This revision's own DDL is frozen — it shipped, and editing it would let
    #: an applied database and a fresh one diverge. So the model legitimately
    #: carries columns this file never mentions, and the check below accounts
    #: for them BY NAME rather than being relaxed: an unexplained extra column
    #: still fails, and the revision that introduces each one is asserted
    #: separately in ``test_eco_threshold_capture.py``.
    LATER_REVISION_COLUMNS = {
        "config_version": {"captured_by_label": "e5f8c2a91d64"},
        # Owner ruling R1/R4 (GV-27): the Worker-classification dimension, so
        # one version can carry both of a tier's per-worker-type answers.
        # Nullable, and NULL keeps the pre-existing "applies to everyone"
        # meaning, so no row captured before it needed changing.
        "config_value": {"worker_classification": "a9d3f6b21c58"},
        # Owner decision D-18D: the working-day result, frozen at seal so a
        # later roster edit cannot change what a sealed day was measured
        # against. Added by the producer revision, not by Step 6's own DDL.
        "work_day_ledger": {
            "is_working_day": "d5f1a8c3b724",
            "working_day_source": "d5f1a8c3b724",
            # Owner rulings D18-F2 / D18-F3-S: the day's verified departure and
            # the SOURCE-evidence digest, both frozen at seal and both added by
            # the D-18 correction revision rather than by Step 6's own DDL.
            "departure_at": "e7c2b9d4f815",
            "departure_provenance": "e7c2b9d4f815",
            "departure_source_ref": "e7c2b9d4f815",
            "source_fingerprint": "e7c2b9d4f815",
            # R-12.3.4 / §35: the shift-attribution rule in force when this
            # day's work_date was chosen, frozen on the row so a replay never
            # has to assume the rule the code uses today. MODEL-WORKWEEK.
            "shift_attribution_rule": "b2e6d9f04a17",
            # Owner rulings D-1.1 / D-2.1: §18 step 14's second operand — the
            # scheduled end and the configuration tier that produced it, taken
            # from the same resolution as is_working_day above and frozen for
            # the same reason. Both nullable: a row sealed before this revision
            # has no scheduled end and none may be invented, and a tenant with
            # no configured shift end records a "system" tier with a NULL time.
            "scheduled_end_local": "f4a7c2b91e68",
            "scheduled_end_source": "f4a7c2b91e68",
        },
    }

    @pytest.mark.parametrize("model", [
        WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
    ])
    def test_migration_columns_match_the_model(self, model):
        declared = _create_table_calls(_migration_tree())
        table = model.__tablename__
        assert table in declared, f"migration does not create {table}"
        expected = set(declared[table]) | set(
            self.LATER_REVISION_COLUMNS.get(table, {})
        )
        assert expected == {c.name for c in model.__table__.columns}, (
            f"{table}: migration DDL and ORM model disagree on columns"
        )

    #: Named constraints a LATER revision adds to a Step 6 table. Accounted
    #: for BY NAME, exactly like LATER_REVISION_COLUMNS: an unexplained extra
    #: constraint still fails, and the revision that introduces each one is
    #: asserted in tests/test_work_ledger_producer.py.
    LATER_REVISION_CONSTRAINTS = {
        "work_day_ledger": {
            "ck_work_day_ledger_departure_all_or_none": "e7c2b9d4f815",
            "ck_work_day_ledger_departure_verified": "e7c2b9d4f815",
        },
        # Owner rulings R4 and R2 (GV-27): the two scopes are mutually
        # exclusive, and a classification-scoped row carries one of exactly two
        # authorized values. Added by the classification revision; PostgreSQL
        # only there, because a SQLite database gets both from ``create_all``.
        "config_value": {
            "ck_config_value_scope_exclusive": "a9d3f6b21c58",
            "ck_config_value_classification_vocabulary": "a9d3f6b21c58",
        },
    }

    @pytest.mark.parametrize("model", [
        WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
    ])
    def test_migration_declares_the_models_named_constraints(self, model):
        """Every uq_/ck_/fk_ name on the model appears in the migration."""
        wanted = {
            c.name for c in model.__table__.constraints
            if c.name and str(c.name).split("_")[0] in {"uq", "ck", "fk"}
        } - set(self.LATER_REVISION_CONSTRAINTS.get(model.__tablename__, {}))
        assert wanted, f"no named constraints found on {model.__tablename__}"
        declared = _named_constraints(_migration_tree(), model.__tablename__)
        assert wanted <= declared, (
            f"{model.__tablename__}: migration is missing "
            f"{sorted(wanted - declared)}"
        )

    def test_the_parser_actually_found_all_four_tables(self):
        """Guard the guard: an empty parse would make the checks above vacuous."""
        assert set(_create_table_calls(_migration_tree())) == {
            "work_day_ledger", "work_state_interval",
            "config_version", "config_value",
        }

    def test_the_expression_index_is_created_for_null_scope_uniqueness(self):
        body = MIGRATION.read_text(encoding="utf-8")
        assert "COALESCE(scope_user_id, 0)" in body
        assert "uq_config_value_scope" in body


# ═════════════════════════════════════════════════════════════════════════════
# Migration safety
# ═════════════════════════════════════════════════════════════════════════════
def _module_constant(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {MIGRATION.name}")


def _function_string_literals(tree: ast.Module) -> list:
    """String constants inside function bodies — excludes module documentation."""
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    out.append(inner.value)
    return out


def _called_attributes(tree: ast.Module) -> set:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


class TestMigrationSafety:
    def test_the_migration_file_exists(self):
        assert MIGRATION.is_file(), f"missing {MIGRATION.name}"

    def test_revision_and_parent_are_exactly_as_authorised(self):
        tree = _migration_tree()
        assert _module_constant(tree, "revision") == EXPECTED_REVISION
        assert _module_constant(tree, "down_revision") == EXPECTED_PARENT_REVISION

    def test_it_is_a_single_parent_revision_not_a_merge(self):
        parent = _module_constant(_migration_tree(), "down_revision")
        assert isinstance(parent, str), (
            "down_revision must be a single string; a tuple would be a merge "
            "revision, which was not authorised"
        )

    def test_the_graph_has_exactly_one_head_and_one_root(self):
        """Alembic's own resolver, reading scripts only — no env.py, no DB.

        Asserts the INVARIANT, not a snapshot. This revision was the sole head
        when it was written; later steps chain onto it, which is the normal case
        and must not fail a test. What must never change is that the graph does
        not fork — that is the condition a parser error once falsely reported.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)

        assert len(sd.get_heads()) == 1, f"the graph forked: {sd.get_heads()}"
        assert len(sd.get_bases()) == 1, f"unexpected extra root: {sd.get_bases()}"

    def test_this_revision_is_still_in_the_graph(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert sd.get_revision(EXPECTED_REVISION) is not None

    def test_exactly_one_revision_claims_that_parent(self):
        """A second child of e4f7a2b91c60 would fork the graph."""
        versions = BACKEND / "alembic" / "versions"
        children = []
        for path in versions.glob("*.py"):
            try:
                parent = _module_constant(ast.parse(
                    path.read_text(encoding="utf-8")), "down_revision")
            except AssertionError:
                continue
            if parent == EXPECTED_PARENT_REVISION:
                children.append(path.name)
        assert children == [MIGRATION.name], f"multiple children: {children}"

    def test_the_migration_is_additive_only(self):
        """No ALTER, no DROP, no RENAME of anything that already exists."""
        forbidden = {
            "alter_column", "drop_column", "add_column",
            "rename_table", "drop_constraint", "drop_index",
        }
        called = _called_attributes(_migration_tree())
        assert not (called & forbidden), (
            f"non-additive operations present: {sorted(called & forbidden)}"
        )

    def test_the_migration_contains_no_data_backfill(self):
        """Fabricating configuration history or ledger intervals is forbidden."""
        called = _called_attributes(_migration_tree())
        assert "bulk_insert" not in called

        for literal in _function_string_literals(_migration_tree()):
            upper = " ".join(literal.upper().split())
            for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "SELECT "):
                assert verb not in upper, (
                    f"data-manipulation SQL in the migration: {literal!r}"
                )

    def test_the_migration_opens_no_connection_of_its_own(self):
        """It must use Alembic's bind, never construct or configure one."""
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv"):
            assert banned not in body, f"{banned!r} must not appear in a migration"

    def test_only_the_four_authorised_tables_are_created(self):
        assert set(_create_table_calls(_migration_tree())) == {
            "work_day_ledger", "work_state_interval",
            "config_version", "config_value",
        }

    def test_each_table_creation_is_guarded_for_already_provisioned_databases(self):
        """The create_all/Alembic overlap the repository already handles."""
        body = MIGRATION.read_text(encoding="utf-8")
        for table in ("work_day_ledger", "work_state_interval",
                      "config_version", "config_value"):
            assert f'if "{table}" not in existing:' in body, (
                f"{table} creation is not guarded against a pre-existing table"
            )

    def test_downgrade_drops_only_what_upgrade_creates(self):
        tree = _migration_tree()
        dropped = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == "drop_table"):
                        dropped.add(inner.args[0].value)
        assert dropped == set(_create_table_calls(tree))


# ═════════════════════════════════════════════════════════════════════════════
# The migration, actually executed — offline, against a throwaway SQLite file
# ═════════════════════════════════════════════════════════════════════════════
#: Revisions after ``d4b7e2f9a615`` that alter a Step 6 table, in graph order.
#: Applied by ``migrated_schema`` so the executed schema keeps matching the ORM
#: models. Each is a file name under ``alembic/versions``.
LATER_STEP6_REVISIONS = (
    "e5f8c2a91d64_config_version_actor_label.py",
    # D-18C/D-18D. Included so the executed-schema parity tests below compare
    # the ORM model against the FULL migration chain rather than against a
    # snapshot that predates the producer.
    "d5f1a8c3b724_producer_cutover_and_working_day.py",
    # The D-18 correction: append-only cutover history, day-level verified
    # departure, source-evidence fingerprint.
    "e7c2b9d4f815_d18_correction_append_only_cutover.py",
    # MODEL-WORKWEEK: the shift-attribution rule the day was attributed under,
    # required on the day record by R-12.3.4 and §35.
    "b2e6d9f04a17_ledger_shift_attribution_rule.py",
    # GV-17 / §24.7: D18-F2's departure CHECK widened to admit synthesized
    # provenance, so an unverified exit can be recorded and deferred.
    "c3f7a1e58d2b_admissible_departure_provenance.py",
    # GV-27 / owner rulings R1/R4/R5: the Worker-classification dimension on
    # config_value, and uq_config_value_scope rebuilt around it so one version
    # can hold a per-worker-type answer for each classification.
    "a9d3f6b21c58_config_value_worker_classification.py",
    # GV-01 / owner rulings D-1.1 and D-2.1: §18 step 14's second operand —
    # the scheduled end and its configuration tier, frozen on the ledger at
    # seal beside the working-day result d5f1a8c3b724 added above.
    "f4a7c2b91e68_ledger_scheduled_end_evidence.py",
)


def _load_revision_module(filename: str):
    """Import one revision file directly, without Alembic's environment.

    ``env.py`` is never loaded and the configured DATABASE_URL is never read:
    this imports the module and drives its ``upgrade()`` against a connection
    the test itself created.
    """
    import importlib.util

    path = BACKEND / "alembic" / "versions" / filename
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_module():
    """The Step 6 revision itself."""
    return _load_revision_module(MIGRATION.name)


@pytest.fixture(scope="module")
def migrated_schema():
    """Apply the revision chain to a private in-memory SQLite database.

    Reading DDL proves it parses. Running it proves it works — including the two
    raw-SQL index statements, the composite foreign key and the self-referential
    supersession keys, none of which a static check would catch if malformed.

    Later revisions that alter a Step 6 table are applied here too, in graph
    order, so this fixture keeps representing "what a real database looks
    like" rather than "what the first revision alone would build". Applying
    them also proves each one lands cleanly on top of its parent.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from database.models import Organization, User

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    # FK targets must exist first; the revision assumes a provisioned database.
    Organization.__table__.create(eng, checkfirst=True)
    User.__table__.create(eng, checkfirst=True)

    modules = [_load_migration_module()]
    for later in LATER_STEP6_REVISIONS:
        modules.append(_load_revision_module(later))

    for module in modules:
        with eng.begin() as connection:
            ctx = MigrationContext.configure(connection)
            with Operations.context(ctx):
                module.upgrade()

    return eng


class TestMigrationActuallyRuns:
    def test_all_four_tables_are_created(self, migrated_schema):
        present = set(sa.inspect(migrated_schema).get_table_names())
        assert {"work_day_ledger", "work_state_interval",
                "config_version", "config_value"} <= present

    @pytest.mark.parametrize("model", [
        WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
    ])
    def test_executed_columns_match_the_orm_model(self, migrated_schema, model):
        """What the database ends up with, not what the file says it will."""
        actual = sa.inspect(migrated_schema).get_columns(model.__tablename__)
        assert sorted(c["name"] for c in actual) == sorted(
            c.name for c in model.__table__.columns
        )

    @pytest.mark.parametrize("model", [
        WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
    ])
    def test_executed_nullability_matches_the_orm_model(self, migrated_schema, model):
        actual = {
            c["name"]: c["nullable"]
            for c in sa.inspect(migrated_schema).get_columns(model.__tablename__)
        }
        expected = {c.name: c.nullable for c in model.__table__.columns}
        assert actual == expected

    def test_the_expression_index_exists_and_is_unique(self, migrated_schema):
        """Read from the catalogue, not the inspector.

        SQLAlchemy's reflection silently skips expression-based indexes
        ("Skipped unsupported reflection of expression-based index"), so
        ``get_indexes`` would report this index absent even when it exists.
        The behavioural proof is
        ``test_the_org_wide_uniqueness_rule_holds_in_the_migrated_schema``;
        this confirms the DDL that enforces it.
        """
        with migrated_schema.connect() as conn:
            sql = conn.execute(sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='uq_config_value_scope'"
            )).scalar()
        assert sql is not None, "uq_config_value_scope was not created"
        normalised = " ".join(sql.upper().split())
        assert "UNIQUE INDEX" in normalised
        assert "COALESCE(SCOPE_USER_ID, 0)" in normalised

    def test_the_effective_date_index_exists_and_is_descending(self, migrated_schema):
        with migrated_schema.connect() as conn:
            sql = conn.execute(sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='ix_config_version_org_effective'"
            )).scalar()
        assert sql is not None, "ix_config_version_org_effective was not created"
        assert "DESC" in sql.upper()

    def test_the_org_wide_uniqueness_rule_holds_in_the_migrated_schema(
        self, migrated_schema
    ):
        """The rule the parity constraint could not express, proven at runtime."""
        with Session(migrated_schema) as s:
            version = ConfigVersion(
                organization_id=ORG, version_key="v1",
                effective_from=date(2026, 1, 1),
                captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                content_hash="e" * 64,
            )
            s.add(version)
            s.flush()
            for _ in range(2):
                s.add(ConfigValue(
                    config_version_id=version.id, organization_id=ORG,
                    scope_user_id=None, tier="SHIFT", rank=2,
                    key="required_minutes", value_minutes=480,
                ))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()

    def test_the_check_constraints_are_live_in_the_migrated_schema(
        self, migrated_schema
    ):
        with Session(migrated_schema) as s:
            version = ConfigVersion(
                organization_id=ORG, version_key="v2",
                effective_from=date(2026, 2, 1),
                captured_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
                content_hash="f" * 64,
            )
            s.add(version)
            s.flush()
            s.add(ConfigValue(
                config_version_id=version.id, organization_id=ORG,
                tier="SHIFT", rank=2, key="required_minutes",
                value_minutes=99999,
            ))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()

    def test_no_existing_table_was_altered(self, migrated_schema):
        """organizations/users must be exactly what the ORM declares."""
        from database.models import Organization, User

        inspector = sa.inspect(migrated_schema)
        for model in (Organization, User):
            actual = sorted(c["name"] for c in inspector.get_columns(model.__tablename__))
            assert actual == sorted(c.name for c in model.__table__.columns)

    def test_the_migration_created_no_rows(self, migrated_schema):
        """Additive schema, zero data. No backfill, no fabricated history."""
        with migrated_schema.connect() as conn:
            for table in ("work_day_ledger", "work_state_interval",
                          "config_version", "config_value"):
                count = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed names
                ).scalar()
                assert count == 0, f"{table} was populated by the migration"

    def test_downgrade_removes_exactly_the_four_tables(self):
        """Run on its own database so the shared migrated fixture survives."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)

        before = set(sa.inspect(eng).get_table_names())
        with eng.begin() as connection:
            ctx = MigrationContext.configure(connection)
            with Operations.context(ctx):
                module.upgrade()
        after_upgrade = set(sa.inspect(eng).get_table_names())

        with eng.begin() as connection:
            ctx = MigrationContext.configure(connection)
            with Operations.context(ctx):
                module.downgrade()
        after_downgrade = set(sa.inspect(eng).get_table_names())

        assert after_upgrade - before == {
            "work_day_ledger", "work_state_interval",
            "config_version", "config_value",
        }
        assert after_downgrade == before, (
            f"downgrade did not restore the original schema: "
            f"{after_downgrade ^ before}"
        )

    def test_reapplying_the_migration_is_a_no_op(self):
        """A database already carrying these tables (create_all, or a rerun)
        must not fail with "table already exists"."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)

        for _ in range(2):
            with eng.begin() as connection:
                ctx = MigrationContext.configure(connection)
                with Operations.context(ctx):
                    module.upgrade()

        assert {"config_version", "config_value"} <= set(
            sa.inspect(eng).get_table_names()
        )


# ═════════════════════════════════════════════════════════════════════════════
# Ledger schema — Step 1/2 contracts, unchanged by Step 6
# ═════════════════════════════════════════════════════════════════════════════
class TestLedgerSchemaUnchanged:
    """Step 6 gave the ledger tables a migration. It must not have redesigned
    them: the vocabulary, keys and constraints are the ones already accepted."""

    def test_the_eight_work_states_are_unchanged(self):
        from app.work_ledger import WorkState
        assert sorted(s.value for s in WorkState) == [
            "BRK_M", "BRK_R", "LVE", "OFF", "ONC", "TRV", "UNK", "WRK",
        ]

    def test_the_provenance_vocabulary_is_the_owner_ruled_nine(self):
        """Eight at Gate 6, plus PRODUCER_DERIVED by decision D-18A.

        Still pinned as an exact list: a tenth value is a governance decision,
        not a code change.
        """
        from app.work_ledger import Provenance
        assert sorted(p.value for p in Provenance) == [
            "ABANDONED_AUTOCLOSE", "ADMIN_OVERRIDE", "BREAK_DERIVED",
            "BREAK_REPAIR", "CLASSIFIER_DERIVED", "OFFBOARDING",
            "PRODUCER_DERIVED", "STALE_AUTOCLOSE", "WORKER",
        ]

    def test_only_worker_and_admin_override_are_verified_departures(self):
        from app.work_ledger import VERIFIED_DEPARTURE_PROVENANCE
        assert sorted(p.value for p in VERIFIED_DEPARTURE_PROVENANCE) == [
            "ADMIN_OVERRIDE", "WORKER",
        ]

    def test_provenance_is_not_nullable(self):
        assert WorkStateInterval.__table__.c.provenance.nullable is False

    def test_the_natural_key_is_org_user_date_version(self):
        keys = {
            tuple(c.name for c in c_.columns)
            for c_ in WorkDayLedger.__table__.constraints
            if isinstance(c_, sa.UniqueConstraint)
        }
        assert ("organization_id", "user_id", "work_date", "ledger_version") in keys

    def test_intervals_are_uniquely_sequenced_within_a_ledger(self):
        keys = {
            tuple(c.name for c in c_.columns)
            for c_ in WorkStateInterval.__table__.constraints
            if isinstance(c_, sa.UniqueConstraint)
        }
        assert ("ledger_id", "seq") in keys

    def test_both_ledger_tables_are_organization_scoped(self):
        for model in (WorkDayLedger, WorkStateInterval):
            col = model.__table__.c.organization_id
            assert col.nullable is False, model.__tablename__

    def test_intervals_reference_a_ledger(self):
        fks = {
            (fk.parent.name, fk.column.table.name)
            for fk in WorkStateInterval.__table__.foreign_keys
        }
        assert ("ledger_id", "work_day_ledger") in fks

    def test_supersession_chain_is_present(self):
        cols = set(WorkDayLedger.__table__.c.keys())
        assert {"supersedes", "superseded_by", "ledger_version"} <= cols

    def test_integrity_fault_is_recordable(self):
        from app.work_ledger import LedgerState
        assert "LEDGER_INTEGRITY_FAULT" in {s.value for s in LedgerState}
        assert WorkDayLedger.__table__.c.integrity_fault_reason.nullable is True

    def test_provenance_immutability_still_refuses_a_rewrite(self, db):
        """Step 2 behaviour, re-asserted after Step 6 touched the package."""
        from app.work_ledger import Provenance
        from app.work_ledger.states import ProvenanceImmutable

        ledger = WorkDayLedger(
            organization_id=ORG, user_id=USER,
            work_date=datetime(2026, 9, 4), workweek_key="2026-W36",
            ledger_version=1, state="SEALED", timezone_iana="Europe/London", is_working_day=True, working_day_source="org",
            utc_offset_seconds=0,
            created_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            created_by="SYSTEM",
        )
        db.add(ledger)
        db.flush()

        interval = WorkStateInterval(
            ledger_id=ledger.id, organization_id=ORG, seq=1, state="WRK",
            start_at_utc=datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 9, 4, 17, tzinfo=timezone.utc),
            duration_seconds=28800, provenance=Provenance.WORKER.value,
            created_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
        db.add(interval)
        db.flush()

        with pytest.raises(ProvenanceImmutable):
            interval.provenance = Provenance.STALE_AUTOCLOSE.value
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Isolation guards — what this package must never import
# ═════════════════════════════════════════════════════════════════════════════
def _imported_modules(path: pathlib.Path) -> set:
    """Module names imported by a file, read from the AST.

    AST rather than text so a module name appearing in a docstring — and these
    docstrings name ``app_settings`` repeatedly, explaining why it is absent —
    cannot fail the guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _package_files() -> list:
    files = sorted(PACKAGE.glob("*.py"))
    assert files, f"no modules found under {PACKAGE}"
    return files


class TestIsolationGuards:
    def test_the_guard_reads_the_whole_package(self):
        """Guard the guard: a glob that matched nothing would pass vacuously."""
        assert {p.name for p in _package_files()} == {
            "__init__.py", "adapter.py",
            # Owner Decision 1A: the ONE definition of ``config_value``'s
            # append-only trigger SQL, imported by both emitters. String
            # constants only -- it imports nothing, so it cannot violate an
            # import guard, and the forbidden-import checks below cover it
            # anyway. ``config_version`` is deliberately NOT enforced: see
            # that module's docstring.
            "append_only_ddl.py",
            "errors.py", "models.py", "reference.py",
        }

    @pytest.mark.parametrize("forbidden", [
        "app.settings",          # the live, mutate-in-place settings resolver
        "database.models",       # where AppSettings itself is declared
        "app.attendance_policy",  # the fail-open clock-out resolver (D-10)
    ])
    def test_no_live_configuration_dependency(self, forbidden):
        """A live read here would defeat the historical floor entirely."""
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if m == forbidden or m.startswith(forbidden + ".")
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_forbidden_modules_actually_exist(self):
        """Guard the guard: a typo'd module name can never be imported, so the
        test above would pass for the wrong reason."""
        import importlib.util
        for name in ("app.settings", "database.models", "app.attendance_policy"):
            assert importlib.util.find_spec(name) is not None, name

    def test_no_worked_time_dependency(self):
        """Requirement resolution never computes what was WORKED."""
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if "_worked" in m or "work_ledger" in m or "active_time" in m
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_activity_or_ml_dependency(self):
        """INV-6 (no screenshots/keystrokes/app names) and INV-7 (no ML)."""
        banned = ("screenshot", "keystroke", "sklearn", "numpy", "torch",
                  "tensorflow", "app.intelligence")
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path) if any(b in m for b in banned)
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_default_requirement_constant_exists(self):
        """§10.2: nothing here may become a default by accident."""
        suspicious = {480, 420, 28800, 540}
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if isinstance(value, ast.Constant) and value.value in suspicious:
                        pytest.fail(
                            f"{path.name}:{node.lineno} assigns {value.value!r}, "
                            f"which could become a default requirement"
                        )


# ═════════════════════════════════════════════════════════════════════════════
# Scope control — Step 6 must not have enabled anything
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    #: The Phase-2 foundation packages. They may reference each other — that is
    #: how the steps compose, and each such link is pinned by its own step's
    #: tests. What must not happen is the APPLICATION reaching into them, which
    #: is what "not wired" means and what this guard is for.
    FOUNDATION_PACKAGES = (
        "config_versioning", "requirement_resolution", "work_ledger",
        "leave_intervals", "worker_context", "rollout", "eval_day", "eval_week",
        # The ECO threshold reader. Registered here for the same reason as the
        # others: it composes Step 6's version selection and the Step 5 port
        # types, which is foundation-to-foundation and is what this list is for.
        # It is held to its own not-wired guard in test_eco_threshold_reader.
        "eco_threshold",
        # The ECO threshold capture path. Same reasoning; its own not-wired
        # guard lives in test_eco_threshold_capture.
        "eco_threshold_capture",
        # An organization's FIRST configuration version. Composes these models,
        # this hashing function and the approved boundary/actor helpers, which
        # is foundation-to-foundation. Its own not-wired guard lives in
        # test_config_first_capture.
        "config_first_capture",
    )

    def test_nothing_in_the_application_wires_these_packages(self):
        """Not imported by any route, service or scheduler."""
        # Two roles that were the same list until §45.D-15, and are now not:
        #
        #  * NOT_APPLICATION — directories whose OWN files are foundation, so
        #    composing another foundation package from them is expected;
        #  * BANNED_TARGETS  — packages application code may not import.
        #
        # `clock_session_boundary` is foundation on its input side (it composes
        # the Work Ledger `Provenance` vocabulary, which owner ruling §45.D-15
        # requires it to reuse rather than duplicate) but is DELIBERATELY WIRED
        # to production on its output side: D-15 exists precisely so the
        # attendance writers record boundary provenance. It is the first
        # Phase-2 primitive the owner authorised to be wired, so it belongs in
        # the first list and NOT the second — putting it in both would make
        # `app/employees/router.py` an offender for doing what D-15 requires.
        # Its own coverage lives in tests/test_clock_session_boundary.py.
        # `work_ledger_producer` joins the same list for the same reason:
        # foundation on its input side (it composes the Work Ledger service and
        # the D-15 boundary events), and NOT wired into any route, service or
        # scheduler by this increment — which
        # tests/test_work_ledger_producer.py asserts directly.
        NOT_APPLICATION = self.FOUNDATION_PACKAGES + (
            "clock_session_boundary", "work_ledger_producer",
            # EVAL-EVENT composes Step 6 through the ECO threshold reader
            # to perform W >= T. Foundation-to-foundation, and held to its
            # own not-wired guard in tests/test_eval_event.py.
            "eval_event",
            # ALERT consumes EVAL-EVENT's recorded facts and reaches Step 6
            # only transitively through them. Foundation, not application: it
            # has no route, scheduler or UI, and is held to its own guards in
            # tests/test_alert.py.
            "alert",
            # DEPARTURE-RESOLUTION (§24.6, owner Ruling 4). Foundation on its
            # input side: it composes the producer's evidence layer and the
            # Work Ledger provenance vocabulary rather than duplicating either,
            # which is precisely what this list is for. It has no route, no
            # scheduler and no UI, and its own not-wired guard lives in
            # tests/test_departure_resolution.py.
            "departure_resolution",
            # VIEW-AUTHORITY (§24.7, owner ruling B2). It answers "may this
            # caller read this Worker's ECO records" from an explicitly
            # persisted per-tenant grant. Foundation: no route, no scheduler,
            # no UI, and its own not-wired guard lives in
            # tests/test_eco_view_authority.py.
            "eco_view_authority",
            # The GV-17 read contract. Foundation on its input side: it
            # composes the recorded day record, the ledger version that record
            # was evaluated against, and EVAL-DAY's own §19 step-6 predicate —
            # borrowing each rather than restating it, which is what this list
            # is for. It carries no copy and has no surface; its guard lives in
            # tests/test_eco_day_view.py.
            "eco_day_view",
            # BREAK CLASSIFICATION (P-1, P-2). Foundation on its input side: it
            # composes the Work Ledger's own ``LedgerState`` vocabulary to
            # enforce the post-seal rule, rather than duplicating the state
            # names — which is exactly what this list is for. It has no route,
            # no scheduler and no UI, and its guards live in
            # tests/test_break_classification.py.
            "break_classification",
            # BREAK TREATMENT (P-3). The §13.2 artifact behind
            # ``PolicyVersion.break_treatment_ref``. Pure storage and
            # historical resolution; it composes nothing and is wired nowhere.
            "break_treatment",
            # The P0 CALCULATION DRIVER. Foundation on its input side: it
            # composes the producer, EVAL-DAY, EVAL-EVENT, the day alert and
            # ALERT, and reaches Step 6 only transitively through them -- it
            # holds no configuration opinion of its own and resolves nothing.
            # It is the authorized INVOCATION of that pipeline, which §44.2
            # requires before a shadow period can produce data, and it is
            # invoked by an explicit operational command rather than a route or
            # a scheduler. Its guards live in tests/test_eco_driver.py.
            "eco_driver",
            # The §44.3 enablement-gate assessment. Gate 2 is "Tenant fallback
            # policy exists at rank 6", and the versioned-configuration
            # provider is the authoritative reader for it -- asking it is how
            # the gate gets the same answer requirement resolution would.
            # Read-only: no route, no scheduler, no writer, and its own guards
            # in tests/test_enablement_gates.py assert every one of those.
            "enablement_gates",
            # The governed jurisdiction mapping and the three compliance
            # evidence stores. Both compose AUTHORIZED_ROLES -- the one ECO
            # governance authorization vocabulary -- rather than restating it,
            # which is the same composition eval_event makes above. Neither
            # has a route, a scheduler or a UI, and their own guards in
            # tests/test_jurisdiction.py and tests/test_compliance_evidence.py
            # assert that.
            "jurisdiction", "compliance_evidence")
        BANNED_TARGETS = tuple(f"app.{p}" for p in self.FOUNDATION_PACKAGES)

        # The SECOND piece of Phase-2 wiring the owner has authorised, after
        # §45.D-15's boundary events. Owner Ruling 5 for MODEL-WORKWEEK requires
        # that changing `schedule.week_start_day` inside an already-open
        # workweek be REFUSED, and the only place that change can be refused is
        # the endpoint that performs it.
        #
        # Allowed per-file and per-module rather than by exempting
        # `app.work_ledger` wholesale: the settings router may reach the
        # boundary guard and nothing else, so importing the ledger SERVICE from
        # a route is still an offence here, as it should be. Nothing is
        # computed, sealed or enabled by this import — it can only say no.
        AUTHORISED_WIRING = {
            pathlib.Path("app") / "settings" / "router.py":
                ("app.work_ledger.workweek",),
        }

        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if any(part in NOT_APPLICATION for part in path.parts):
                continue
            relative = path.relative_to(BACKEND)
            allowed = AUTHORISED_WIRING.get(relative, ())
            imported = _imported_modules(path)
            banned = [m for m in imported
                      if m.startswith(BANNED_TARGETS)
                      and not m.startswith(allowed)]
            if banned:
                offenders.append(f"{relative} -> {sorted(banned)}")
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_guard_would_notice_application_wiring(self):
        """Guard the guard: a skip list that swallowed everything would pass."""
        scanned = [
            p for p in (BACKEND / "app").rglob("*.py")
            if not any(part in self.FOUNDATION_PACKAGES for part in p.parts)
        ]
        assert len(scanned) > 100, (
            f"only {len(scanned)} application modules scanned; the skip list is "
            f"excluding far more than the foundation packages"
        )

    def test_no_eco_v2_calculation_exists_in_step6(self):
        for path in _package_files():
            body = path.read_text(encoding="utf-8").lower()
            for term in ("shortfall", "early_clock_out_event", "eco_result"):
                assert term not in body, f"{path.name} mentions {term}"


# ═════════════════════════════════════════════════════════════════════════════
# Configuration-domain isolation
# ═════════════════════════════════════════════════════════════════════════════
# ``config_value`` is a general key/value store: one version can hold rows for
# several configuration domains, distinguished only by ``key``. The Step 5 port
# is not general — ``RequirementCandidate`` carries no key. So a value from any
# other domain that reached the resolver would arrive indistinguishable from a
# requirement, and could win on ``rank``. The adapter's read filters on the
# requirement key; this section holds that filter in place.

#: Written literally rather than imported, per this module's convention: a wrong
#: constant must not be able to agree with itself.
REQUIREMENT_KEY_LITERAL = "required_minutes"

#: A key belonging to some other configuration domain. Deliberately generic: the
#: guarantee under test is domain-agnostic, and it holds for any non-requirement
#: key, which is the whole point of filtering on the requirement key rather than
#: excluding a list of known others.
FOREIGN_DOMAIN_KEY = "some_other_domain_minutes"

REQUIREMENT_PATH_FILES = (
    PACKAGE / "adapter.py",
    PACKAGE / "reference.py",
    *sorted((BACKEND / "app" / "requirement_resolution").glob("*.py")),
)


class TestConfigurationDomainIsolation:
    """Exercised against the database-backed provider specifically.

    The in-memory reference provider stores ``RequirementCandidate`` objects,
    which have no key at all, so it cannot hold a foreign-domain value in the
    first place. That immunity is asserted below rather than assumed.
    """

    def _capture(self, db, *, candidates, version_key="v1",
                 effective_from=date(2026, 1, 1)):
        provider = DatabaseConfigVersionProvider(db)
        provider.capture_version(
            organization_id=ORG, config_version_id=version_key,
            effective_from=effective_from, candidates=candidates,
        )
        return provider

    def _version_pk(self, db, version_key="v1") -> int:
        return db.execute(
            sa.select(ConfigVersion).where(
                ConfigVersion.organization_id == ORG,
                ConfigVersion.version_key == version_key,
            )
        ).scalars().one().id

    def _add_foreign_value(self, db, version_pk, *, rank, minutes, tier="SHIFT"):
        """Store another domain's value in the same version.

        Written directly rather than through ``capture_version``, because the
        capture path only ever writes the requirement key — which is precisely
        why the read needed a filter of its own.
        """
        db.add(ConfigValue(
            config_version_id=version_pk, organization_id=ORG,
            scope_user_id=None, tier=tier, rank=rank,
            key=FOREIGN_DOMAIN_KEY, value_minutes=minutes,
        ))
        db.flush()

    def _keys_in(self, db, version_pk):
        return sorted(db.execute(
            sa.select(ConfigValue.key).where(
                ConfigValue.config_version_id == version_pk
            )
        ).scalars().all())

    # ── A: only requirement values are returned ─────────────────────────────
    def test_only_requirement_values_are_returned(self, db):
        provider = self._capture(db, candidates=[_cand("SHIFT", rank=2, minutes=480)])
        pk = self._version_pk(db)
        self._add_foreign_value(db, pk, rank=9, minutes=30)

        # Guard the guard: both rows really are stored in this version, so one
        # returned candidate proves filtering rather than an empty capture.
        assert self._keys_in(db, pk) == sorted(
            [FOREIGN_DOMAIN_KEY, REQUIREMENT_KEY_LITERAL]
        )

        candidates = provider.requirement_candidates(
            organization_id=ORG, user_id=USER, as_of=date(2026, 6, 1),
            version=ConfigVersionRef(
                config_version_id="v1", effective_from=date(2026, 1, 1)),
        )
        assert [(c.tier, c.rank, c.minutes) for c in candidates] == [
            ("SHIFT", 2, 480)
        ]

    def test_a_version_holding_only_a_foreign_value_is_unresolved_not_defaulted(
        self, db
    ):
        """§10.2 again: filtering everything out must not invent a requirement."""
        provider = self._capture(db, candidates=[])
        pk = self._version_pk(db)
        self._add_foreign_value(db, pk, rank=1, minutes=30)

        assert self._keys_in(db, pk) == [FOREIGN_DOMAIN_KEY]
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.outcome is not ResolutionOutcome.RESOLVED
        assert result.required_minutes is None

    # ── B: rank cannot promote a foreign value ──────────────────────────────
    def test_a_better_ranked_foreign_value_never_becomes_the_requirement(self, db):
        """Rank 0 beats every requirement rank there is.

        Without the key filter this row would be returned, sorted to the front
        by the resolver, and the day would resolve to 30 minutes from a
        configuration domain that has nothing to do with a daily requirement.
        """
        provider = self._capture(db, candidates=[_cand("SHIFT", rank=2, minutes=480)])
        pk = self._version_pk(db)
        self._add_foreign_value(
            db, pk, rank=0, minutes=30, tier="WORKER_OVERRIDE")

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 480
        assert result.source_tier == "SHIFT"

    def test_a_foreign_value_never_appears_in_the_audit_chain(self, db):
        """A foreign-domain row is not a malformed requirement — it is not a
        requirement at all, so it must be absent from the resolver's account of
        what the configuration said, not merely absent from the answer.

        ``chain_evaluated`` records every tier consulted up to the winner, so a
        foreign row ranked ahead of the winner would show up there if it leaked.
        """
        provider = self._capture(db, candidates=[_cand("SHIFT", rank=3, minutes=480)])
        pk = self._version_pk(db)
        self._add_foreign_value(db, pk, rank=1, minutes=30, tier="SCHEDULE")

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.required_minutes == 480
        assert result.chain_evaluated == ("SHIFT",)
        assert result.invalid_candidates == ()

    # ── C: precedence is unchanged in the presence of a foreign value ───────
    def test_precedence_is_unchanged_when_a_foreign_value_is_present(self, db):
        provider = self._capture(db, candidates=[
            _cand("TENANT_FALLBACK", rank=6, minutes=480),
            _cand("WORKER_OVERRIDE", rank=1, minutes=300),
            _cand("SHIFT", rank=3, minutes=420),
        ])
        pk = self._version_pk(db)
        self._add_foreign_value(db, pk, rank=2, minutes=30, tier="SCHEDULE")

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.required_minutes == 300
        assert result.source_tier == "WORKER_OVERRIDE"

    def test_a_silent_requirement_tier_still_defers_to_the_next_rank(self, db):
        """The foreign row must not be mistaken for the answer a silent tier
        deferred to."""
        provider = self._capture(db, candidates=[
            _cand("WORKER_OVERRIDE", rank=1, minutes=None),
            _cand("SHIFT", rank=3, minutes=420),
        ])
        pk = self._version_pk(db)
        self._add_foreign_value(db, pk, rank=2, minutes=30, tier="SCHEDULE")

        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.required_minutes == 420
        assert result.source_tier == "SHIFT"

    # ── D: effective dating and the floor are unchanged ─────────────────────
    def test_effective_dating_is_unchanged_when_foreign_values_are_present(self, db):
        provider = self._capture(
            db, version_key="v1", effective_from=date(2026, 1, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        provider.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 6, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=540)],
        )
        for key in ("v1", "v2"):
            self._add_foreign_value(
                db, self._version_pk(db, key), rank=0, minutes=30)

        for as_of, expected, version in (
            (date(2026, 5, 31), 480, "v1"),
            (date(2026, 6, 1), 540, "v2"),
            (date(2026, 9, 1), 540, "v2"),
        ):
            result = resolve_requirement_strict(
                organization_id=ORG, user_id=USER,
                as_of=as_of, provider=provider,
            )
            assert result.required_minutes == expected, as_of
            assert result.config_version_id == version, as_of

    def test_the_historical_floor_is_unchanged_when_a_foreign_value_is_present(
        self, db
    ):
        provider = self._capture(
            db, effective_from=date(2026, 6, 1),
            candidates=[_cand("SHIFT", rank=2, minutes=480)],
        )
        self._add_foreign_value(db, self._version_pk(db), rank=0, minutes=30)

        assert provider.version_as_of(
            organization_id=ORG, as_of=date(2026, 5, 31)
        ) is None
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 5, 31), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.NO_VERSION
        assert result.required_minutes is None

    # ── E: the reference provider is semantically identical ─────────────────
    def test_both_providers_agree_on_a_requirement_only_capture(self, provider):
        """Runs on the full provider matrix: the filter changed the adapter's
        query, and must not have changed the contract either implementation
        satisfies."""
        provider.capture_version(
            organization_id=ORG, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[
                _cand("WORKER_OVERRIDE", rank=1, minutes=300),
                _cand("SHIFT", rank=3, minutes=420),
            ],
        )
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=USER,
            as_of=date(2026, 6, 1), provider=provider,
        )
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == 300
        assert result.source_tier == "WORKER_OVERRIDE"
        assert result.config_version_id == "v1"

    def test_the_port_type_carries_no_key_so_the_reference_cannot_leak_one(self):
        """Why only the adapter needed the filter.

        ``RequirementCandidate`` is the only thing the port can return, and it
        has no key field — so the in-memory reference provider has no domain
        dimension to confuse, and the type itself is the second line of
        defence behind the adapter's query.
        """
        assert "key" not in RequirementCandidate.__dataclass_fields__
        assert set(RequirementCandidate.__dataclass_fields__) == {
            "tier", "rank", "minutes", "source_ref",
        }

    # ── F: the requirement path does not read the relationship ─────────────
    def test_the_requirement_path_never_reads_configversion_values(self):
        """``ConfigVersion.values`` returns every value in a version regardless
        of domain, so it is a third way past the filter.

        It is declared on the model (composing a version needs it) and read
        nowhere in the requirement path. Asserted from the AST so a mention in
        a docstring cannot fail the guard.
        """
        offenders = []
        for path in REQUIREMENT_PATH_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "values":
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            f"requirement path reads a .values attribute at {offenders}; if this "
            f"is ConfigVersion.values it bypasses the requirement-key filter"
        )

    def test_the_requirement_path_guard_reads_the_files_it_claims_to(self):
        """Guard the guard: a glob that matched nothing would pass vacuously."""
        names = {p.name for p in REQUIREMENT_PATH_FILES}
        assert {"adapter.py", "reference.py", "resolver.py", "ports.py"} <= names
        for path in REQUIREMENT_PATH_FILES:
            assert path.is_file(), path

    # ── The constant itself ─────────────────────────────────────────────────
    def test_the_adapter_writes_and_reads_one_and_the_same_key(self):
        """A write under one key and a read under another would resolve every
        day to no candidates — silently, and only in production."""
        from app.config_versioning.adapter import REQUIREMENT_KEY

        assert REQUIREMENT_KEY == REQUIREMENT_KEY_LITERAL

    def test_the_stored_key_is_the_requirement_key(self, db):
        provider = self._capture(db, candidates=[_cand("SHIFT", rank=2, minutes=480)])
        assert self._keys_in(db, self._version_pk(db)) == [REQUIREMENT_KEY_LITERAL]
