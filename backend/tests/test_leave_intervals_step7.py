"""
STEP 7 — intraday leave foundation.
===================================

The property under test throughout is that intraday leave is an ADDITIVE
temporal record bound to an existing grant — never a second answer to "was this
Worker on leave".

Expected values are written out independently. Durations, dates and boundary
answers are stated as literals a reader can check by hand, not computed by
calling the implementation that is supposed to produce them.

ON FOREIGN-KEY ENFORCEMENT: the shared test engine leaves
``PRAGMA foreign_keys`` off, so SQLite does not enforce foreign keys there.
Rather than assert rules that would not run, the composite tenant key and the
RESTRICT on the parent grant are exercised on a dedicated engine with the pragma
enabled — see ``TestForeignKeysWhereEnforced``.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.leave_intervals import (
    CandidateLeaveInterval,
    LeaveInterval,
    LeaveIntervalFault,
    LeaveIntervalImmutable,
    LeaveIntervalIntegrityFault,
    LeaveIntervalOwnershipMismatch,
    LeaveIntervalSet,
    LeaveRequestNotTenanted,
    StaleLeaveIntervalVersion,
    attributed_work_date,
    current_version,
    intervals_for_day,
    read_intervals,
    record_leave_intervals,
    validate_leave_intervals,
    withdraw_leave_intervals,
)

UTC = timezone.utc
ORG = 21
OTHER_ORG = 22
USER = 601
OTHER_USER = 602

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "leave_intervals"
MIGRATION = (
    BACKEND / "alembic" / "versions" / "f1a6b9c4d283_intraday_leave_intervals.py"
)

EXPECTED_REVISION = "f1a6b9c4d283"
EXPECTED_PARENT_REVISION = "d4b7e2f9a615"

#: A fixed zone with no DST, so every expected local date below can be checked
#: by hand. DST behaviour is exercised separately with a real zone.
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module", autouse=True)
def _step7_schema(engine):
    from database.models import LeaveRequest, Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    LeaveRequest.__table__.create(engine, checkfirst=True)
    LeaveIntervalSet.__table__.create(engine, checkfirst=True)
    LeaveInterval.__table__.create(engine, checkfirst=True)


def _grant(db, *, org=ORG, user=USER, on=date(2026, 9, 4), days=1, **kw):
    """A leave request — the canonical grant intervals must hang off."""
    from database.models import LeaveRequest

    req = LeaveRequest(
        user_id=user,
        organization_id=org,
        leave_type="Annual Leave",
        from_date=datetime(on.year, on.month, on.day),
        to_date=datetime(on.year, on.month, on.day) + timedelta(days=days - 1),
        description="audit trail",
        status="approved",
        **kw,
    )
    db.add(req)
    db.flush()
    return req


def _iv(seq, start, end):
    return CandidateLeaveInterval(seq=seq, start_at_utc=start, end_at_utc=end)


def _at(hh, mm=0, day=4):
    return datetime(2026, 9, day, hh, mm, tzinfo=UTC)


def _record(db, req, intervals, *, expected=None, tz="Asia/Kolkata", zone=IST):
    return record_leave_intervals(
        db, leave_request=req, intervals=intervals,
        timezone_iana=tz, zone=zone, expected_version=expected,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Pure validation — no database
# ═════════════════════════════════════════════════════════════════════════════
class TestValidation:
    def test_a_clean_set_is_admissible(self):
        result = validate_leave_intervals([
            _iv(1, _at(9), _at(11)),
            _iv(2, _at(14), _at(16)),
        ])
        assert result.admissible
        assert result.violations == ()

    def test_an_empty_set_is_admissible(self):
        """Not an oversight: an empty set is how intraday leave is withdrawn."""
        assert validate_leave_intervals([]).admissible

    def test_zero_length_interval_is_refused(self):
        result = validate_leave_intervals([_iv(1, _at(9), _at(9))])
        assert not result.admissible
        assert [v.fault for v in result.violations] == [
            LeaveIntervalFault.NON_POSITIVE_DURATION
        ]

    def test_reversed_interval_is_refused(self):
        result = validate_leave_intervals([_iv(1, _at(11), _at(9))])
        assert [v.fault for v in result.violations] == [
            LeaveIntervalFault.NON_POSITIVE_DURATION
        ]

    @pytest.mark.parametrize("start,end", [
        (datetime(2026, 9, 4, 9), _at(11)),
        (_at(9), datetime(2026, 9, 4, 11)),
        (datetime(2026, 9, 4, 9), datetime(2026, 9, 4, 11)),
    ])
    def test_naive_timestamps_are_refused(self, start, end):
        """§16.4. A naive instant is not a true UTC instant, and assuming a
        zone is how a leave boundary silently moves by hours."""
        result = validate_leave_intervals([
            CandidateLeaveInterval(seq=1, start_at_utc=start, end_at_utc=end)
        ])
        assert LeaveIntervalFault.NAIVE_TIMESTAMP in {
            v.fault for v in result.violations
        }

    def test_overlapping_intervals_are_refused(self):
        result = validate_leave_intervals([
            _iv(1, _at(9), _at(12)),
            _iv(2, _at(11), _at(14)),
        ])
        assert [v.fault for v in result.violations] == [
            LeaveIntervalFault.OVERLAPPING_INTERVALS
        ]

    def test_touching_intervals_are_contiguous_not_overlapping(self):
        """Half-open [start, end). One ending exactly where the next begins is
        two adjacent blocks, which must remain recordable."""
        assert validate_leave_intervals([
            _iv(1, _at(9), _at(12)),
            _iv(2, _at(12), _at(15)),
        ]).admissible

    def test_overlap_is_detected_regardless_of_input_order(self):
        forward = validate_leave_intervals([
            _iv(1, _at(9), _at(12)), _iv(2, _at(11), _at(14)),
        ])
        backward = validate_leave_intervals([
            _iv(2, _at(11), _at(14)), _iv(1, _at(9), _at(12)),
        ])
        assert forward.admissible is backward.admissible is False

    def test_duplicate_sequence_is_refused(self):
        result = validate_leave_intervals([
            _iv(1, _at(9), _at(11)),
            _iv(1, _at(14), _at(16)),
        ])
        assert LeaveIntervalFault.DUPLICATE_SEQUENCE in {
            v.fault for v in result.violations
        }

    def test_non_positive_sequence_is_refused(self):
        result = validate_leave_intervals([_iv(0, _at(9), _at(11))])
        assert LeaveIntervalFault.NON_POSITIVE_SEQUENCE in {
            v.fault for v in result.violations
        }

    def test_every_violation_is_reported_not_only_the_first(self):
        """An operator fixing a bad import wants the whole list."""
        result = validate_leave_intervals([
            _iv(1, _at(11), _at(9)),          # reversed
            _iv(2, _at(14), _at(14)),         # zero length
            _iv(3, _at(16), _at(18)),
            _iv(4, _at(17), _at(19)),         # overlaps 3
        ])
        assert len(result.violations) == 3
        assert {v.fault for v in result.violations} == {
            LeaveIntervalFault.NON_POSITIVE_DURATION,
            LeaveIntervalFault.OVERLAPPING_INTERVALS,
        }


class TestAttribution:
    """START_DATE attribution — the ruling already in force for the ledger."""

    def test_an_ordinary_interval_takes_its_own_local_date(self):
        # 09:00 UTC on the 4th is 14:30 IST on the 4th.
        assert attributed_work_date(_at(9), IST) == date(2026, 9, 4)

    def test_a_cross_midnight_interval_belongs_to_the_date_it_began(self):
        # 20:00 UTC on the 4th is 01:30 IST on the 5th — the local date the
        # interval STARTS on, which is the 5th. Attribution follows the start.
        assert attributed_work_date(_at(20), IST) == date(2026, 9, 5)

    def test_local_date_is_the_organizations_not_utc(self):
        # 19:00 UTC on the 4th = 00:30 IST on the 5th. A UTC-based answer would
        # say the 4th; the organization's clock says the 5th.
        assert attributed_work_date(_at(19), IST) == date(2026, 9, 5)
        assert _at(19).date() == date(2026, 9, 4)   # the wrong answer, named

    def test_a_naive_start_has_no_local_date(self):
        with pytest.raises(ValueError):
            attributed_work_date(datetime(2026, 9, 4, 9), IST)


class TestDaylightSaving:
    """A real zone with a transition, not just the fixed offset used elsewhere.

    DST is where naive timestamps do their damage, so the two properties that
    matter are checked directly: duration is computed on true instants and is
    unaffected by the clock change, and the local date still comes from the
    organization's zone.
    """

    @staticmethod
    def _london():
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/London")

    def test_duration_across_a_spring_transition_is_real_elapsed_time(self):
        """British Summer Time begins 01:00 UTC on 2026-03-29.

        Local clocks jump 01:00 → 02:00, so the wall-clock span reads three
        hours. Only two hours actually elapsed, and that is what must be stored:
        a Worker is not on leave for an hour that did not happen.
        """
        from app.leave_intervals import duration_seconds

        start = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
        end = datetime(2026, 3, 29, 2, 30, tzinfo=UTC)
        assert duration_seconds(start, end) == 7200

    def test_local_date_across_a_transition_uses_the_organizations_zone(self):
        london = self._london()
        # 23:30 UTC on 2026-06-30 is 00:30 BST on 2026-07-01 — the next day
        # locally, while UTC still says June.
        instant = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
        assert attributed_work_date(instant, london) == date(2026, 7, 1)
        assert instant.date() == date(2026, 6, 30)   # the wrong answer, named

    def test_a_recorded_interval_keeps_true_elapsed_seconds(self, db):
        req = _grant(db, on=date(2026, 3, 29))
        _record(
            db, req,
            [_iv(1, datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
                 datetime(2026, 3, 29, 2, 30, tzinfo=UTC))],
            tz="Europe/London", zone=self._london(),
        )
        stored = read_intervals(db, leave_request_id=req.id)
        assert stored[0].duration_seconds == 7200
        assert stored[0].work_date == date(2026, 3, 29)


# ═════════════════════════════════════════════════════════════════════════════
# Recording
# ═════════════════════════════════════════════════════════════════════════════
class TestRecording:
    def test_a_first_recording_creates_version_one(self, db):
        req = _grant(db)
        result = _record(db, req, [_iv(1, _at(9), _at(11))])
        assert result.set_version == 1
        assert result.created is True
        assert result.interval_count == 1

    def test_stored_boundaries_and_duration_are_exact(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11, 30))])
        stored = read_intervals(db, leave_request_id=req.id)
        assert len(stored) == 1
        # 2h30m, stated independently.
        assert stored[0].duration_seconds == 9000
        assert stored[0].seq == 1

    def test_multiple_intervals_in_one_workday(self, db):
        """A Worker taking two separate blocks on the same day."""
        req = _grant(db)
        _record(db, req, [
            _iv(1, _at(4), _at(6)),      # 09:30–11:30 IST
            _iv(2, _at(8), _at(9, 30)),  # 13:30–15:00 IST
        ])
        stored = read_intervals(db, leave_request_id=req.id)
        assert [i.seq for i in stored] == [1, 2]
        assert [i.duration_seconds for i in stored] == [7200, 5400]
        assert {i.work_date for i in stored} == {date(2026, 9, 4)}

    def test_intervals_are_returned_in_deterministic_order(self, db):
        req = _grant(db)
        # Supplied out of order on purpose.
        _record(db, req, [
            _iv(2, _at(14), _at(16)),
            _iv(1, _at(9), _at(11)),
        ])
        stored = read_intervals(db, leave_request_id=req.id)
        assert [i.seq for i in stored] == [1, 2]

    def test_a_cross_midnight_interval_is_stored_whole(self, db):
        """22:00–06:00 local. One interval, attributed to its start date."""
        req = _grant(db, on=date(2026, 9, 4), days=2)
        # 16:30 UTC = 22:00 IST on the 4th; 00:30 UTC on the 5th = 06:00 IST.
        start = datetime(2026, 9, 4, 16, 30, tzinfo=UTC)
        end = datetime(2026, 9, 5, 0, 30, tzinfo=UTC)
        _record(db, req, [_iv(1, start, end)])

        stored = read_intervals(db, leave_request_id=req.id)
        assert len(stored) == 1, "a cross-midnight interval must not be split"
        assert stored[0].duration_seconds == 28800          # 8h
        assert stored[0].work_date == date(2026, 9, 4)      # START_DATE

    def test_an_inadmissible_set_is_refused_and_stores_nothing(self, db):
        req = _grant(db)
        with pytest.raises(LeaveIntervalIntegrityFault) as excinfo:
            _record(db, req, [
                _iv(1, _at(9), _at(12)),
                _iv(2, _at(11), _at(14)),
            ])
        assert excinfo.value.violations
        db.rollback()
        assert current_version(db, leave_request_id=req.id) is None

    def test_a_naive_boundary_is_refused_at_the_service_boundary(self, db):
        req = _grant(db)
        with pytest.raises(LeaveIntervalIntegrityFault):
            _record(db, req, [
                CandidateLeaveInterval(
                    seq=1,
                    start_at_utc=datetime(2026, 9, 4, 9),
                    end_at_utc=datetime(2026, 9, 4, 11),
                )
            ])
        db.rollback()

    def test_the_recording_timezone_is_persisted(self, db):
        """So a later tenant timezone change cannot re-interpret the record."""
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))], tz="Europe/London")
        row = db.execute(
            sa.select(LeaveIntervalSet).where(
                LeaveIntervalSet.leave_request_id == req.id
            )
        ).scalars().one()
        assert row.timezone_iana == "Europe/London"


# ═════════════════════════════════════════════════════════════════════════════
# Correction, withdrawal, concurrency
# ═════════════════════════════════════════════════════════════════════════════
class TestCorrection:
    def test_a_correction_appends_a_new_version(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        result = _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)
        assert result.set_version == 2
        assert current_version(db, leave_request_id=req.id) == 2

    def test_the_prior_version_stays_exactly_as_recorded(self, db):
        """Historical reproducibility: the record as it stood, not as corrected."""
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)

        v1 = read_intervals(db, leave_request_id=req.id, set_version=1)
        v2 = read_intervals(db, leave_request_id=req.id, set_version=2)
        assert v1[0].duration_seconds == 7200    # 2h, as first recorded
        assert v2[0].duration_seconds == 14400   # 4h, as corrected

    def test_reading_without_a_version_gives_the_current_one(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)
        assert read_intervals(db, leave_request_id=req.id)[0].duration_seconds == 14400

    def test_the_supersession_chain_points_backwards(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)

        sets = db.execute(
            sa.select(LeaveIntervalSet)
            .where(LeaveIntervalSet.leave_request_id == req.id)
            .order_by(LeaveIntervalSet.set_version)
        ).scalars().all()
        assert sets[0].supersedes is None
        assert sets[1].supersedes == sets[0].id

    def test_replaying_an_identical_set_is_a_no_op(self, db):
        """§22.1 idempotency: a retry must not append a spurious correction."""
        req = _grant(db)
        first = _record(db, req, [_iv(1, _at(9), _at(11))])
        replay = _record(db, req, [_iv(1, _at(9), _at(11))], expected=1)
        assert replay.created is False
        assert replay.set_version == first.set_version == 1
        assert current_version(db, leave_request_id=req.id) == 1

    def test_a_replay_is_recognised_regardless_of_supplied_order(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11)), _iv(2, _at(14), _at(16))])
        replay = _record(
            db, req, [_iv(2, _at(14), _at(16)), _iv(1, _at(9), _at(11))],
            expected=1,
        )
        assert replay.created is False

    def test_a_stale_correction_is_rejected_not_merged(self, db):
        """Two administrators correcting concurrently must not interleave."""
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)

        with pytest.raises(StaleLeaveIntervalVersion):
            _record(db, req, [_iv(1, _at(9), _at(15))], expected=1)
        db.rollback()

    def test_a_first_recording_against_an_existing_version_is_rejected(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        with pytest.raises(StaleLeaveIntervalVersion):
            _record(db, req, [_iv(1, _at(9), _at(13))], expected=None)
        db.rollback()

    def test_withdrawal_records_an_empty_version_rather_than_deleting(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        result = withdraw_leave_intervals(
            db, leave_request=req, timezone_iana="Asia/Kolkata",
            zone=IST, expected_version=1,
        )
        assert result.set_version == 2
        assert read_intervals(db, leave_request_id=req.id) == ()
        # And the withdrawn intervals are still there, at version 1.
        assert len(read_intervals(db, leave_request_id=req.id, set_version=1)) == 1

    def test_withdrawn_is_distinguishable_from_never_recorded(self, db):
        """The distinction the set table exists to make."""
        never = _grant(db)
        withdrawn = _grant(db, on=date(2026, 9, 5))
        _record(db, withdrawn, [_iv(1, _at(9, 0, 5), _at(11, 0, 5))])
        withdraw_leave_intervals(
            db, leave_request=withdrawn, timezone_iana="Asia/Kolkata",
            zone=IST, expected_version=1,
        )

        assert current_version(db, leave_request_id=never.id) is None
        assert current_version(db, leave_request_id=withdrawn.id) == 2
        assert read_intervals(db, leave_request_id=never.id) == ()
        assert read_intervals(db, leave_request_id=withdrawn.id) == ()

    def test_intervals_can_be_re_recorded_after_withdrawal(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        withdraw_leave_intervals(
            db, leave_request=req, timezone_iana="Asia/Kolkata",
            zone=IST, expected_version=1,
        )
        result = _record(db, req, [_iv(1, _at(9), _at(12))], expected=2)
        assert result.set_version == 3
        assert read_intervals(db, leave_request_id=req.id)[0].duration_seconds == 10800


# ═════════════════════════════════════════════════════════════════════════════
# Binding to the canonical grant — no second source of leave truth
# ═════════════════════════════════════════════════════════════════════════════
class TestCanonicalGrantBinding:
    def test_every_set_is_bound_to_a_leave_request(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        row = db.execute(
            sa.select(LeaveIntervalSet).where(
                LeaveIntervalSet.leave_request_id == req.id
            )
        ).scalars().one()
        assert row.leave_request_id == req.id

    def test_the_grant_reference_is_not_nullable(self):
        """An interval set cannot exist without a grant — structurally."""
        assert LeaveIntervalSet.__table__.c.leave_request_id.nullable is False

    def test_a_request_without_an_organization_is_refused(self, db):
        """The nullable tenant column on leave_requests is not inherited."""
        req = _grant(db, org=None)
        with pytest.raises(LeaveRequestNotTenanted):
            _record(db, req, [_iv(1, _at(9), _at(11))])
        db.rollback()

    def test_identity_comes_from_the_grant_not_the_caller(self, db):
        req = _grant(db, org=ORG, user=USER)
        result = _record(db, req, [_iv(1, _at(9), _at(11))])
        assert (result.organization_id, result.user_id) == (ORG, USER)

    def test_a_grant_without_a_worker_is_refused(self, db):
        class _Detached:
            id, organization_id, user_id = 999999, ORG, None

        with pytest.raises(LeaveIntervalOwnershipMismatch):
            _record(db, _Detached(), [_iv(1, _at(9), _at(11))])
        db.rollback()

    def test_recording_does_not_alter_the_parent_request(self, db):
        """The grant is authoritative and must be left exactly as it was."""
        req = _grant(db)
        before = (req.from_date, req.to_date, req.status,
                  req.leave_days, req.is_half_day, req.half_day_period)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        db.flush()
        after = (req.from_date, req.to_date, req.status,
                 req.leave_days, req.is_half_day, req.half_day_period)
        assert before == after

    def test_whole_day_requests_keep_their_existing_meaning(self, db):
        """A request with no intervals behaves exactly as it did before Step 7."""
        req = _grant(db, on=date(2026, 9, 7), days=3)
        assert current_version(db, leave_request_id=req.id) is None
        assert read_intervals(db, leave_request_id=req.id) == ()
        assert req.from_date == datetime(2026, 9, 7)
        assert req.to_date == datetime(2026, 9, 9)

    def test_half_day_requests_are_not_converted_to_intervals(self, db):
        """No derivation. half_day_period stays a label; nothing infers hours.

        The platform has never mapped "first_half" to clock times, and choosing
        those hours is a policy question Steps 3-4 are blocked on. A half-day
        request therefore gets no intervals until somebody states them.
        """
        req = _grant(db, on=date(2026, 9, 10),
                     is_half_day=True, half_day_period="first_half",
                     leave_days=0.5)
        assert current_version(db, leave_request_id=req.id) is None
        assert req.is_half_day is True
        assert req.half_day_period == "first_half"
        assert req.leave_days == 0.5


# ═════════════════════════════════════════════════════════════════════════════
# Isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestIsolation:
    def test_day_reads_are_scoped_to_the_organization(self, db):
        mine = _grant(db, org=ORG, user=USER)
        theirs = _grant(db, org=OTHER_ORG, user=USER)
        _record(db, mine, [_iv(1, _at(9), _at(11))])
        _record(db, theirs, [_iv(1, _at(12), _at(14))])

        rows = intervals_for_day(
            db, organization_id=ORG, user_id=USER, work_date=date(2026, 9, 4)
        )
        assert len(rows) == 1
        assert rows[0].duration_seconds == 7200

    def test_day_reads_are_scoped_to_the_worker(self, db):
        mine = _grant(db, org=ORG, user=USER)
        theirs = _grant(db, org=ORG, user=OTHER_USER)
        _record(db, mine, [_iv(1, _at(9), _at(11))])
        _record(db, theirs, [_iv(1, _at(12), _at(15))])

        rows = intervals_for_day(
            db, organization_id=ORG, user_id=OTHER_USER,
            work_date=date(2026, 9, 4),
        )
        assert len(rows) == 1
        assert rows[0].duration_seconds == 10800

    def test_superseded_versions_do_not_appear_in_day_reads(self, db):
        """A corrected interval must not sit beside its own correction."""
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        _record(db, req, [_iv(1, _at(9), _at(13))], expected=1)

        rows = intervals_for_day(
            db, organization_id=ORG, user_id=USER, work_date=date(2026, 9, 4)
        )
        assert len(rows) == 1
        assert rows[0].duration_seconds == 14400

    def test_a_withdrawn_set_disappears_from_day_reads(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        withdraw_leave_intervals(
            db, leave_request=req, timezone_iana="Asia/Kolkata",
            zone=IST, expected_version=1,
        )
        assert intervals_for_day(
            db, organization_id=ORG, user_id=USER, work_date=date(2026, 9, 4)
        ) == ()

    def test_both_tables_require_an_organization(self):
        for model in (LeaveIntervalSet, LeaveInterval):
            assert model.__table__.c.organization_id.nullable is False


# ═════════════════════════════════════════════════════════════════════════════
# Append-only enforcement
# ═════════════════════════════════════════════════════════════════════════════
class TestAppendOnly:
    def test_a_recorded_interval_cannot_be_modified(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        interval = read_intervals(db, leave_request_id=req.id)[0]
        interval.duration_seconds = 1
        with pytest.raises(LeaveIntervalImmutable):
            db.flush()
        db.rollback()

    def test_a_recorded_set_cannot_be_modified(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        row = db.execute(
            sa.select(LeaveIntervalSet).where(
                LeaveIntervalSet.leave_request_id == req.id
            )
        ).scalars().one()
        row.timezone_iana = "Europe/London"
        with pytest.raises(LeaveIntervalImmutable):
            db.flush()
        db.rollback()

    def test_a_recorded_interval_cannot_be_deleted(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        interval = read_intervals(db, leave_request_id=req.id)[0]
        db.delete(interval)
        with pytest.raises(LeaveIntervalImmutable):
            db.flush()
        db.rollback()

    def test_a_recorded_set_cannot_be_deleted(self, db):
        req = _grant(db)
        _record(db, req, [_iv(1, _at(9), _at(11))])
        row = db.execute(
            sa.select(LeaveIntervalSet).where(
                LeaveIntervalSet.leave_request_id == req.id
            )
        ).scalars().one()
        db.delete(row)
        with pytest.raises(LeaveIntervalImmutable):
            db.flush()
        db.rollback()


class TestStorageConstraints:
    def _set(self, db, req, version=1):
        row = LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=req.id,
            set_version=version, timezone_iana="Asia/Kolkata",
            recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC), recorded_by="TEST",
        )
        db.add(row)
        db.flush()
        return row

    @pytest.mark.parametrize("duration", [0, -1, -3600])
    def test_non_positive_duration_is_refused_at_rest(self, db, duration):
        req = _grant(db)
        row = self._set(db, req)
        db.add(LeaveInterval(
            set_id=row.id, organization_id=ORG, seq=1,
            work_date=date(2026, 9, 4), start_at_utc=_at(9), end_at_utc=_at(9),
            duration_seconds=duration,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_non_positive_sequence_is_refused_at_rest(self, db):
        req = _grant(db)
        row = self._set(db, req)
        db.add(LeaveInterval(
            set_id=row.id, organization_id=ORG, seq=0,
            work_date=date(2026, 9, 4), start_at_utc=_at(9), end_at_utc=_at(11),
            duration_seconds=7200,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_two_intervals_cannot_share_a_sequence_in_one_set(self, db):
        req = _grant(db)
        row = self._set(db, req)
        for _ in range(2):
            db.add(LeaveInterval(
                set_id=row.id, organization_id=ORG, seq=1,
                work_date=date(2026, 9, 4), start_at_utc=_at(9),
                end_at_utc=_at(11), duration_seconds=7200,
            ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_two_sets_cannot_share_a_version_for_one_request(self, db):
        req = _grant(db)
        self._set(db, req, version=1)
        with pytest.raises(IntegrityError):
            self._set(db, req, version=1)
        db.rollback()

    def test_version_zero_is_refused(self, db):
        req = _grant(db)
        with pytest.raises(IntegrityError):
            self._set(db, req, version=0)
        db.rollback()

    def test_an_empty_timezone_is_refused(self, db):
        req = _grant(db)
        row = LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=req.id,
            set_version=1, timezone_iana="",
            recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC), recorded_by="TEST",
        )
        db.add(row)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


class TestForeignKeysWhereEnforced:
    """Exercised on a dedicated engine, because the shared one leaves
    ``PRAGMA foreign_keys`` off and every assertion here would pass vacuously."""

    @pytest.fixture()
    def fk_session(self):
        from database.models import LeaveRequest, Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        for table in (Organization.__table__, User.__table__,
                      LeaveRequest.__table__, LeaveIntervalSet.__table__,
                      LeaveInterval.__table__):
            table.create(eng, checkfirst=True)

        with Session(eng) as s:
            s.add(Organization(id=ORG, name="Org A"))
            s.add(Organization(id=OTHER_ORG, name="Org B"))
            s.add(User(id=USER, name="W", email="w@example.test",
                       organization_id=ORG))
            s.commit()
            yield s

    def test_foreign_key_enforcement_is_actually_on(self, fk_session):
        """Guard the guard."""
        assert fk_session.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1

    def test_a_set_cannot_reference_a_nonexistent_leave_request(self, fk_session):
        fk_session.add(LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=999999,
            set_version=1, timezone_iana="Asia/Kolkata",
            recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC), recorded_by="TEST",
        ))
        with pytest.raises(IntegrityError):
            fk_session.flush()
        fk_session.rollback()

    def test_an_interval_cannot_be_attached_to_another_organizations_set(
        self, fk_session
    ):
        req = _grant(fk_session)
        row = LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=req.id,
            set_version=1, timezone_iana="Asia/Kolkata",
            recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC), recorded_by="TEST",
        )
        fk_session.add(row)
        fk_session.flush()

        fk_session.add(LeaveInterval(
            set_id=row.id, organization_id=OTHER_ORG, seq=1,
            work_date=date(2026, 9, 4), start_at_utc=_at(9), end_at_utc=_at(11),
            duration_seconds=7200,
        ))
        with pytest.raises(IntegrityError):
            fk_session.flush()
        fk_session.rollback()

    def test_a_grant_carrying_intervals_cannot_be_hard_deleted(self, fk_session):
        """RESTRICT. The existing DELETE /leave/{id} endpoint hard-deletes; this
        is what stops it silently destroying intraday history."""
        req = _grant(fk_session)
        row = LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=req.id,
            set_version=1, timezone_iana="Asia/Kolkata",
            recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC), recorded_by="TEST",
        )
        fk_session.add(row)
        fk_session.flush()

        with pytest.raises(IntegrityError):
            fk_session.execute(
                sa.text("DELETE FROM leave_requests WHERE id = :i"), {"i": req.id}
            )
        fk_session.rollback()

    def test_a_grant_without_intervals_can_still_be_deleted(self, fk_session):
        """Existing cancellation behaviour is unchanged where no intervals exist."""
        req = _grant(fk_session)
        fk_session.commit()
        fk_session.execute(
            sa.text("DELETE FROM leave_requests WHERE id = :i"), {"i": req.id}
        )
        fk_session.commit()


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

    spec = importlib.util.spec_from_file_location("_step7_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrated_schema():
    """Apply the revision to a private in-memory SQLite database."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from database.models import LeaveRequest, Organization, User

    module = _load_migration_module()
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    for table in (Organization.__table__, User.__table__, LeaveRequest.__table__):
        table.create(eng, checkfirst=True)

    with eng.begin() as connection:
        ctx = MigrationContext.configure(connection)
        with Operations.context(ctx):
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
        """Alembic's own resolver, reading scripts only — no env.py, no DB.

        Asserts the invariant, not a snapshot: this revision was the head when
        it was written, and later steps chain onto it. What must never change is
        that the graph does not fork.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert len(sd.get_heads()) == 1, f"the graph forked: {sd.get_heads()}"
        assert len(sd.get_bases()) == 1

    def test_this_revision_is_still_in_the_graph(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert sd.get_revision(EXPECTED_REVISION) is not None

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

    def test_leave_requests_is_not_touched(self):
        """The authoritative grant table keeps its exact current shape."""
        assert "leave_requests" not in _create_table_calls(_migration_tree())
        body = MIGRATION.read_text(encoding="utf-8")
        for op_name in ("add_column", "alter_column", "drop_column"):
            assert f"op.{op_name}" not in body

    def test_the_migration_contains_no_data_backfill(self):
        """No intervals may be manufactured for existing leave."""
        assert "bulk_insert" not in _called_attributes(_migration_tree())
        for node in _migration_tree().body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    upper = " ".join(inner.value.upper().split())
                    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "SELECT "):
                        assert verb not in upper, inner.value

    def test_the_migration_opens_no_connection_of_its_own(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv"):
            assert banned not in body

    def test_only_the_two_authorised_tables_are_created(self):
        assert set(_create_table_calls(_migration_tree())) == {
            "leave_interval_set", "leave_interval",
        }


class TestMigrationActuallyRuns:
    def test_both_tables_are_created(self, migrated_schema):
        present = set(sa.inspect(migrated_schema).get_table_names())
        assert {"leave_interval_set", "leave_interval"} <= present

    @pytest.mark.parametrize("model", [LeaveIntervalSet, LeaveInterval])
    def test_executed_columns_match_the_orm_model(self, migrated_schema, model):
        actual = sa.inspect(migrated_schema).get_columns(model.__tablename__)
        assert sorted(c["name"] for c in actual) == sorted(
            c.name for c in model.__table__.columns
        )

    @pytest.mark.parametrize("model", [LeaveIntervalSet, LeaveInterval])
    def test_executed_nullability_matches_the_orm_model(self, migrated_schema, model):
        actual = {
            c["name"]: c["nullable"]
            for c in sa.inspect(migrated_schema).get_columns(model.__tablename__)
        }
        assert actual == {c.name: c.nullable for c in model.__table__.columns}

    def test_the_migration_created_no_rows(self, migrated_schema):
        with migrated_schema.connect() as conn:
            for table in ("leave_interval_set", "leave_interval"):
                assert conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 fixed
                ).scalar() == 0

    def test_leave_requests_was_not_altered(self, migrated_schema):
        from database.models import LeaveRequest

        actual = sorted(
            c["name"] for c in
            sa.inspect(migrated_schema).get_columns("leave_requests")
        )
        assert actual == sorted(c.name for c in LeaveRequest.__table__.columns)

    def test_the_duration_constraint_is_live(self, migrated_schema):
        with Session(migrated_schema) as s:
            req = _grant(s)
            row = LeaveIntervalSet(
                organization_id=ORG, user_id=USER, leave_request_id=req.id,
                set_version=1, timezone_iana="Asia/Kolkata",
                recorded_at_utc=datetime(2026, 9, 4, tzinfo=UTC),
                recorded_by="TEST",
            )
            s.add(row)
            s.flush()
            s.add(LeaveInterval(
                set_id=row.id, organization_id=ORG, seq=1,
                work_date=date(2026, 9, 4), start_at_utc=_at(9),
                end_at_utc=_at(9), duration_seconds=0,
            ))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()

    def test_downgrade_removes_exactly_the_two_tables(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import LeaveRequest, Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        for table in (Organization.__table__, User.__table__,
                      LeaveRequest.__table__):
            table.create(eng, checkfirst=True)

        before = set(sa.inspect(eng).get_table_names())
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
        after_upgrade = set(sa.inspect(eng).get_table_names())
        with eng.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()

        assert after_upgrade - before == {"leave_interval_set", "leave_interval"}
        assert set(sa.inspect(eng).get_table_names()) == before

    def test_reapplying_the_migration_is_a_no_op(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import LeaveRequest, Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        for table in (Organization.__table__, User.__table__,
                      LeaveRequest.__table__):
            table.create(eng, checkfirst=True)
        for _ in range(2):
            with eng.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()
        assert {"leave_interval_set", "leave_interval"} <= set(
            sa.inspect(eng).get_table_names()
        )


class TestPostgresDialectCompilation:
    """SQLite passing does not prove PostgreSQL correctness.

    These compile the same DDL against the PostgreSQL dialect — a pure string
    generation, no connection, no DSN — so dialect-specific details are checked
    rather than assumed.
    """

    @staticmethod
    def _ddl(table):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        return str(CreateTable(table).compile(dialect=postgresql.dialect()))

    def test_instants_compile_to_timestamp_with_time_zone(self):
        ddl = self._ddl(LeaveInterval.__table__)
        for column in ("start_at_utc", "end_at_utc"):
            assert f"{column} TIMESTAMP WITH TIME ZONE" in ddl, ddl

    def test_the_local_date_compiles_to_a_bare_date(self):
        assert "work_date DATE" in self._ddl(LeaveInterval.__table__)

    def test_the_composite_tenant_key_compiles_with_both_columns(self):
        ddl = self._ddl(LeaveInterval.__table__)
        assert "FOREIGN KEY(set_id, organization_id)" in ddl
        assert "REFERENCES leave_interval_set (id, organization_id)" in ddl
        assert "ON DELETE RESTRICT" in ddl

    def test_the_grant_reference_restricts_on_postgres(self):
        ddl = self._ddl(LeaveIntervalSet.__table__)
        assert "REFERENCES leave_requests (id) ON DELETE RESTRICT" in ddl

    def test_check_constraints_survive_compilation(self):
        ddl = self._ddl(LeaveInterval.__table__)
        assert "duration_seconds > 0" in ddl
        assert "seq >= 1" in ddl

    def test_the_natural_key_compiles_as_unique(self):
        assert "UNIQUE (leave_request_id, set_version)" in self._ddl(
            LeaveIntervalSet.__table__
        )


# ═════════════════════════════════════════════════════════════════════════════
# Isolation guards
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
    """Names the code actually defines or references — never its prose.

    Docstrings and comments are excluded by construction: only identifier
    positions are collected, so a module explaining which concepts it must not
    contain cannot fail a guard for naming them.
    """
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


class TestIsolationGuards:
    def test_the_guard_reads_the_whole_package(self):
        assert {p.name for p in _package_files()} == {
            "__init__.py", "errors.py", "models.py", "service.py", "validation.py",
        }

    def test_no_activity_or_screenshot_dependency(self):
        """INV-6. Leave is never inferred from what a Worker's machine did."""
        banned = ("active_time", "Activity", "ActivityWindow", "BrowserActivity",
                  "AppSession", "screenshot", "keystroke", "app.intelligence")
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path) if any(b in m for b in banned)
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_worked_time_dependency(self):
        """Leave says when it was taken, never what work it displaced."""
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if "_worked" in m or "attendance_policy" in m or "payroll" in m
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_only_thing_taken_from_the_ledger_is_duration_arithmetic(self):
        """One §16.4 implementation, shared — and provably nothing else.

        Importing the ledger's duration function is deliberate: two functions
        computing elapsed seconds is exactly the divergence this programme
        exists to remove. This pins that the coupling is that and only that —
        no models, no service, no work-state vocabulary.
        """
        permitted = {
            "app.work_ledger.integrity",
            "app.work_ledger.integrity.duration_seconds",
        }
        for path in _package_files():
            taken = {
                m for m in _imported_modules(path) if m.startswith("app.work_ledger")
            }
            assert taken <= permitted, f"{path.name} takes {taken - permitted}"

    def test_no_early_clock_out_dependency(self):
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if "early_clock_out" in m or "requirement_resolution" in m
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_half_day_derivation_exists(self):
        """No rule anywhere here turns a half-day label into clock times."""
        for path in _package_files():
            body = path.read_text(encoding="utf-8")
            code = "\n".join(
                line for line in body.splitlines()
                if not line.lstrip().startswith("#")
            )
            for token in ("first_half", "second_half"):
                # The names may be discussed in docstrings explaining why no
                # mapping exists; they must never appear in a comparison.
                assert f'== "{token}"' not in code, f"{path.name} branches on {token}"
                assert f"== '{token}'" not in code


class TestScopeHeld:
    def test_nothing_in_the_application_wires_this_package(self):
        """Still not wired to any application surface.

        ``app/eval_day/exclusion.py`` is the one authorized consumer: owner
        ruling MODEL-EXCLUSION makes approved-leave overlap an input to the day
        evaluation, and it reads through this package's own
        ``intervals_for_day`` rather than querying the tables directly — so the
        current-version, tenant-scoped authority stays here. EVAL-DAY is
        foundation, not an application surface, and is held to its own
        not-wired guard.
        """
        allowed = {"app/eval_day/exclusion.py"}
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "leave_intervals" in path.parts:
                continue
            rel = path.relative_to(BACKEND).as_posix()
            if rel.replace("backend/", "") in allowed or rel in allowed:
                continue
            if any(m.startswith("app.leave_intervals")
                   for m in _imported_modules(path)):
                offenders.append(rel)
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_authorized_consumer_reads_through_the_service(self):
        """Guard the exemption: the consumer must use this package's accessor,
        not reach past it into the interval table.

        Read from the imports rather than the file text — the consumer's
        docstring explains the ``work_date`` attribution rule at length, and a
        text scan cannot tell an explanation from a query.
        """
        import ast

        consumer = BACKEND / "app" / "eval_day" / "exclusion.py"
        tree = ast.parse(consumer.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(a.name for a in node.names)

        # The accessor is used; the interval model itself is never imported, so
        # the current-version and tenant filtering cannot be bypassed.
        assert "intervals_for_day" in imported
        assert "LeaveInterval" not in imported

    def test_the_existing_leave_router_is_untouched_by_this_package(self):
        """Step 7 adds a layer; it does not edit the leave module."""
        router = BACKEND / "app" / "leave" / "router.py"
        assert "leave_intervals" not in router.read_text(encoding="utf-8")

    def test_no_eco_or_threshold_logic_exists_here(self):
        """Scanned over identifiers, not raw text.

        These docstrings name "thresholds" and "shortfall" precisely to record
        that they must never be implemented here. A text scan would fail on the
        documentation of its own rule — so the check reads what the code
        actually does, not what it says about itself.
        """
        for path in _package_files():
            names = _code_identifiers(path)
            offenders = {
                n for n in names
                if any(term in n.lower()
                       for term in ("shortfall", "threshold", "early_clock_out"))
            }
            assert not offenders, f"{path.name} defines/uses {offenders}"
