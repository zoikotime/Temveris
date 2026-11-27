"""
W4 — canonical workweek-key derivation.
=======================================

The property under test: one derivation, honouring the organization's configured
week start, fixed at seal time and never moved afterwards.

Expected keys are written out as literal dates a reader can check against a
calendar, never computed by calling the function under test.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.work_ledger import (
    CandidateInterval,
    DayKey,
    LedgerState,
    Provenance,
    WorkDayLedger,
    WorkState,
    WorkStateInterval,
    seal_day,
)
from app.work_ledger.workweek import (
    WorkweekBoundaryMalformed,
    WorkweekBoundaryUnset,
    canonical_workweek_key,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: 2026-08-31 is a Monday; 2026-09-06 the Sunday that ends that Monday-week.
MON = date(2026, 8, 31)
TUE = date(2026, 9, 1)
SAT = date(2026, 9, 5)
SUN = date(2026, 9, 6)
NEXT_MON = date(2026, 9, 7)


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import AppSettings, Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    AppSettings.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)


_org = [7700]


def _new_org(db, week_start=None):
    """An organization, optionally with a configured week start."""
    from database.models import AppSettings

    _org[0] += 1
    oid = _org[0]
    if week_start is not None:
        db.add(AppSettings(organization_id=oid,
                           key="schedule.week_start_day", value=week_start))
        db.flush()
    return oid


def _set_week_start(db, oid, value):
    from database.models import AppSettings
    import sqlalchemy as sa

    row = db.execute(
        sa.select(AppSettings).where(
            AppSettings.organization_id == oid,
            AppSettings.key == "schedule.week_start_day")
    ).scalars().first()
    if row is None:
        db.add(AppSettings(organization_id=oid,
                           key="schedule.week_start_day", value=value))
    else:
        row.value = value
    db.flush()


def _intervals(on):
    begin = datetime(on.year, on.month, on.day, 9, 0, tzinfo=UTC)
    return [CandidateInterval(
        seq=1, state=WorkState.WRK.value, start_at_utc=begin,
        end_at_utc=begin + timedelta(hours=8),
        provenance=Provenance.WORKER.value)]


# ═════════════════════════════════════════════════════════════════════════════
# 1-2. Configured week start
# ═════════════════════════════════════════════════════════════════════════════
class TestConfiguredWeekStart:
    def test_an_undefined_boundary_is_refused_not_defaulted(self, db):
        """No setting configured -> refuse.

        This test previously asserted the opposite: that an unconfigured
        organization silently got MONDAY, "the documented system default".
        Owner Ruling 5 (F) overturned that claim for the ECO path, and §12.1
        is why — "Every Tenant MUST have a defined workweek before this
        control can be enabled". A day sealed under an assumed boundary would
        carry a week identity nobody chose, and the assumption would be
        invisible in the sealed row forever after.

        The lenient default still exists for the weekly rollups and pay-period
        windows that call ``schedule_policy.week_start_date()`` — see
        ``test_week_start_date_itself_is_unchanged``. It is this path, and only
        this path, that now refuses.
        """
        oid = _new_org(db)
        with pytest.raises(WorkweekBoundaryUnset):
            canonical_workweek_key(db, organization_id=oid, work_date=SAT)

    def test_explicit_monday_start(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(db, organization_id=oid, work_date=SAT) \
            == "2026-08-31"

    def test_sunday_start_gives_a_different_week(self, db):
        """The Gulf/US case the ISO format could not express."""
        oid = _new_org(db, "SUNDAY")
        assert canonical_workweek_key(db, organization_id=oid, work_date=SAT) \
            == "2026-08-30"

    def test_the_same_physical_day_differs_by_organization(self, db):
        mon_org = _new_org(db, "MONDAY")
        sun_org = _new_org(db, "SUNDAY")
        assert canonical_workweek_key(db, organization_id=mon_org, work_date=SAT) \
            != canonical_workweek_key(db, organization_id=sun_org, work_date=SAT)

    def test_a_malformed_setting_is_refused_not_defaulted(self, db):
        """Also inverted by Ruling 5 (F), for the same reason.

        A value the platform cannot read is a configuration defect. Coercing
        it to MONDAY produces a confident answer from a broken input, which is
        the one outcome a fail-closed contract exists to prevent.
        """
        oid = _new_org(db, "Someday")
        with pytest.raises(WorkweekBoundaryMalformed):
            canonical_workweek_key(db, organization_id=oid, work_date=SAT)

    def test_a_blank_setting_is_refused(self, db):
        oid = _new_org(db, "   ")
        with pytest.raises(WorkweekBoundaryUnset):
            canonical_workweek_key(db, organization_id=oid, work_date=SAT)


# ═════════════════════════════════════════════════════════════════════════════
# 3, 5, 6. Boundaries and grouping
# ═════════════════════════════════════════════════════════════════════════════
class TestWeekGrouping:
    def test_every_day_of_a_monday_week_shares_one_key(self, db):
        oid = _new_org(db, "MONDAY")
        keys = {
            canonical_workweek_key(db, organization_id=oid,
                                   work_date=MON + timedelta(days=n))
            for n in range(7)
        }
        assert keys == {"2026-08-31"}

    def test_every_day_of_a_sunday_week_shares_one_key(self, db):
        oid = _new_org(db, "SUNDAY")
        keys = {
            canonical_workweek_key(db, organization_id=oid,
                                   work_date=date(2026, 8, 30) + timedelta(days=n))
            for n in range(7)
        }
        assert keys == {"2026-08-30"}

    def test_adjacent_weeks_differ(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(db, organization_id=oid, work_date=SUN) \
            != canonical_workweek_key(db, organization_id=oid, work_date=NEXT_MON)

    def test_the_boundary_falls_between_sunday_and_monday(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(db, organization_id=oid,
                                      work_date=SUN) == "2026-08-31"
        assert canonical_workweek_key(db, organization_id=oid,
                                      work_date=NEXT_MON) == "2026-09-07"

    def test_a_datetime_work_date_is_accepted(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(
            db, organization_id=oid,
            work_date=datetime(2026, 9, 5, 23, 30)) == "2026-08-31"

    def test_the_derivation_is_deterministic(self, db):
        oid = _new_org(db, "MONDAY")
        assert len({canonical_workweek_key(db, organization_id=oid, work_date=SAT)
                    for _ in range(5)}) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 4, 10. START_DATE attribution — GV-09
# ═════════════════════════════════════════════════════════════════════════════
class TestStartDateAttribution:
    def test_gv09_sunday_shift_accrues_to_the_week_containing_sunday(self, db):
        """Shift Sun 22:00 -> Mon 06:00, attribution START_DATE.

        The day is attributed to Sunday, so its week is the one containing
        Sunday — for a Monday-start organization, the week beginning 2026-08-31.
        Counted once (R-12.3.5), never in both weeks.
        """
        oid = _new_org(db, "MONDAY")
        sunday_key = canonical_workweek_key(db, organization_id=oid, work_date=SUN)
        monday_key = canonical_workweek_key(db, organization_id=oid,
                                            work_date=NEXT_MON)
        assert sunday_key == "2026-08-31"
        assert sunday_key != monday_key

    def test_a_cross_midnight_day_yields_exactly_one_key(self, db):
        """One work_date in, one key out — a shift cannot land in two weeks."""
        oid = _new_org(db, "MONDAY")
        key = canonical_workweek_key(db, organization_id=oid, work_date=SUN)
        assert isinstance(key, str)
        assert key == "2026-08-31"


# ═════════════════════════════════════════════════════════════════════════════
# 7. The historical anchor
# ═════════════════════════════════════════════════════════════════════════════
class TestHistoricalAnchor:
    def test_a_sealed_key_is_derived_not_supplied(self, db):
        oid = _new_org(db, "MONDAY")
        row = seal_day(db, DayKey(organization_id=oid, user_id=901, work_date=SAT),
                       intervals=_intervals(SAT), timezone_iana="Europe/London", is_working_day=True, working_day_source="org")
        assert row.workweek_key == "2026-08-31"

    def test_changing_the_setting_does_not_move_a_sealed_day(self, db):
        """W4-1: the persisted key is the historical identity, permanently."""
        oid = _new_org(db, "MONDAY")
        row = seal_day(db, DayKey(organization_id=oid, user_id=902, work_date=SAT),
                       intervals=_intervals(SAT), timezone_iana="Europe/London", is_working_day=True, working_day_source="org")
        assert row.workweek_key == "2026-08-31"

        _set_week_start(db, oid, "SUNDAY")
        db.refresh(row)
        assert row.workweek_key == "2026-08-31", "a sealed day was retroactively moved"
        # …while a day sealed AFTER the change adopts the new definition (W4-7).
        assert canonical_workweek_key(db, organization_id=oid,
                                      work_date=SAT) == "2026-08-30"

    def test_days_sealed_either_side_of_a_change_keep_what_they_were_given(self, db):
        oid = _new_org(db, "MONDAY")
        before = seal_day(
            db, DayKey(organization_id=oid, user_id=903, work_date=SAT),
            intervals=_intervals(SAT), timezone_iana="Europe/London", is_working_day=True, working_day_source="org")
        _set_week_start(db, oid, "SUNDAY")
        after = seal_day(
            db, DayKey(organization_id=oid, user_id=904, work_date=SAT),
            intervals=_intervals(SAT), timezone_iana="Europe/London", is_working_day=True, working_day_source="org")
        assert before.workweek_key == "2026-08-31"
        assert after.workweek_key == "2026-08-30"

    def test_the_caller_cannot_supply_a_workweek_key(self):
        """W4-4: the parameter is gone from the public service surface."""
        from app.work_ledger import record_integrity_fault, supersede_day

        for fn in (seal_day, supersede_day, record_integrity_fault):
            assert "workweek_key" not in inspect.signature(fn).parameters, fn.__name__

    def test_supplying_one_anyway_is_rejected(self, db):
        oid = _new_org(db, "MONDAY")
        with pytest.raises(TypeError):
            seal_day(db, DayKey(organization_id=oid, user_id=905, work_date=SAT),
                     intervals=_intervals(SAT), timezone_iana="Europe/London", is_working_day=True, working_day_source="org",
                     workweek_key="2026-W36")


# ═════════════════════════════════════════════════════════════════════════════
# 8. ISO week numbering is not the canonical key
# ═════════════════════════════════════════════════════════════════════════════
class TestNotIsoWeek:
    def test_the_key_is_a_date_not_an_iso_week_label(self, db):
        oid = _new_org(db, "MONDAY")
        key = canonical_workweek_key(db, organization_id=oid, work_date=SAT)
        assert "W" not in key
        assert key == date.fromisoformat(key).isoformat()

    def test_it_does_not_reproduce_isocalendar(self, db):
        """A Sunday-start organization is exactly where ISO would be wrong."""
        oid = _new_org(db, "SUNDAY")
        iso = f"{SAT.isocalendar()[0]}-W{SAT.isocalendar()[1]:02d}"
        assert canonical_workweek_key(db, organization_id=oid, work_date=SAT) != iso

    def test_no_eco_module_calls_isocalendar(self):
        """Scanned over attribute access, not prose.

        ``workweek.py``'s docstring names ``isocalendar`` precisely to record
        why it is not used; a text scan would fail on the documentation of its
        own rule.
        """
        for pkg in ("work_ledger", "eval_day", "eval_week"):
            for path in sorted((BACKEND / "app" / pkg).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                called = {
                    n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                }
                assert "isocalendar" not in called, path.name

    def test_the_key_fits_the_column(self):
        assert len("2026-08-31") <= WorkDayLedger.__table__.c.workweek_key.type.length


# ═════════════════════════════════════════════════════════════════════════════
# 9, 11. DST — GV-11
# ═════════════════════════════════════════════════════════════════════════════
class TestDaylightSaving:
    #: Europe/London autumn transition 2026 falls on Sunday 2026-10-25.
    TRANSITION = date(2026, 10, 25)
    WEEK_START = date(2026, 10, 19)      # the Monday before

    def test_gv11_the_transition_week_has_one_identity(self, db):
        """Every day of the DST week maps to one key: no day double-counted.

        The 169-hour property is asserted by the vector, not computed here —
        identity is date arithmetic and a clock change cannot shift it.
        """
        oid = _new_org(db, "MONDAY")
        keys = {
            canonical_workweek_key(db, organization_id=oid,
                                   work_date=self.WEEK_START + timedelta(days=n))
            for n in range(7)
        }
        assert keys == {"2026-10-19"}

    def test_the_transition_day_belongs_to_that_week(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(db, organization_id=oid,
                                      work_date=self.TRANSITION) == "2026-10-19"

    def test_the_day_after_the_transition_starts_a_new_week(self, db):
        oid = _new_org(db, "MONDAY")
        assert canonical_workweek_key(
            db, organization_id=oid,
            work_date=self.TRANSITION + timedelta(days=1)) == "2026-10-26"

    def test_no_week_duration_calculation_was_added(self):
        """The vector's 169 hours must not become a new weekly computation."""
        src = (BACKEND / "app" / "work_ledger" / "workweek.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        names = {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))
        }
        assert not any("hour" in n.lower() or "duration" in n.lower()
                       for n in names), names


# ═════════════════════════════════════════════════════════════════════════════
# Scope guards
# ═════════════════════════════════════════════════════════════════════════════
class TestScope:
    def test_no_new_table_was_introduced(self):
        src = (BACKEND / "app" / "work_ledger" / "workweek.py").read_text(
            encoding="utf-8")
        for banned in ("Base", "Column", "__tablename__", "op.create_table"):
            assert banned not in src

    def test_no_migration_was_added_for_w4(self):
        """W4-a needed no schema change: the column already existed.

        Asserted as "no revision touches the workweek column or the ledger"
        rather than by pinning the head, which moves whenever any later
        increment adds a revision for its own reasons — the W5 capture
        increment added ``e5f8c2a91d64`` for durable actor attribution on
        ``config_version``, which has nothing to do with W4. Pinning the head
        would have made this test fail for an unrelated change while proving
        nothing about W4.

        Not a text search for the word either: the Step 6 revision legitimately
        mentions ``workweek_key`` because it creates the column.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # The graph must still be linear; a fork is what would endanger W4.
        assert len(sd.get_heads()) == 1, sd.get_heads()
        assert len(sd.get_bases()) == 1

        # And no revision after Step 6 may OPERATE on the ledger or its week
        # column. Read from the ``op.*`` calls, not the file text: the Step 7
        # and EVAL-DAY revisions name work_day_ledger in their prose precisely
        # to say they leave it alone, and a text scan would fail on that.
        import ast

        versions = BACKEND / "alembic" / "versions"
        step6 = "d4b7e2f9a615_phase2_foundation_ledger_and_config.py"
        for path in sorted(versions.glob("*.py")):
            if path.name == step6:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))

            # A NEW table may declare a ``workweek_key`` column of its own —
            # EVAL-EVENT's ``eco_threshold_event`` does, because an event is
            # identified by the week it concerns. That is not the W4 column and
            # does not touch it. Only ``work_day_ledger``'s own week column is
            # what this guard protects, so columns declared inside the creation
            # of some OTHER table are exempt.
            exempt = set()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "op"
                        and node.func.attr == "create_table"
                        and node.args):
                    continue
                first = node.args[0]
                if (isinstance(first, ast.Constant)
                        and first.value == "work_day_ledger"):
                    continue
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call)
                            and getattr(inner.func, "attr", None) == "Column"
                            and inner.args
                            and isinstance(inner.args[0], ast.Constant)):
                        exempt.add(inner.args[0].value)

            targets = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # op.<operation>(...) — the first string argument is the table
                # or column being operated on.
                if (isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "op"):
                    targets.update(
                        a.value for a in node.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    )
                # sa.Column("name", ...) — a column being added.
                if (getattr(func, "attr", None) == "Column" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    targets.add(node.args[0].value)

            assert "work_day_ledger" not in targets, (
                f"{path.name} operates on work_day_ledger"
            )
            assert "workweek_key" not in (targets - exempt), (
                f"{path.name} operates on workweek_key"
            )

    def test_week_start_date_itself_is_unchanged(self):
        """W4-5: the current-config helper keeps its existing contract."""
        from app.schedule_policy import week_start_date

        params = list(inspect.signature(week_start_date).parameters)
        assert params == ["db", "org_id", "on_date"]

    def test_no_threshold_or_event_work_leaked_in(self):
        src = (BACKEND / "app" / "work_ledger" / "workweek.py").read_text(
            encoding="utf-8").lower()
        for banned in ("threshold", "tier", "crossing", "notif", "event_"):
            assert banned not in src
