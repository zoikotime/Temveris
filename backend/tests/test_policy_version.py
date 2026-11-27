"""
POLICY-VERSION — the §11 Policy artifact.
==========================================

Three properties under test:

1. **A captured policy is evidence, so it cannot be rewritten** — by the ORM or
   beneath it. The database triggers are the guarantee; the ORM listeners are
   defence in depth, and both are exercised.
2. **Which policy governed a past date is decided by the timeline, never by
   what is configured today**, and every way that question can fail to have an
   answer fails closed with its own typed outcome.
3. **It owns exactly five attributes.** ``daily_minutes`` stays in
   ``ConfigValue`` and ``is_working_day`` stays on ``WorkDayLedger``; this suite
   asserts both, because a duplicated attribute is a second source of truth
   that no test downstream would notice.

ONE AUTHORIZED CONSUMER, AND PINNED THAT WAY
--------------------------------------------
EVAL-DAY's recording boundary now resolves a policy and refuses the day with
``POLICY_UNRESOLVED`` when none applies. That is the ONLY consumer;
``TestNotWired`` names it and still fails on a second one, and separately
asserts that the seam applies none of the five policy attributes — netting,
grace, break treatment and rounding remain deferred. GV-25 stays blocked.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

from database.database import Base

from app.policy_version import (
    InvalidPolicyVersion,
    NETTING_MODES,
    NettingMode,
    PolicyOutcome,
    PolicyResolution,
    PolicyVersion,
    PolicyVersionCollision,
    PolicyVersionImmutable,
    capture_policy_version,
    DEFAULT_RETROACTIVE_HORIZON_DAYS,
    compute_policy_content_hash,
    resolve_policy_as_of,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "policy_version"

ORG, OTHER_ORG = 9310, 9320
JAN = date(2026, 1, 1)
JUN = date(2026, 6, 1)


@pytest.fixture()
def db():
    import app.policy_version.models      # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng, tables=[PolicyVersion.__table__])
    with SaSession(eng, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def unconstrained():
    """A ``policy_version`` table built WITHOUT its unique constraints, CHECKs
    or triggers.

    Only for the resolver's defensive branches. Those exist for a database that
    somehow holds a row the real schema forbids, so they cannot be reached
    through the real schema — and a branch that can never run under test is a
    branch nobody has checked.
    """
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE policy_version ("
            " id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL,"
            " policy_version VARCHAR(64) NOT NULL, effective_from DATE NOT NULL,"
            " captured_at_utc DATETIME NOT NULL, captured_by VARCHAR(255) NOT NULL,"
            " counts_toward_week BOOLEAN NOT NULL, break_treatment_ref VARCHAR(64),"
            " netting_mode VARCHAR(32) NOT NULL, rounding_profile_ref VARCHAR(64),"
            " grace_minutes INTEGER NOT NULL,"
            # D-6.8 / D-7.1. Present so the ORM can SELECT the row, but
            # still WITHOUT their CHECKs — this fixture exists to hold
            # values the real schema forbids.
            " surplus_cap_per_day_minutes INTEGER,"
            " surplus_cap_per_period_minutes INTEGER,"
            " retroactive_horizon_days INTEGER,"
            # D-5 / OD-02. Shape only: nullable and unconstrained,
            # exactly as a pre-existing row would have it.
            " netting_decision_provenance VARCHAR(32),"
            " content_hash VARCHAR(64) NOT NULL)"))
    with SaSession(eng, expire_on_commit=False) as s:
        yield s


def _raw_insert(db, *, policy_version, effective_from, organization_id=ORG,
                netting_mode="NONE", grace_minutes=0, counts_toward_week=True):
    db.execute(sa.text(
        "INSERT INTO policy_version (organization_id, policy_version,"
        " effective_from, captured_at_utc, captured_by, counts_toward_week,"
        " netting_mode, grace_minutes, content_hash)"
        " VALUES (:org, :ver, :eff, :at, 'x', :ctw, :mode, :grace, 'h')"),
        {"org": organization_id, "ver": policy_version,
         "eff": effective_from.isoformat(), "at": "2026-01-01 00:00:00",
         "ctw": counts_toward_week, "mode": netting_mode,
         "grace": grace_minutes})
    db.commit()


def _capture(db, *, version="p1", effective_from=JAN, organization_id=ORG,
             counts_toward_week=True, netting_mode="NONE", grace_minutes=0,
             netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
             **kw):
    row = capture_policy_version(
        db, organization_id=organization_id, policy_version=version,
        effective_from=effective_from, counts_toward_week=counts_toward_week,
        netting_mode=netting_mode, grace_minutes=grace_minutes,
        netting_decision_provenance=netting_decision_provenance,
        captured_by="owner@policy.test", **kw)
    db.commit()
    return row


# ══════════════════════════════════════════════════════════════════════════════
# 1. Creation and required fields
# ══════════════════════════════════════════════════════════════════════════════
class TestCapture:
    def test_a_policy_is_captured_with_its_five_attributes(self, db):
        row = _capture(db, netting_mode="WEEKLY_BANK", grace_minutes=5,
                       counts_toward_week=False,
                       break_treatment_ref="bt-1", rounding_profile_ref="rp-1")
        assert row.id is not None
        assert (row.counts_toward_week, row.netting_mode, row.grace_minutes) == \
            (False, "WEEKLY_BANK", 5)
        assert (row.break_treatment_ref, row.rounding_profile_ref) == \
            ("bt-1", "rp-1")
        assert row.captured_at_utc is not None
        assert row.content_hash

    def test_the_two_profile_references_are_optional(self, db):
        """Both referenced artifacts are DEFERRED, so nothing can supply one.

        A NOT NULL column would force a placeholder, and a placeholder is an
        invented profile.
        """
        row = _capture(db)
        assert row.break_treatment_ref is None
        assert row.rounding_profile_ref is None

    @pytest.mark.parametrize("field,value,message", [
        ("policy_version", "  ", "non-empty key"),
        ("captured_by", "", "non-empty label"),
    ])
    def test_required_text_is_refused_when_blank(self, db, field, value, message):
        kw = dict(organization_id=ORG, policy_version="p1",
                  effective_from=JAN, counts_toward_week=True,
                  netting_mode="NONE", grace_minutes=0,
                  netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                  captured_by="owner@policy.test")
        kw[field] = value
        with pytest.raises(InvalidPolicyVersion, match=message):
            capture_policy_version(db, **kw)

    def test_a_rejected_capture_writes_nothing(self, db):
        with pytest.raises(InvalidPolicyVersion):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="SOMETHING_ELSE", grace_minutes=0,
                netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                captured_by="owner@policy.test")
        db.rollback()
        assert db.query(PolicyVersion).count() == 0

    def test_a_version_key_cannot_be_reused(self, db):
        _capture(db, version="p1", effective_from=JAN)
        with pytest.raises(PolicyVersionCollision, match="already captured"):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JUN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=0,
                netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                captured_by="owner@policy.test")

    def test_two_versions_cannot_start_on_the_same_date(self, db):
        """"Which policy governed that day" must have exactly one answer."""
        _capture(db, version="p1", effective_from=JAN)
        with pytest.raises(PolicyVersionCollision, match="cannot start on the same date"):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p2",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=0,
                netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                captured_by="owner@policy.test")

    def test_another_tenant_is_unaffected(self, db):
        _capture(db, version="p1", effective_from=JAN)
        other = _capture(db, version="p1", effective_from=JAN,
                         organization_id=OTHER_ORG)
        assert other.organization_id == OTHER_ORG


# ══════════════════════════════════════════════════════════════════════════════
# 2. Governed values — fail closed, never normalise
# ══════════════════════════════════════════════════════════════════════════════
class TestGovernedValues:
    def test_the_netting_vocabulary_is_exactly_three(self):
        assert NETTING_MODES == {"NONE", "WEEKLY_BANK", "PERIOD_BANK"}
        assert {m.value for m in NettingMode} == NETTING_MODES

    @pytest.mark.parametrize("mode", sorted(NETTING_MODES))
    def test_every_governed_mode_is_storable(self, db, mode):
        row = _capture(db, version=f"p-{mode}",
                       effective_from=JAN + timedelta(days=len(mode)),
                       netting_mode=mode)
        assert row.netting_mode == mode

    @pytest.mark.parametrize("bad", ["none", "weekly_bank", "WEEKLY-BANK",
                                     "", "BANK", None, 1])
    def test_an_unsupported_mode_is_refused_not_normalised(self, db, bad):
        with pytest.raises(InvalidPolicyVersion):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode=bad, grace_minutes=0,
                netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                captured_by="owner@policy.test")

    @pytest.mark.parametrize("bad", [-1, -60])
    def test_negative_grace_is_refused(self, db, bad):
        with pytest.raises(InvalidPolicyVersion, match=">= 0"):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=bad,
                netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                captured_by="owner@policy.test")

    def test_zero_grace_is_valid_and_must_be_explicit(self, db):
        """§11's default is 0, but the column carries no default: a capture
        that omitted it would otherwise look deliberate."""
        assert _capture(db, grace_minutes=0).grace_minutes == 0
        assert PolicyVersion.__table__.c.grace_minutes.default is None
        assert PolicyVersion.__table__.c.grace_minutes.server_default is None

    def test_counts_toward_week_must_be_an_explicit_bool(self, db):
        """Never inferred, and never a truthy integer that happens to work."""
        for bad in (1, 0, "true", None):
            with pytest.raises(InvalidPolicyVersion, match="explicit bool"):
                capture_policy_version(
                    db, organization_id=ORG, policy_version="p1",
                    effective_from=JAN, counts_toward_week=bad,
                    netting_mode="NONE", grace_minutes=0,
                    netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
                    captured_by="owner@policy.test")

    def test_the_database_refuses_an_ungoverned_mode_too(self, db):
        """The service refuses first; the CHECK is what stops a path that
        never goes through the service."""
        db.add(PolicyVersion(
            organization_id=ORG, policy_version="raw", effective_from=JAN,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            captured_by="x", counts_toward_week=True,
            netting_mode="INVENTED", grace_minutes=0, content_hash="0" * 64))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_the_database_refuses_negative_grace_too(self, db):
        db.add(PolicyVersion(
            organization_id=ORG, policy_version="raw", effective_from=JAN,
            captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            captured_by="x", counts_toward_week=True,
            netting_mode="NONE", grace_minutes=-1, content_hash="0" * 64))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Immutability — ORM and, decisively, the database
# ══════════════════════════════════════════════════════════════════════════════
class TestImmutability:
    def test_the_orm_refuses_an_update(self, db):
        row = _capture(db)
        row.grace_minutes = 99
        with pytest.raises(PolicyVersionImmutable, match="cannot be modified"):
            db.flush()
        db.rollback()

    def test_the_orm_refuses_a_delete(self, db):
        row = _capture(db)
        db.delete(row)
        with pytest.raises(PolicyVersionImmutable, match="cannot be deleted"):
            db.flush()
        db.rollback()

    def test_the_database_refuses_a_core_update(self, db):
        """The ORM listener never sees this. Only the trigger can refuse it."""
        _capture(db)
        with pytest.raises(sa.exc.DatabaseError, match="append-only"):
            db.execute(sa.update(PolicyVersion.__table__)
                       .values(grace_minutes=99))
        db.rollback()

    def test_the_database_refuses_a_core_delete(self, db):
        _capture(db)
        with pytest.raises(sa.exc.DatabaseError, match="append-only"):
            db.execute(sa.delete(PolicyVersion.__table__))
        db.rollback()

    def test_the_row_survives_every_refused_attempt(self, db):
        row = _capture(db, grace_minutes=7)
        for attempt in (
            lambda: db.execute(sa.update(PolicyVersion.__table__)
                               .values(grace_minutes=99)),
            lambda: db.execute(sa.delete(PolicyVersion.__table__)),
        ):
            with pytest.raises(sa.exc.DatabaseError):
                attempt()
            db.rollback()
        db.expire_all()
        survivor = db.query(PolicyVersion).one()
        assert (survivor.id, survivor.grace_minutes) == (row.id, 7)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Historical resolution
# ══════════════════════════════════════════════════════════════════════════════
class TestResolution:
    def test_the_latest_version_at_or_before_the_date_wins(self, db):
        _capture(db, version="p1", effective_from=JAN, grace_minutes=1)
        _capture(db, version="p2", effective_from=JUN, grace_minutes=2)

        for on, expected in ((JAN, "p1"), (JAN + timedelta(days=1), "p1"),
                             (JUN - timedelta(days=1), "p1"),
                             (JUN, "p2"), (date(2026, 12, 31), "p2")):
            r = resolve_policy_as_of(db, organization_id=ORG, as_of=on)
            assert r.is_resolved and r.policy_version == expected, on

    def test_a_future_version_is_invisible_before_it_takes_effect(self, db):
        _capture(db, version="p1", effective_from=JAN)
        _capture(db, version="future", effective_from=date(2027, 1, 1))
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=JUN)
        assert r.policy_version == "p1"

    def test_below_the_first_version_fails_closed(self, db):
        _capture(db, version="p1", effective_from=JUN)
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=JAN)
        assert r.outcome is PolicyOutcome.NO_VERSION
        assert r.is_unresolved
        assert r.policy_version is None and r.netting_mode is None

    def test_no_history_at_all_fails_closed(self, db):
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=JAN)
        assert r.outcome is PolicyOutcome.NO_VERSION

    def test_another_tenants_policy_is_never_returned(self, db):
        _capture(db, version="p1", effective_from=JAN,
                 organization_id=OTHER_ORG)
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=JUN)
        assert r.outcome is PolicyOutcome.NO_VERSION

    def test_the_constraint_makes_two_versions_on_one_date_unstorable(self, db):
        """The PRIMARY defence: ambiguity cannot arise through any path that
        reaches the database with the constraint in place."""
        _capture(db, version="p1", effective_from=JAN)
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.insert(PolicyVersion.__table__).values(
                organization_id=ORG, policy_version="p2", effective_from=JAN,
                captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
                captured_by="x", counts_toward_week=True, netting_mode="NONE",
                grace_minutes=0, content_hash="0" * 64))
        db.rollback()

    def test_two_versions_on_one_date_fail_closed(self, unconstrained):
        """The SECOND defence, exercised against a table built WITHOUT the
        unique constraints.

        The constraint above is what actually prevents this, which is exactly
        why the resolver branch needs its own fixture: a database that somehow
        held both rows must refuse rather than pick one, and a test that
        relied on the constraint being absent in production would be testing
        nothing.
        """
        _raw_insert(unconstrained, policy_version="p1", effective_from=JAN)
        _raw_insert(unconstrained, policy_version="p2", effective_from=JAN)
        r = resolve_policy_as_of(unconstrained, organization_id=ORG, as_of=JUN)
        assert r.outcome is PolicyOutcome.AMBIGUOUS
        assert r.is_unresolved
        assert "effective_from" in r.invalid_reason
        assert r.policy_version is None

    def test_an_ungoverned_stored_value_fails_closed(self, unconstrained):
        """A row that reached the table without passing the service or the
        CHECK. Refused, never corrected — guessing what it meant would be
        inventing policy."""
        _raw_insert(unconstrained, policy_version="p1", effective_from=JAN,
                    netting_mode="INVENTED")
        r = resolve_policy_as_of(unconstrained, organization_id=ORG, as_of=JUN)
        assert r.outcome is PolicyOutcome.INVALID
        assert "INVENTED" in r.invalid_reason
        assert r.netting_mode is None

    def test_a_negative_stored_grace_fails_closed(self, unconstrained):
        _raw_insert(unconstrained, policy_version="p1", effective_from=JAN,
                    grace_minutes=-5)
        r = resolve_policy_as_of(unconstrained, organization_id=ORG, as_of=JUN)
        assert r.outcome is PolicyOutcome.INVALID
        assert "grace_minutes" in r.invalid_reason

    def test_a_resolved_answer_carries_every_owned_attribute(self, db):
        _capture(db, version="p1", effective_from=JAN,
                 counts_toward_week=False, netting_mode="PERIOD_BANK",
                 grace_minutes=9, break_treatment_ref="bt",
                 rounding_profile_ref="rp")
        r = resolve_policy_as_of(db, organization_id=ORG, as_of=JUN)
        assert (r.counts_toward_week, r.netting_mode, r.grace_minutes) == \
            (False, "PERIOD_BANK", 9)
        assert (r.break_treatment_ref, r.rounding_profile_ref) == ("bt", "rp")
        assert r.effective_from == JAN and r.content_hash

    def test_an_unresolved_answer_structurally_cannot_carry_a_value(self):
        with pytest.raises(InvalidPolicyVersion, match="cannot carry"):
            PolicyResolution(outcome=PolicyOutcome.NO_VERSION,
                             organization_id=ORG, as_of=JAN,
                             netting_mode="NONE")

    def test_a_resolved_answer_requires_its_values(self):
        with pytest.raises(InvalidPolicyVersion, match="RESOLVED requires"):
            PolicyResolution(outcome=PolicyOutcome.RESOLVED,
                             organization_id=ORG, as_of=JAN)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Content hash
# ══════════════════════════════════════════════════════════════════════════════
class TestContentHash:
    BASE = dict(organization_id=ORG, policy_version="p1", effective_from=JAN,
                counts_toward_week=True, break_treatment_ref=None,
                netting_mode="NONE", rounding_profile_ref=None,

                grace_minutes=0,
                # D-6.8 / D-7.1: None is the base case for all three, and
                # it is a REAL value in the digest, not an omission.
                surplus_cap_per_day_minutes=None,
                surplus_cap_per_period_minutes=None,
                retroactive_horizon_days=None)

    def test_it_is_deterministic(self):
        assert compute_policy_content_hash(**self.BASE) == \
            compute_policy_content_hash(**self.BASE)

    @pytest.mark.parametrize("field,value", [
        ("organization_id", OTHER_ORG),
        ("policy_version", "p2"),
        ("effective_from", JUN),
        ("counts_toward_week", False),
        ("break_treatment_ref", "bt"),
        ("netting_mode", "WEEKLY_BANK"),
        ("rounding_profile_ref", "rp"),
        ("grace_minutes", 1),
        # D-6.8: each cap moves the digest independently, so a policy
        # whose per-day cap changed cannot be mistaken for one whose
        # per-period cap did.
        ("surplus_cap_per_day_minutes", 60),
        ("surplus_cap_per_period_minutes", 240),
        # D-7.1.
        ("retroactive_horizon_days", 90),
    ])
    def test_changing_any_owned_attribute_changes_the_hash(self, field, value):
        other = dict(self.BASE)
        other[field] = value
        assert compute_policy_content_hash(**other) != \
            compute_policy_content_hash(**self.BASE)

    def test_provenance_is_outside_the_hash(self, db):
        """Two identical policies captured by different people at different
        times must be recognisable as the same configuration."""
        a = capture_policy_version(
            db, organization_id=ORG, policy_version="p1", effective_from=JAN,
            counts_toward_week=True, netting_mode="NONE", grace_minutes=0,
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            captured_by="alice@t.test",
            captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC))
        b = capture_policy_version(
            db, organization_id=OTHER_ORG, policy_version="p1",
            effective_from=JAN, counts_toward_week=True, netting_mode="NONE",
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            grace_minutes=0, captured_by="bob@t.test",
            captured_at_utc=datetime(2026, 9, 9, tzinfo=UTC))
        db.commit()
        # Same rules, different tenant -> identity differs, so the hashes do.
        assert a.content_hash != b.content_hash
        # But provenance alone does not move it:
        assert a.content_hash == compute_policy_content_hash(
            organization_id=ORG, policy_version="p1", effective_from=JAN,
            counts_toward_week=True, break_treatment_ref=None,
            netting_mode="NONE", rounding_profile_ref=None, grace_minutes=0,

            surplus_cap_per_day_minutes=None,
            surplus_cap_per_period_minutes=None,
            # The capture above stated no horizon, so it recorded §21.5's
            # default — and the digest has to be computed against what was
            # RECORDED, not against the omission.
            retroactive_horizon_days=DEFAULT_RETROACTIVE_HORIZON_DAYS)

    def test_the_stored_hash_matches_an_independent_calculation(self, db):
        """Transcribed, not imported: importing the implementation would let a
        wrong digest agree with itself."""
        import hashlib

        row = _capture(db, netting_mode="WEEKLY_BANK", grace_minutes=5,
                       counts_toward_week=False, break_treatment_ref="bt")
        payload = {
            "organization_id": ORG,
            "policy_version": "p1",
            "effective_from": JAN.isoformat(),
            "counts_toward_week": False,
            "break_treatment_ref": "bt",
            "netting_mode": "WEEKLY_BANK",
            "rounding_profile_ref": None,
            "grace_minutes": 5,
            # D-6.8 / D-7.1, transcribed by hand like the rest. The
            # capture states no caps, so both are null; it states no
            # horizon, so 90 is what got RECORDED.
            "surplus_cap_per_day_minutes": None,
            "surplus_cap_per_period_minutes": None,
            "retroactive_horizon_days": 90,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True)
        assert row.content_hash == hashlib.sha256(
            encoded.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# 6. The boundaries this artifact must not cross
# ══════════════════════════════════════════════════════════════════════════════
class TestOwnershipBoundaries:
    def test_daily_minutes_stays_in_config_value(self):
        """Duplicating it here would create a second answer to one question,
        and GV-27's classification scoping hangs off that row."""
        from app.config_versioning.adapter import REQUIREMENT_KEY
        from app.config_versioning.models import ConfigValue

        assert REQUIREMENT_KEY == "required_minutes"
        assert "value_minutes" in ConfigValue.__table__.c
        assert "worker_classification" in ConfigValue.__table__.c
        for banned in ("daily_minutes", "required_minutes", "value_minutes"):
            assert banned not in PolicyVersion.__table__.c, banned

    def test_is_working_day_stays_on_the_ledger(self):
        """Frozen at seal by D-18D so a later roster edit cannot change what a
        sealed day was measured against."""
        from app.work_ledger.models import WorkDayLedger

        assert "is_working_day" in WorkDayLedger.__table__.c
        assert "is_working_day" not in PolicyVersion.__table__.c

    def test_it_owns_exactly_the_eight_ruled_attributes(self):
        """Five under the original ruling, three added by D-6.8 and D-7.1.

        Still an EXACT set, and still the point of the test: a ninth attribute
        appearing here means someone moved a decision onto the policy that no
        ruling put there.
        """
        owned = {c.name for c in PolicyVersion.__table__.c} - {
            "id", "organization_id", "policy_version", "effective_from",
            "captured_at_utc", "captured_by", "content_hash",
            # Owner ruling D5. Provenance about the CAPTURE -- HOW the netting
            # mode was selected -- not an attribute OF the policy, which is
            # why it sits beside `captured_by` here and is likewise excluded
            # from `compute_policy_content_hash`. Counting it as a ruled
            # attribute would make it the ninth, and it is not one.
            "netting_decision_provenance",
        }
        assert owned == {
            "counts_toward_week", "break_treatment_ref", "netting_mode",
            "rounding_profile_ref", "grace_minutes",
            # D-6.8: §15.1's two caps. Note there are exactly TWO — per day
            # and per period. A third cap would be a specification change.
            "surplus_cap_per_day_minutes", "surplus_cap_per_period_minutes",
            # D-7.1: §21.5's horizon.
            "retroactive_horizon_days",
        }

    def test_no_bank_balance_counter_or_accumulator_was_added(self):
        """§15.1 / INV-3: "the bank balance is itself a fold over day records,
        never a stored counter."

        The two caps are LIMITS, not quantities that accumulate. This asserts
        the distinction structurally, over the whole ECO surface rather than
        this table alone, because the tempting place to add a counter later is
        a new table rather than a new column here.
        """
        import ast
        import pathlib

        banned = ("bank_balance", "bank_counter", "bank_state",
                  "banked_minutes", "surplus_balance", "bank_accumulator")
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            # IDENTIFIERS, not substrings. MODEL-NETTING's module names
            # these things in prose precisely in order to forbid them, and a
            # text scan cannot tell a prohibition from a violation. Parsing
            # for real names is both narrower and stronger.
            tree = ast.parse(path.read_text(encoding="utf-8",
                                            errors="ignore"))
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
            for token in banned:
                if token in names:
                    offenders.append(f"{path.name}:{token}")
        assert offenders == [], (
            f"a stored bank artifact appeared; §15.1 forbids one: {offenders}")

        tables = {c.name for c in PolicyVersion.__table__.c}
        assert not any("balance" in c or "counter" in c for c in tables), tables

    def test_the_four_version_identities_stay_separate(self):
        """policy_version · config_version · ledger_version ·
        evaluation_generation are four different facts."""
        from app.eval_day.models import EcoDayRecord

        for column in ("policy_version", "config_version", "ledger_version",
                       "evaluation_generation"):
            assert column in EcoDayRecord.__table__.c, column
        # The day record's policy key is String(64); this artifact's key must
        # fit it, or a captured policy could not be recorded against a day.
        assert EcoDayRecord.__table__.c.policy_version.type.length == \
            PolicyVersion.__table__.c.policy_version.type.length

    def test_the_rounding_profile_artifact_is_still_deferred(self):
        """ONE of the two referenced artifacts now exists; the other does not.

        The claim narrowed when owner ruling P-3 authorised
        ``BreakTreatmentProfile`` — the §13.2 artifact this package's
        ``break_treatment_ref`` was always a pointer to. It lives in
        ``app.break_treatment``, NOT here, which is the boundary this class
        actually protects: POLICY-VERSION records WHICH rule was in force and
        still defines none of them. ``test_the_references_are_opaque`` below
        is the other half and is unchanged.

        The rounding profile remains deferred, so its guard stands as written.
        """
        tables = set(Base.metadata.tables)
        for still_deferred in ("rounding_profile", "policy_rounding_profile"):
            assert still_deferred not in tables, still_deferred

        # The break-treatment artifact exists, and it is NOT owned here.
        assert "break_treatment_profile" in tables
        assert not any(
            t.startswith("policy_") and "break" in t for t in tables), (
            "a break-treatment table appeared under POLICY-VERSION's ownership")

    def test_the_references_are_opaque(self):
        """Neither column may encode a profile's definition."""
        src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(PACKAGE.glob("*.py")))
        for banned in ("rest_qualifies", "meal_qualifies", "cap_seconds",
                       "rest_qualifying_cap", "rounding_profile_minutes",
                       "round_half_up", "BRK_M", "BRK_R"):
            assert banned not in src, f"policy_version encodes {banned!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. Not wired — pinned so the next increment removes a guard deliberately
# ══════════════════════════════════════════════════════════════════════════════
class TestNotWired:
    #: The modules authorized to consume this one. EVAL-DAY's recording
    #: boundary, which owns the session and is where §11's ``evaluateDay`` step
    #: 1 belongs — and, since MODEL-NETTING, EVAL-DAY's netting fold, which
    #: needs §15's `NettingMode` vocabulary to know which modes bank.
    #:
    #: Both live inside EVAL-DAY's recording path; nothing else in the
    #: application may reach the package, and a THIRD consumer still fails.
    _AUTHORIZED_CONSUMERS = frozenset({
        "app/eval_day/recording.py",
        "app/eval_day/netting.py",
        # MODEL-HORIZON resolves the §21.5 horizon governing a corrected
        # workweek. It reads ONE attribute and writes nothing; the horizon is
        # versioned policy content and has no other source (D-7.1).
        "app/eval_event/horizon.py",
        # The §44.3 enablement-gate assessment. Gate 3 is "Netting decision
        # made explicitly", and PolicyVersion is where a Tenant's netting mode
        # is recorded -- there is no other source. It READS one resolution and
        # writes nothing: tests/test_enablement_gates.py instruments the engine
        # and fails on any INSERT, UPDATE or DELETE the assessment emits.
        "app/enablement_gates/service.py",
    })
    #: The one that threads policy ATTRIBUTES into an evaluation.
    _AUTHORIZED_CONSUMER = "app/eval_day/recording.py"

    def test_only_the_authorized_consumer_uses_this_package(self):
        """This guard narrowed when the consumption seam was authorized.

        It previously asserted NOTHING consumed the package — the right claim
        while it was inert, because a consumer appearing without a ruling was
        the risk. One was then authorized, so the claim narrows rather than
        disappearing: every other module in ``app/`` is still held to the
        original rule.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "policy_version" in path.parts:
                continue
            if path.relative_to(BACKEND).as_posix() in self._AUTHORIZED_CONSUMERS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("app.policy_version"):
                        offenders.append(path.relative_to(BACKEND).as_posix())
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("app.policy_version"):
                            offenders.append(
                                path.relative_to(BACKEND).as_posix())
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_authorized_consumer_really_does_resolve_a_policy(self):
        """The other half: the exemption must not become dead licence."""
        body = (BACKEND / self._AUTHORIZED_CONSUMER).read_text(encoding="utf-8")
        assert "resolve_policy_as_of" in body
        assert "POLICY_UNRESOLVED" in body

    def test_only_the_authorized_policy_attributes_are_applied(self):
        """Two of the attributes now have behaviour: grace, and netting.

        MODEL-GRACE was authorised and built, then MODEL-NETTING; break
        treatment, rounding and ``counts_toward_week`` remain deferred, so the
        seam must still carry them as provenance and read none of them. The
        guard narrows by one name per authorised increment rather than being
        deleted — the three that stay deferred are the reason it exists.
        """
        body = (BACKEND / self._AUTHORIZED_CONSUMER).read_text(encoding="utf-8")
        for banned in ("counts_toward_week", "break_treatment_ref",
                       "rounding_profile_ref"):
            assert banned not in body, f"recording.py applies {banned}"
        # And the authorised ones really are threaded, from the SAME historical
        # resolution as the identity — not from a second lookup or a default.
        assert "grace_minutes=policy.grace_minutes" in body
        assert "netting_mode=policy.netting_mode" in body
        # MODEL-NETTING's caps come from that same resolution too. A cap read
        # from anywhere else would be a bound the governing policy did not set.
        assert "cap_per_day_minutes=policy.surplus_cap_per_day_minutes" in body
        assert ("cap_per_period_minutes=policy.surplus_cap_per_period_minutes"
                in body)

    def test_this_package_imports_no_evaluator(self):
        """It records which rule was in force; it applies none of them."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = (node.module if isinstance(node, ast.ImportFrom)
                          else None)
                if module:
                    for banned in ("eval_day", "eval_week", "eval_event",
                                   "alert", "work_ledger", "rollout",
                                   "schedule_policy", "attendance_policy"):
                        assert banned not in module, f"{path.name} -> {module}"

    def test_no_behaviour_was_implemented(self):
        """Netting, grace, rounding and break classification are each their own
        increment; this one records the choice and applies none of it."""
        src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(PACKAGE.glob("*.py")))
        for banned in ("bankCredit", "bank_credit", "attributable",
                       "qualifying_minutes", "roundMinutes"):
            assert banned not in src, f"policy_version implements {banned!r}"

    def test_gv25_was_promoted_on_this_artifact(self):
        """This guard inverted when GV-25 was promoted.

        It asserted GV-25 was still blocked while this package was inert — the
        right claim then, because promoting the vector the package exists to
        unblock would have been marking its own homework. The consumption seam
        and a real execution harness came first; GV-25 was promoted on
        2026-09-18 and now carries no blocker.
        """
        data = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))
        gv25 = [v for v in data["vectors"] if v["id"] == "GV-25"][0]
        assert gv25["harness"]["execution"] == "executable"
        assert gv25["harness"]["blocking_dependencies"] == []

    def test_the_promoted_vectors_are_untouched(self):
        data = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))
        by = {v["id"]: v["harness"] for v in data["vectors"]}
        for gid in ("GV-17", "GV-27"):
            assert by[gid]["execution"] == "executable", gid
            assert by[gid]["blocking_dependencies"] == [], gid
        assert sum(1 for h in by.values()
                   if h["execution"] == "executable") == 28


# ══════════════════════════════════════════════════════════════════════════════
# 8. The migration
# ══════════════════════════════════════════════════════════════════════════════
class TestMigration:
    MIGRATION = (BACKEND / "alembic" / "versions"
                 / "b4e8c2a95d17_policy_version.py")

    #: Revisions AFTER the creating one that also change ``policy_version``.
    #: Named rather than loosening the parity comparison below: an unexplained
    #: extra column still fails.
    LATER_MIGRATIONS = (
        BACKEND / "alembic" / "versions"
        / "a3c8e5f10b47_policy_surplus_caps_and_horizon.py",
        BACKEND / "alembic" / "versions"
        / "f1c6b28d5a73_eco_compliance_evidence_and_jurisdiction.py",
    )

    def test_the_graph_has_one_base_and_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # Tip moved to D-6.8/D-7.1's a3c8e5f10b47, an ordinary
        # single-parent child of D-1.1's f4a7c2b91e68.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_it_is_an_ordinary_child_of_the_previous_head(self):
        tree = ast.parse(self.MIGRATION.read_text(encoding="utf-8"))
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", None) in ("revision", "down_revision"):
                        constants[t.id] = ast.literal_eval(node.value)
        assert constants["revision"] == "b4e8c2a95d17"
        assert constants["down_revision"] == "a9d3f6b21c58"

    def test_it_creates_one_table_and_alters_nothing(self):
        """Additive: no existing table is touched and no row is written."""
        tree = ast.parse(self.MIGRATION.read_text(encoding="utf-8"))
        created, banned = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "create_table":
                    arg = node.args[0]
                    created.append(getattr(arg, "value", None)
                                   or getattr(arg, "id", None))
                if node.func.attr in ("add_column", "alter_column",
                                      "drop_column", "bulk_insert"):
                    banned.append(node.func.attr)
        assert created == ["POLICY_TABLE"]
        assert banned == [], banned

    def test_the_executed_schema_matches_the_orm_model(self):
        """Run it, rather than reading it: the trigger SQL and the CHECK
        constraints are strings a static check would not catch if malformed."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        import importlib.util

        def _load(path):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        # The creating revision and every later one that alters this table,
        # applied in graph order, so the comparison is against what a real
        # database ends up with rather than a snapshot of the first revision.
        for path in (self.MIGRATION, *self.LATER_MIGRATIONS):
            module = _load(path)
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()

        actual = sa.inspect(eng)
        assert "policy_version" in actual.get_table_names()
        assert sorted(c["name"] for c in actual.get_columns("policy_version")) \
            == sorted(c.name for c in PolicyVersion.__table__.columns)
        assert {c["name"]: c["nullable"]
                for c in actual.get_columns("policy_version")} == \
            {c.name: c.nullable for c in PolicyVersion.__table__.columns}

    def test_the_migrated_schema_is_append_only(self):
        """The triggers must exist in a MIGRATED database, not only in a
        create_all one — otherwise every immutability test above passes
        vacuously against a shape production does not have."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mig_policy2", self.MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()

        with eng.connect() as conn:
            triggers = {r[0] for r in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"))}
            assert {"policy_version_no_update",
                    "policy_version_no_delete"} <= triggers

            conn.execute(sa.text(
                "INSERT INTO policy_version (organization_id, policy_version, "
                "effective_from, captured_at_utc, captured_by, "
                "counts_toward_week, netting_mode, grace_minutes, content_hash)"
                " VALUES (1, 'p1', '2026-01-01', '2026-01-01 00:00:00', 'x', "
                "1, 'NONE', 0, 'h')"))
            for statement in (
                "UPDATE policy_version SET grace_minutes = 5",
                "DELETE FROM policy_version",
            ):
                with pytest.raises(sa.exc.DatabaseError, match="append-only"):
                    conn.execute(sa.text(statement))


# ══════════════════════════════════════════════════════════════════════════════
# 9. Netting-decision provenance (owner rulings D5, OD-02: A, F: D)
# ══════════════════════════════════════════════════════════════════════════════
class TestNettingProvenance:
    """§15: "Tenant onboarding MUST surface this choice explicitly as a
    required decision, never as a silently-accepted default."

    A netting mode with no record of HOW it was chosen cannot distinguish a
    Tenant that decided from a Tenant that was defaulted. That is the whole
    point of the column, and it is why the capture refuses without one.
    """

    def test_a_new_capture_requires_it(self, db):
        """Not optional, not defaulted. Missing is a TypeError from the
        signature itself, before any validation runs."""
        with pytest.raises(TypeError):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=0,
                captured_by="owner@policy.test")

    def test_it_is_recorded_on_the_row(self, db):
        row = _capture(db)
        assert row.netting_decision_provenance == "EXPLICIT_TENANT_CHOICE"

    @pytest.mark.parametrize("value", ["", "  ", "DEFAULT", "ASSUMED",
                                       "explicit_tenant_choice", "UNKNOWN"])
    def test_an_ungoverned_value_is_refused(self, db, value):
        """A silently-accepted default is exactly what §15 forbids, so
        "DEFAULT" must not be sayable."""
        with pytest.raises(InvalidPolicyVersion):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=0,
                netting_decision_provenance=value,
                captured_by="owner@policy.test")

    def test_a_refused_provenance_writes_nothing(self, db):
        with pytest.raises(InvalidPolicyVersion):
            capture_policy_version(
                db, organization_id=ORG, policy_version="p1",
                effective_from=JAN, counts_toward_week=True,
                netting_mode="NONE", grace_minutes=0,
                netting_decision_provenance="DEFAULT",
                captured_by="owner@policy.test")
        db.rollback()
        assert db.query(PolicyVersion).count() == 0

    def test_the_governed_set_is_exactly_one_value(self):
        """Owner ruling D5 authorized ONE provenance. A second value is a
        specification change, not a code change."""
        from app.policy_version import NETTING_DECISION_PROVENANCES

        assert set(NETTING_DECISION_PROVENANCES) == {"EXPLICIT_TENANT_CHOICE"}

    def test_it_is_not_in_the_content_hash(self, db):
        """``compute_policy_content_hash`` excludes ``captured_at_utc`` and
        ``captured_by`` because they are "provenance about the capture, not
        the policy... two Tenants with the same rules would be unable to
        recognise that they match". HOW a netting mode was selected is the
        same class of fact, so hashing it would make two identical policies
        hash differently — and would invalidate every hash already stored.
        """
        import inspect

        source = inspect.getsource(compute_policy_content_hash)
        assert "netting_decision_provenance" not in source

        row = _capture(db)
        recomputed = compute_policy_content_hash(
            organization_id=row.organization_id,
            policy_version=row.policy_version,
            effective_from=row.effective_from,
            counts_toward_week=row.counts_toward_week,
            netting_mode=row.netting_mode,
            grace_minutes=row.grace_minutes,
            break_treatment_ref=row.break_treatment_ref,
            rounding_profile_ref=row.rounding_profile_ref,
            surplus_cap_per_day_minutes=row.surplus_cap_per_day_minutes,
            surplus_cap_per_period_minutes=row.surplus_cap_per_period_minutes,
            retroactive_horizon_days=row.retroactive_horizon_days)
        assert recomputed == row.content_hash

    def test_the_column_is_nullable_and_nothing_back_fills_it(self):
        """Owner ruling F: D. ``policy_version`` is append-only with database
        triggers, so a row captured before the column existed can NEVER be
        back-filled. NULL is the grandfathered historical state, and the
        requirement binds on the next capture."""
        column = PolicyVersion.__table__.c.netting_decision_provenance
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None

        source = self.MIGRATION_TEXT
        assert "UPDATE policy_version" not in source
        tree = ast.parse(source)
        called = {getattr(n.func, "attr", None) for n in ast.walk(tree)
                  if isinstance(n, ast.Call)}
        assert "bulk_insert" not in called

    MIGRATION_TEXT = (
        BACKEND / "alembic" / "versions"
        / "f1c6b28d5a73_eco_compliance_evidence_and_jurisdiction.py"
    ).read_text(encoding="utf-8")

    def test_the_sqlite_check_gap_is_stated_rather_than_hidden(self):
        """SQLite cannot ADD a CHECK to an existing table without rebuilding
        it, and rebuilding ``policy_version`` would drop its append-only
        triggers and rewrite immutable rows. So the constraint is added on
        PostgreSQL only, and the SQLite gap is covered by the model (which
        every create_all schema carries) and by the capture, which refuses an
        ungoverned value before the session is touched.

        Asserted so the limitation is a known one rather than a discovery.
        """
        assert "postgresql" in self.MIGRATION_TEXT
        checks = [c for c in PolicyVersion.__table__.constraints
                  if isinstance(c, sa.CheckConstraint)
                  and "netting_decision_provenance" in c.sqltext.text]
        assert len(checks) == 1, "the model must still carry the CHECK"
