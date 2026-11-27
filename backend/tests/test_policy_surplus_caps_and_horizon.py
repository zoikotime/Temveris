"""
Stage 2 — §15.1's surplus caps and §21.5's retroactive horizon on PolicyVersion.
================================================================================

Owner rulings D-6.8 and D-7.1. This increment establishes **authoritative
versioned policy data and nothing else**. It implements no netting, no bank, no
horizon evaluation and no notification behaviour; Stage 3 consumes the caps and
Stage 4 consumes the horizon.

WHAT §15.1 ACTUALLY SAYS ABOUT THE CAPS
----------------------------------------
    "Surplus MUST be capped per day and per period, per Tenant configuration,
     to prevent one long day erasing a week."

TWO caps. The spec is explicit and this suite pins the count, because a single
"surplus cap" would satisfy a careless reading and silently drop half the rule.

    "The bank balance is itself a fold over day records, never a stored
     counter (INV-3)."

So the caps are LIMITS, never quantities that accumulate. `TestNoStoredBank`
asserts that structurally across the whole ECO surface, not just this table —
the tempting place to add a counter later is a new table, not a new column here.

WHY THE HORIZON DEFAULTS TO 90 AND THE CAPS DEFAULT TO NOTHING
---------------------------------------------------------------
§21.5 states its default outright ("a Tenant-configurable retroactive horizon
(default 90 days)"), so recording 90 when the Tenant states nothing is the
specification's answer. §15.1 states no default for the caps and there is no
safe one to invent: a cap of 0 means "bank nothing" and an absent cap means
"nobody said". Those are different policies, and MODEL-NETTING needs to tell
them apart in order to fail closed.

THE ONE THING THIS SUITE PROVES ABOUT RESOLUTION
-------------------------------------------------
A default is applied at CAPTURE, where it is recorded explicitly and visibly.
It is NEVER substituted at resolution for a value a row does not carry. That is
the same rule D-1.1/D-2.1 set for the scheduled end, and `TestResolutionNeverDefaults`
is the test that stops a future change from quietly softening it.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa

from app.policy_version import (
    DEFAULT_RETROACTIVE_HORIZON_DAYS,
    InvalidPolicyVersion,
    PolicyOutcome,
    PolicyVersion,
    capture_policy_version,
    compute_policy_content_hash,
    resolve_policy_as_of,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = (BACKEND / "alembic" / "versions"
             / "a3c8e5f10b47_policy_surplus_caps_and_horizon.py")

ORG = 9720
JAN = date(2026, 1, 1)
JUN = date(2026, 6, 1)
MAR = date(2026, 3, 15)


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    PolicyVersion.__table__.create(engine, checkfirst=True)


def _capture(db, *, key="p1", effective_from=JAN, org=ORG, **kw):
    params = dict(
        organization_id=org, policy_version=key, effective_from=effective_from,
        counts_toward_week=True, netting_mode="NONE", grace_minutes=0,
        netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
        captured_by="owner@stage2.test",
    )
    params.update(kw)
    row = capture_policy_version(db, **params)
    db.flush()
    return row


# ═════════════════════════════════════════════════════════════════════════════
# A / B. The three fields exist, with the ruled names and units
# ═════════════════════════════════════════════════════════════════════════════

class TestFieldsExist:

    def test_both_surplus_caps_exist(self):
        cols = {c.name for c in PolicyVersion.__table__.c}
        assert "surplus_cap_per_day_minutes" in cols
        assert "surplus_cap_per_period_minutes" in cols

    def test_there_are_exactly_two_caps_not_one_and_not_three(self):
        """§15.1 names per-day AND per-period. A third would be a spec change."""
        # "_cap_" rather than "cap": `captured_at_utc` and `captured_by`
        # both contain the substring and are provenance, not policy.
        caps = {c.name for c in PolicyVersion.__table__.c
                if "_cap_" in c.name}
        assert caps == {"surplus_cap_per_day_minutes",
                        "surplus_cap_per_period_minutes"}

    def test_the_retroactive_horizon_exists(self):
        assert "retroactive_horizon_days" in {
            c.name for c in PolicyVersion.__table__.c}

    def test_the_units_are_in_the_names(self):
        """Minutes for the caps, days for the horizon — §15.1 and §21.5."""
        cols = PolicyVersion.__table__.c
        assert cols["surplus_cap_per_day_minutes"].name.endswith("_minutes")
        assert cols["surplus_cap_per_period_minutes"].name.endswith("_minutes")
        assert cols["retroactive_horizon_days"].name.endswith("_days")

    def test_all_three_are_integers(self):
        for name in ("surplus_cap_per_day_minutes",
                     "surplus_cap_per_period_minutes",
                     "retroactive_horizon_days"):
            assert isinstance(PolicyVersion.__table__.c[name].type,
                              sa.Integer), name


# ═════════════════════════════════════════════════════════════════════════════
# C. The horizon default is 90 — recorded, never substituted
# ═════════════════════════════════════════════════════════════════════════════

class TestHorizonDefault:

    def test_the_named_default_is_ninety(self):
        assert DEFAULT_RETROACTIVE_HORIZON_DAYS == 90

    def test_a_capture_that_states_nothing_records_ninety(self, db):
        row = _capture(db)
        assert row.retroactive_horizon_days == 90

    def test_it_is_recorded_on_the_row_not_implied(self, db):
        """The stored value is 90, not NULL-meaning-90."""
        _capture(db)
        stored = db.execute(sa.text(
            "SELECT retroactive_horizon_days FROM policy_version "
            "WHERE organization_id = :o"), {"o": ORG}).scalar()
        assert stored == 90

    def test_an_explicit_value_overrides_the_default(self, db):
        row = _capture(db, retroactive_horizon_days=30)
        assert row.retroactive_horizon_days == 30

    def test_an_explicit_none_records_no_horizon(self, db):
        """Distinct from the default: "no horizon recorded" stays available."""
        row = _capture(db, retroactive_horizon_days=None)
        assert row.retroactive_horizon_days is None

    def test_zero_is_a_real_horizon_not_an_absence(self, db):
        row = _capture(db, retroactive_horizon_days=0)
        assert row.retroactive_horizon_days == 0
        assert row.retroactive_horizon_days is not None


# ═════════════════════════════════════════════════════════════════════════════
# D / E / F. Content hash
# ═════════════════════════════════════════════════════════════════════════════

BASE = dict(
    organization_id=ORG, policy_version="p1", effective_from=JAN,
    counts_toward_week=True, break_treatment_ref=None, netting_mode="NONE",
    rounding_profile_ref=None, grace_minutes=0,
    surplus_cap_per_day_minutes=None, surplus_cap_per_period_minutes=None,
    retroactive_horizon_days=None,
)


class TestContentHash:

    def test_identical_content_hashes_identically(self):
        assert compute_policy_content_hash(**BASE) == \
            compute_policy_content_hash(**BASE)

    @pytest.mark.parametrize("field,value", [
        ("surplus_cap_per_day_minutes", 60),
        ("surplus_cap_per_period_minutes", 240),
        ("retroactive_horizon_days", 90),
    ])
    def test_each_new_field_moves_the_hash(self, field, value):
        other = dict(BASE)
        other[field] = value
        assert compute_policy_content_hash(**other) != \
            compute_policy_content_hash(**BASE)

    def test_the_two_caps_are_not_interchangeable(self):
        """A per-day cap of 60 and a per-period cap of 60 are different
        policies, so they must not collide onto one digest."""
        day = dict(BASE, surplus_cap_per_day_minutes=60)
        period = dict(BASE, surplus_cap_per_period_minutes=60)
        assert compute_policy_content_hash(**day) != \
            compute_policy_content_hash(**period)

    def test_none_and_zero_hash_differently(self):
        """"No cap recorded" is not "a cap of zero". MODEL-NETTING needs the
        difference to refuse an uncapped bank rather than bank nothing."""
        absent = dict(BASE, surplus_cap_per_day_minutes=None)
        zero = dict(BASE, surplus_cap_per_day_minutes=0)
        assert compute_policy_content_hash(**absent) != \
            compute_policy_content_hash(**zero)

    def test_field_order_cannot_change_the_digest(self):
        """sort_keys makes the projection canonical; asserted rather than
        assumed, because a dict literal's order is the one thing a reader
        cannot see is irrelevant."""
        forward = compute_policy_content_hash(**BASE)
        shuffled = compute_policy_content_hash(
            **{k: BASE[k] for k in reversed(list(BASE))})
        assert forward == shuffled

    def test_the_stored_hash_matches_an_independent_calculation(self, db):
        """Transcribed by hand, never imported."""
        import hashlib

        row = _capture(db, netting_mode="WEEKLY_BANK",
                       surplus_cap_per_day_minutes=45,
                       surplus_cap_per_period_minutes=60,
                       retroactive_horizon_days=30)
        payload = {
            "organization_id": ORG,
            "policy_version": "p1",
            "effective_from": JAN.isoformat(),
            "counts_toward_week": True,
            "break_treatment_ref": None,
            "netting_mode": "WEEKLY_BANK",
            "rounding_profile_ref": None,
            "grace_minutes": 0,
            "surplus_cap_per_day_minutes": 45,
            "surplus_cap_per_period_minutes": 60,
            "retroactive_horizon_days": 30,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True)
        assert row.content_hash == hashlib.sha256(
            encoded.encode("utf-8")).hexdigest()

    def test_there_is_still_exactly_one_hashing_mechanism(self):
        """The canonical projection was EXTENDED, not duplicated."""
        src = (BACKEND / "app" / "policy_version"
               / "models.py").read_text(encoding="utf-8")
        assert src.count("hashlib.sha256") == 1
        assert src.count("def compute_policy_content_hash") == 1


# ═════════════════════════════════════════════════════════════════════════════
# G / H. Effective dating and historical behaviour
# ═════════════════════════════════════════════════════════════════════════════

class TestEffectiveDating:

    def _two_versions(self, db):
        _capture(db, key="v1", effective_from=JAN,
                 surplus_cap_per_day_minutes=30,
                 retroactive_horizon_days=90)
        _capture(db, key="v2", effective_from=JUN,
                 surplus_cap_per_day_minutes=120,
                 retroactive_horizon_days=30)

    def test_the_older_version_governs_a_date_before_the_newer_one(self, db):
        self._two_versions(db)
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=MAR)
        assert r.outcome is PolicyOutcome.RESOLVED
        assert r.policy_version == "v1"
        assert r.surplus_cap_per_day_minutes == 30
        assert r.retroactive_horizon_days == 90

    def test_the_newer_version_governs_only_from_its_own_date(self, db):
        self._two_versions(db)
        assert resolve_policy_as_of(
            db, organization_id=ORG, as_of=JUN).policy_version == "v2"
        assert resolve_policy_as_of(
            db, organization_id=ORG, as_of=date(2026, 5, 31)
        ).policy_version == "v1"

    def test_capturing_a_newer_version_does_not_rewrite_the_older_one(self, db):
        """§17 / §12.4: history changes by addition, never by mutation."""
        first = _capture(db, key="v1", effective_from=JAN,
                         surplus_cap_per_day_minutes=30,
                         retroactive_horizon_days=90)
        before = (first.content_hash, first.surplus_cap_per_day_minutes,
                  first.retroactive_horizon_days)

        _capture(db, key="v2", effective_from=JUN,
                 surplus_cap_per_day_minutes=999,
                 retroactive_horizon_days=7)

        db.expire_all()
        again = db.query(PolicyVersion).filter_by(
            organization_id=ORG, policy_version="v1").one()
        assert (again.content_hash, again.surplus_cap_per_day_minutes,
                again.retroactive_horizon_days) == before

        # And the historical date still resolves to the historical values.
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=MAR)
        assert r.surplus_cap_per_day_minutes == 30
        assert r.retroactive_horizon_days == 90

    def test_a_date_below_the_floor_still_fails_closed(self, db):
        """The strict contract is unchanged: no version, no silent default."""
        self._two_versions(db)
        r = resolve_policy_as_of(db, organization_id=ORG,
                                 as_of=date(2019, 12, 31))
        assert r.outcome is PolicyOutcome.NO_VERSION
        assert r.surplus_cap_per_day_minutes is None
        assert r.retroactive_horizon_days is None


# ═════════════════════════════════════════════════════════════════════════════
# I. Resolution never manufactures a value
# ═════════════════════════════════════════════════════════════════════════════

class TestResolutionNeverDefaults:

    def test_a_row_with_no_horizon_resolves_to_none_not_ninety(self, db):
        """The load-bearing one.

        A row captured with an explicit ``None`` — and, in a real database,
        every row written before the column existed — carries no horizon.
        Substituting §21.5's 90 here would assert a policy the Tenant never
        configured, and Stage 4 could no longer tell "configured 90" from
        "configured nothing".
        """
        _capture(db, retroactive_horizon_days=None)
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=MAR)
        assert r.outcome is PolicyOutcome.RESOLVED
        assert r.retroactive_horizon_days is None
        assert r.retroactive_horizon_days != DEFAULT_RETROACTIVE_HORIZON_DAYS

    def test_a_row_with_no_caps_resolves_to_none_not_zero(self, db):
        _capture(db, netting_mode="WEEKLY_BANK")
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=MAR)
        assert r.surplus_cap_per_day_minutes is None
        assert r.surplus_cap_per_period_minutes is None

    def test_no_mutable_setting_can_override_the_policy(self):
        """The resolver reads PolicyVersion and nothing else.

        Structural: the module must not reach any live-configuration source.
        `resolve_policy_as_of` answering from AppSettings or the schedule
        resolver would make "which policy governed that day" depend on today.
        """
        src = (BACKEND / "app" / "policy_version"
               / "service.py").read_text(encoding="utf-8")
        for forbidden in ("AppSettings", "OrgSettings", "resolve_work_schedule",
                          "getenv", "os.environ"):
            assert forbidden not in src, forbidden

    #: The modules authorised to read the two caps. MODEL-NETTING's fold
    #: consumes them at §18 step 10; the recording boundary hands them to it.
    #: Anything else reading a cap would be a second netting implementation.
    _CAP_CONSUMERS = {"netting.py", "recording.py"}

    def test_only_model_netting_consumes_the_caps(self):
        """Narrowed when MODEL-NETTING was authorised, not deleted.

        Stage 2 asserted that NOTHING consumed the caps — the right claim
        while they were inert. Stage 3 authorised exactly one consumer, so the
        claim narrows to name it; every other module is still held to the
        original rule.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "__pycache__" in str(path) or "policy_version" in str(path):
                continue
            if path.name in self._CAP_CONSUMERS:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "surplus_cap_per_day_minutes" in body:
                offenders.append(path.name)
        assert offenders == [], (
            f"a second cap consumer appeared in {offenders}")

    #: The one module authorised to read the horizon: MODEL-HORIZON's
    #: §21.5 decision. It resolves the value governing a corrected workweek
    #: and decides notification ELIGIBILITY; nothing else may read it, and in
    #: particular no evaluator may, because the horizon touches no persistence.
    _HORIZON_CONSUMERS = {"horizon.py"}

    def test_only_model_horizon_consumes_the_horizon(self):
        """Narrowed when MODEL-HORIZON was authorised, not deleted.

        Stage 3 asserted that NOTHING consumed the horizon — the right claim
        while Stage 4 had not run. Stage 4 authorised exactly one consumer, so
        the claim narrows to name it.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "__pycache__" in str(path) or "policy_version" in str(path):
                continue
            if path.name in self._HORIZON_CONSUMERS:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "retroactive_horizon_days" in body:
                offenders.append(path.name)
        assert offenders == [], (
            f"a second horizon consumer appeared in {offenders}")

    def test_no_evaluator_reads_the_horizon(self):
        """§21.5 governs notification eligibility, never persistence (D-7.3).

        An evaluator reading it would make a day record depend on the clock,
        which is the determinism every other ECO invariant rests on.
        """
        for name in ("evaluator.py", "recording.py", "netting.py"):
            body = (BACKEND / "app" / "eval_day" / name).read_text(
                encoding="utf-8")
            assert "retroactive_horizon" not in body, name


# ═════════════════════════════════════════════════════════════════════════════
# J. No stored bank
# ═════════════════════════════════════════════════════════════════════════════

class TestNoStoredBank:
    """§15.1 / INV-3: the bank balance is a fold, never a stored counter."""

    BANNED = ("bank_balance", "bank_counter", "bank_state", "banked_minutes",
              "surplus_balance", "bank_accumulator", "weekly_bank_total",
              "period_bank_total")

    def test_no_bank_artifact_exists_anywhere_in_the_app(self):
        """IDENTIFIERS, not substrings.

        MODEL-NETTING's module names these things in prose precisely in order
        to forbid them, and a text scan cannot tell a prohibition from a
        violation. Parsing for real names is narrower and stronger.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.arg):
                    names.add(node.arg)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, ast.keyword) and node.arg:
                    names.add(node.arg)
            for token in self.BANNED:
                if token in names:
                    offenders.append(f"{path.name}:{token}")
        assert offenders == [], offenders

    def test_no_bank_table_was_created_by_any_migration(self):
        offenders = []
        for path in (BACKEND / "alembic" / "versions").glob("*.py"):
            body = path.read_text(encoding="utf-8", errors="ignore")
            for token in self.BANNED:
                if token in body:
                    offenders.append(f"{path.name}:{token}")
        assert offenders == [], offenders

    def test_this_migration_adds_columns_and_nothing_else(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {node.func.attr for node in ast.walk(upgrade)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        assert "add_column" in called
        for banned in ("create_table", "execute", "bulk_insert", "drop_table"):
            assert banned not in called, banned

    def test_the_caps_are_limits_not_quantities(self):
        """A limit is named for what it bounds; a balance is named for what it
        holds. Asserted so a later rename cannot turn one into the other."""
        cols = {c.name for c in PolicyVersion.__table__.c}
        for name in cols:
            assert "balance" not in name, name
            assert "counter" not in name, name
            assert "accumulat" not in name, name


# ═════════════════════════════════════════════════════════════════════════════
# K. NONE mode does not acquire a cap
# ═════════════════════════════════════════════════════════════════════════════

class TestNoneMode:

    def test_none_mode_captures_with_no_caps(self, db):
        row = _capture(db, netting_mode="NONE")
        assert row.netting_mode == "NONE"
        assert row.surplus_cap_per_day_minutes is None
        assert row.surplus_cap_per_period_minutes is None

    def test_none_mode_resolves_with_no_caps(self, db):
        _capture(db, netting_mode="NONE")
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=MAR)
        assert r.netting_mode == "NONE"
        assert r.surplus_cap_per_day_minutes is None
        assert r.surplus_cap_per_period_minutes is None

    def test_a_cap_stated_under_none_is_still_recorded_verbatim(self, db):
        """Not silently dropped. §15's "DO NOT SHIP NONE AS AN ASSUMPTION"
        cuts both ways: a Tenant who states a cap alongside NONE has stated
        something, and erasing it would be the platform deciding for them.
        Whether it MEANS anything is MODEL-NETTING's question, not this one."""
        row = _capture(db, netting_mode="NONE",
                       surplus_cap_per_day_minutes=45)
        assert row.surplus_cap_per_day_minutes == 45

    def test_a_bank_mode_with_no_caps_is_recorded_not_refused(self, db):
        """The Stage-3 hand-off, made explicit.

        §15.1's "surplus MUST be capped" constrains bank CONSUMPTION, which
        this increment does not implement. Capture records the gap truthfully
        and MODEL-NETTING refuses to consume an uncapped bank — a refusal it
        can only make because the NULL survived to be seen.
        """
        row = _capture(db, netting_mode="WEEKLY_BANK")
        assert row.netting_mode == "WEEKLY_BANK"
        assert row.surplus_cap_per_day_minutes is None
        assert row.surplus_cap_per_period_minutes is None


# ═════════════════════════════════════════════════════════════════════════════
# Domain validation
# ═════════════════════════════════════════════════════════════════════════════

class TestDomain:

    @pytest.mark.parametrize("field", [
        "surplus_cap_per_day_minutes",
        "surplus_cap_per_period_minutes",
        "retroactive_horizon_days",
    ])
    def test_a_negative_value_is_refused(self, db, field):
        with pytest.raises(InvalidPolicyVersion):
            _capture(db, **{field: -1})

    @pytest.mark.parametrize("field", [
        "surplus_cap_per_day_minutes",
        "surplus_cap_per_period_minutes",
        "retroactive_horizon_days",
    ])
    def test_a_boolean_is_refused(self, db, field):
        """bool is an int subclass; True would otherwise store as 1."""
        with pytest.raises(InvalidPolicyVersion):
            _capture(db, **{field: True})

    @pytest.mark.parametrize("field", [
        "surplus_cap_per_day_minutes",
        "surplus_cap_per_period_minutes",
        "retroactive_horizon_days",
    ])
    def test_a_non_integer_is_refused(self, db, field):
        with pytest.raises(InvalidPolicyVersion):
            _capture(db, **{field: "60"})

    def test_the_database_check_admits_null_but_not_negative(self, db):
        """The CHECK must not reject NULL — doing so would force the backfill
        the migration refuses to perform."""
        checks = {c.name for c in PolicyVersion.__table__.constraints
                  if c.__class__.__name__ == "CheckConstraint"}
        assert "ck_policy_version_surplus_cap_day_nonnegative" in checks
        assert "ck_policy_version_surplus_cap_period_nonnegative" in checks
        assert "ck_policy_version_retroactive_horizon_nonnegative" in checks

        _capture(db, surplus_cap_per_day_minutes=None)   # NULL is admissible
        db.flush()


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════

class TestMigration:

    def test_it_is_an_ordinary_child_of_the_previous_head(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", None) in ("revision", "down_revision",
                                                  "branch_labels", "depends_on"):
                        constants[t.id] = ast.literal_eval(node.value)
        assert constants["revision"] == "a3c8e5f10b47"
        assert constants["down_revision"] == "f4a7c2b91e68"
        assert constants["branch_labels"] is None
        assert constants["depends_on"] is None

    def test_the_graph_stays_one_base_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_it_performs_no_backfill(self):
        """No UPDATE, no execute, no manufactured historical value."""
        body = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(body)
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {node.func.attr for node in ast.walk(upgrade)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        assert "execute" not in called
        assert "bulk_insert" not in called
        # And no literal 90 anywhere in the executable body — the default is
        # applied at capture, never written into history.
        assert not any(
            isinstance(node, ast.Constant) and node.value == 90
            for node in ast.walk(upgrade))

    def test_the_executed_schema_matches_the_orm_model(self, tmp_path):
        """Run it, rather than reading it."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        creating = (BACKEND / "alembic" / "versions"
                    / "b4e8c2a95d17_policy_version.py")

        def _load(path):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        provenance = (BACKEND / "alembic" / "versions"
                      / "f1c6b28d5a73_eco_compliance_evidence_and_jurisdiction.py")
        for path in (creating, MIGRATION, provenance):
            module = _load(path)
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()

        actual = sa.inspect(eng)
        assert sorted(c["name"] for c in actual.get_columns("policy_version")) \
            == sorted(c.name for c in PolicyVersion.__table__.columns)
        nullability = {c["name"]: c["nullable"]
                       for c in actual.get_columns("policy_version")}
        for name in ("surplus_cap_per_day_minutes",
                     "surplus_cap_per_period_minutes",
                     "retroactive_horizon_days"):
            assert nullability[name] is True, name

    def test_the_upgrade_is_idempotent(self):
        """Applied twice, it is a no-op the second time."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        creating = (BACKEND / "alembic" / "versions"
                    / "b4e8c2a95d17_policy_version.py")

        def _load(path):
            spec = importlib.util.spec_from_file_location(
                path.stem + "_idem", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        for path in (creating, MIGRATION, MIGRATION):
            module = _load(path)
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()

        cols = {c["name"] for c in sa.inspect(eng).get_columns("policy_version")}
        assert "retroactive_horizon_days" in cols


# ═════════════════════════════════════════════════════════════════════════════
# L / N. Nothing else moved
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_golden_vectors_are_untouched(self):
        vectors = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = json.loads(vectors.read_text(encoding="utf-8"))
        assert data["_vector_count"] == 28
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

    def test_the_netting_vectors_moved_only_when_netting_was_built(self):
        """Stage 2 added the caps and did NOT move the vectors; Stage 3 built
        the fold and did. Both halves asserted, because the caps alone were
        never sufficient — §15.1 needs the fold, and the fold needs the caps.
        """
        vectors = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        by_id = {v["id"]: v for v in
                 json.loads(vectors.read_text(encoding="utf-8"))["vectors"]}
        for gid in ("GV-19", "GV-20"):
            assert by_id[gid]["harness"]["execution"] == "executable", gid
            assert by_id[gid]["harness"]["blocking_dependencies"] == [], gid

        # And the caps they need are still policy content, not a default.
        from app.policy_version import PolicyVersion
        cols = {c.name for c in PolicyVersion.__table__.c}
        assert "surplus_cap_per_day_minutes" in cols
        assert "surplus_cap_per_period_minutes" in cols

    def test_the_horizon_vector_is_promoted_on_a_stored_horizon(self):
        """Restated for Stage 4, which authorised exactly this promotion.

        Stage 2 asserted GV-23 stayed blocked on MODEL-HORIZON, because this
        package only stored the horizon and nothing evaluated it. Stage 4
        shipped the evaluation, so the claim becomes the stronger one: GV-23
        is executable, AND the horizon it is decided against is still this
        package's stored policy content rather than a constant somewhere.
        """
        vectors = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        by_id = {v["id"]: v for v in
                 json.loads(vectors.read_text(encoding="utf-8"))["vectors"]}
        harness = by_id["GV-23"]["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []

        from app.policy_version import PolicyVersion
        cols = {c.name for c in PolicyVersion.__table__.c}
        assert "retroactive_horizon_days" in cols

    def test_eco_v1_and_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_the_evaluator_applies_the_bank_at_step_eleven_only(self):
        """Restated for Stage 3, which authorised exactly this arithmetic.

        Stage 2 asserted the evaluator mentioned no bank at all. MODEL-NETTING
        made that false by authorisation, so the claim becomes the stronger
        one: the bank appears in exactly ONE arithmetic statement, and it is
        §18 step 11.
        """
        src = (BACKEND / "app" / "eval_day"
               / "evaluator.py").read_text(encoding="utf-8")
        arithmetic = [
            line.strip() for line in src.splitlines()
            if "bank_credit" in line
            and not line.strip().startswith("#")
            and any(op in line for op in (" - ", " + ", " * ", " / "))
        ]
        assert arithmetic == [
            "gross = max(required_minutes - qualifying_minutes "
            "- bank_credit, 0)"
        ], arithmetic

    def test_surplus_is_never_computed_in_the_evaluator(self):
        """The fold owns the surplus; the evaluator only spends its result."""
        src = (BACKEND / "app" / "eval_day"
               / "evaluator.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "surplus" not in line.lower():
                continue
            assert '"""' in line or stripped.startswith("*") or                 "§" in line or "MODEL-NETTING" in line, line
