"""
§45.D-15 — session-boundary provenance.
=======================================

Proves the owner-ruled contract closed through D15-1-B, D15-2-B, D15-3-A and
D15-4-A:

* every MOUNTED production boundary writer records an origin, in the same
  transaction as the boundary write;
* ``clock_in`` cannot carry a closure-only origin — structurally, not by
  convention;
* events are append-only: an admin rewrite appends, it never overwrites;
* the rollback/recovery branch of ``/employees/clock-out`` leaves exactly one
  event;
* neither deleting a Worker nor deleting an actor can destroy an event;
* nothing is backfilled.

Foreign keys are enforced on a DEDICATED engine. The shared test engine leaves
``PRAGMA foreign_keys`` off, and without it every RESTRICT assertion here would
pass vacuously.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from database.database import Base
import database.models as dbm

from app.clock_session_boundary import (
    ALLOWED_PROVENANCE,
    BOUNDARIES,
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    CLOSE_BOUNDARY_PROVENANCE,
    OPEN_BOUNDARY_PROVENANCE,
    SYSTEM_ACTOR_LABEL,
    BoundaryEventImmutable,
    ClockSessionBoundaryEvent,
    InvalidBoundary,
    InvalidBoundaryProvenance,
    MissingActorLabel,
    NaiveBoundaryTimestamp,
    append_boundary_event,
)
from app.clock_session_boundary import service as csb_service
from app.work_ledger.states import (
    Provenance,
    SYNTHESIZED_CLOSURE_PROVENANCE,
    VERIFIED_DEPARTURE_PROVENANCE,
)

BACKEND = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = BACKEND / "alembic" / "versions" / \
    "c4e9b7d2a583_clock_session_boundary_event.py"

ORG = 8801
WORKER = 8811
ADMIN = 8812
OTHER = 8813

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# A full-schema engine that actually enforces foreign keys
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def fk_engine():
    # Importing these registers the domain-local model modules on the shared
    # Base metadata, so create_all builds the whole schema — including the FK
    # targets `clock_sessions` and `users`.
    import app.work_ledger.models          # noqa: F401
    import app.clock_session_boundary.models  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(fk_engine):
    # `expire_on_commit=False` so a commit inside the code under test does not
    # silently replace the tz-AWARE datetimes Python wrote with the NAIVE ones
    # pysqlite reads back. PostgreSQL — production — returns aware values, so
    # expiring here would make these tests exercise a driver artefact.
    with SaSession(fk_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Org D15"))
        s.add(dbm.User(id=WORKER, name="W", email="w@example.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.User(id=ADMIN, name="A", email="a@example.test",
                       organization_id=ORG, role="admin"))
        s.add(dbm.User(id=OTHER, name="O", email="o@example.test",
                       organization_id=ORG, role="employee"))
        s.commit()
        yield s


def _session(db, *, clock_in=NOW, clock_out=None, user_id=WORKER):
    row = dbm.ClockSession(
        user_id=user_id, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=0, break_seconds=0,
        is_active=clock_out is None,
    )
    db.add(row)
    db.commit()
    return row


def _events(db, session_id=None, boundary=None):
    q = db.query(ClockSessionBoundaryEvent)
    if session_id is not None:
        q = q.filter(ClockSessionBoundaryEvent.session_id == session_id)
    if boundary is not None:
        q = q.filter(ClockSessionBoundaryEvent.boundary == boundary)
    return q.order_by(ClockSessionBoundaryEvent.sequence).all()


def _fk(column):
    return list(column.foreign_keys)[0]


# ═════════════════════════════════════════════════════════════════════════════
# Schema
# ═════════════════════════════════════════════════════════════════════════════
class TestSchema:
    def test_foreign_key_enforcement_is_actually_on(self, db):
        """Guard the guard: every RESTRICT test below depends on this."""
        assert db.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1

    def test_the_table_exists_with_the_ruled_columns(self, fk_engine):
        cols = {c["name"]: c for c
                in sa.inspect(fk_engine).get_columns(
                    "clock_session_boundary_event")}
        assert set(cols) == {
            "id", "session_id", "user_id", "boundary", "boundary_at",
            "provenance", "actor_label", "written_at", "sequence",
        }

    @pytest.mark.parametrize("column", [
        "session_id", "user_id", "boundary", "boundary_at", "provenance",
        "actor_label", "written_at", "sequence",
    ])
    def test_every_recorded_field_is_not_null(self, column):
        assert ClockSessionBoundaryEvent.__table__.c[column].nullable is False

    def test_actor_label_is_not_null_and_bounded(self):
        col = ClockSessionBoundaryEvent.__table__.c.actor_label
        assert col.nullable is False
        assert col.type.length == 64

    def test_actor_label_carries_no_foreign_key(self):
        """Owner ruling D15-4-A. A FK would break it whichever action it had:
        CASCADE deletes the event, RESTRICT makes every adjusting admin
        undeletable, SET NULL writes to a row required to be immutable."""
        assert ClockSessionBoundaryEvent.__table__.c.actor_label.foreign_keys \
            == set()

    def test_no_column_on_the_table_references_users_except_the_subject(self):
        referencing = {
            c.name for c in ClockSessionBoundaryEvent.__table__.columns
            if any(fk.column.table.name == "users" for fk in c.foreign_keys)
        }
        assert referencing == {"user_id"}

    def test_the_session_foreign_key_is_restrict(self):
        assert _fk(ClockSessionBoundaryEvent.__table__.c.session_id).ondelete \
            == "RESTRICT"

    def test_the_subject_user_foreign_key_is_restrict(self):
        assert _fk(ClockSessionBoundaryEvent.__table__.c.user_id).ondelete \
            == "RESTRICT"

    def test_no_provenance_column_was_added_to_clock_sessions(self):
        """D15-3-A: the session row stays the timestamp projection only."""
        names = set(dbm.ClockSession.__table__.c.keys())
        assert names == {
            "id", "user_id", "organization_id", "clock_in_time",
            "clock_out_time", "total_seconds", "break_seconds",
            "offline_seconds", "is_active", "created_at",
        }
        for name in names:
            assert "provenance" not in name
            assert "actor" not in name

    def test_no_server_default_can_fabricate_a_provenance(self):
        for name in ("provenance", "boundary", "actor_label", "boundary_at",
                     "sequence"):
            col = ClockSessionBoundaryEvent.__table__.c[name]
            assert col.server_default is None, name
            assert col.default is None, name

    def test_the_unique_constraint_and_indexes_exist(self):
        t = ClockSessionBoundaryEvent.__table__
        uniques = {c.name for c in t.constraints
                   if isinstance(c, sa.UniqueConstraint)}
        assert "uq_csbe_session_boundary_sequence" in uniques
        assert {i.name for i in t.indexes} >= {
            "ix_csbe_session_boundary_seq", "ix_csbe_user_boundary",
        }

    def test_both_dialects_render_restrict(self):
        """Compiled, never executed against a real PostgreSQL."""
        from sqlalchemy.dialects import postgresql, sqlite
        from sqlalchemy.schema import CreateTable

        for dialect in (postgresql.dialect(), sqlite.dialect()):
            ddl = str(CreateTable(
                ClockSessionBoundaryEvent.__table__).compile(dialect=dialect))
            assert "REFERENCES clock_sessions (id) ON DELETE RESTRICT" in ddl
            assert "REFERENCES users (id) ON DELETE RESTRICT" in ddl


# ═════════════════════════════════════════════════════════════════════════════
# D15-2-B — the boundary/provenance pairing, enforced by the database
# ═════════════════════════════════════════════════════════════════════════════
class TestBoundaryProvenancePairing:
    def test_the_vocabulary_is_the_existing_enum_reused(self):
        """No new enum, no duplicate enum, no sixth value."""
        assert CLOSE_BOUNDARY_PROVENANCE == (
            VERIFIED_DEPARTURE_PROVENANCE | SYNTHESIZED_CLOSURE_PROVENANCE)
        assert OPEN_BOUNDARY_PROVENANCE == VERIFIED_DEPARTURE_PROVENANCE
        assert len(CLOSE_BOUNDARY_PROVENANCE) == 5
        assert all(isinstance(p, Provenance) for p in CLOSE_BOUNDARY_PROVENANCE)

    def test_no_generic_origin_was_added_to_the_vocabulary(self):
        banned = {"AUTO", "SYSTEM", "UNKNOWN", "CLOSED", "VERIFIED"}
        assert {p.value for p in Provenance} & banned == set()
        assert SYSTEM_ACTOR_LABEL not in {p.value for p in Provenance}

    def test_boundaries_are_exactly_the_two(self):
        assert BOUNDARIES == {"clock_in", "clock_out"}

    # ── the service refuses ──────────────────────────────────────────────────
    @pytest.mark.parametrize("prov", [
        Provenance.WORKER, Provenance.ADMIN_OVERRIDE,
    ])
    def test_clock_in_accepts_the_two_deliberate_origins(self, db, prov):
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_IN,
            boundary_at=NOW, provenance=prov, actor_label="w@example.test")
        db.commit()
        assert _events(db, s.id)[0].provenance == prov.value

    @pytest.mark.parametrize("prov", [
        Provenance.STALE_AUTOCLOSE, Provenance.ABANDONED_AUTOCLOSE,
        Provenance.OFFBOARDING,
    ])
    def test_clock_in_refuses_every_closure_only_origin(self, db, prov):
        """A clock-in is not a closure. Attributing one would record an event
        that has not happened."""
        s = _session(db)
        with pytest.raises(InvalidBoundaryProvenance):
            append_boundary_event(
                db, session_id=s.id, user_id=WORKER,
                boundary=BOUNDARY_CLOCK_IN, boundary_at=NOW,
                provenance=prov, actor_label=SYSTEM_ACTOR_LABEL)

    @pytest.mark.parametrize("prov", sorted(
        CLOSE_BOUNDARY_PROVENANCE, key=lambda p: p.value))
    def test_clock_out_accepts_all_five(self, db, prov):
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=prov, actor_label=SYSTEM_ACTOR_LABEL)
        db.commit()
        assert _events(db, s.id)[0].provenance == prov.value

    @pytest.mark.parametrize("prov", [
        Provenance.CLASSIFIER_DERIVED, Provenance.BREAK_REPAIR,
        Provenance.BREAK_DERIVED, Provenance.PRODUCER_DERIVED,
    ])
    def test_interval_level_provenance_is_not_a_boundary_origin(self, db, prov):
        """These four are interval-level, not session-close origins.

        PRODUCER_DERIVED (D-18A) is included deliberately: it describes a
        boundary the Work Ledger producer authored, which is by definition not
        a session departure, so it must never reach a D-15 boundary event.
        """
        s = _session(db)
        for boundary in (BOUNDARY_CLOCK_IN, BOUNDARY_CLOCK_OUT):
            with pytest.raises(InvalidBoundaryProvenance):
                append_boundary_event(
                    db, session_id=s.id, user_id=WORKER, boundary=boundary,
                    boundary_at=NOW, provenance=prov, actor_label="x")

    def test_an_unknown_provenance_is_refused_not_guessed(self, db):
        s = _session(db)
        for bad in ("worker", "WORKER ", "AUTO", "SYSTEM", "", None, 3):
            with pytest.raises(InvalidBoundaryProvenance):
                append_boundary_event(
                    db, session_id=s.id, user_id=WORKER,
                    boundary=BOUNDARY_CLOCK_OUT, boundary_at=NOW,
                    provenance=bad, actor_label="x")

    def test_an_unknown_boundary_is_refused(self, db):
        s = _session(db)
        for bad in ("clockin", "CLOCK_IN", "in", "", None):
            with pytest.raises(InvalidBoundary):
                append_boundary_event(
                    db, session_id=s.id, user_id=WORKER, boundary=bad,
                    boundary_at=NOW, provenance=Provenance.WORKER,
                    actor_label="x")

    # ── the DATABASE refuses, independently of the service ───────────────────
    @pytest.mark.parametrize("prov", [
        "STALE_AUTOCLOSE", "ABANDONED_AUTOCLOSE", "OFFBOARDING",
    ])
    def test_the_check_constraint_makes_a_bad_pair_unrepresentable(
            self, db, prov):
        """Bypasses the service entirely. If validation were the only guard, a
        future writer using the ORM directly could still create the row."""
        s = _session(db)
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, 'clock_in', :at, :prov, 'SYSTEM', :at, 1)"),
                {"sid": s.id, "uid": WORKER, "at": NOW, "prov": prov})
        db.rollback()

    @pytest.mark.parametrize("boundary", ["clock_in", "clock_out"])
    def test_the_check_constraint_rejects_producer_derived(self, db, boundary):
        """D-18A added a ninth provenance value; the D-15 pairing constraint
        whitelists the five session-close values explicitly, so it is already
        inadmissible on BOTH boundaries with no change to that constraint.
        Bypasses the service, so this is the database refusing."""
        s = _session(db)
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, :b, :at, 'PRODUCER_DERIVED', 'SYSTEM', :at, 1)"),
                {"sid": s.id, "uid": WORKER, "at": NOW, "b": boundary})
        db.rollback()

    def test_the_check_constraint_rejects_an_unknown_boundary(self, db):
        s = _session(db)
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, 'midnight', :at, 'WORKER', 'SYSTEM', :at, 1)"),
                {"sid": s.id, "uid": WORKER, "at": NOW})
        db.rollback()

    def test_the_check_constraint_rejects_a_zero_sequence(self, db):
        s = _session(db)
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, 'clock_out', :at, 'WORKER', 'SYSTEM', :at, 0)"),
                {"sid": s.id, "uid": WORKER, "at": NOW})
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Required inputs — nothing is defaulted
# ═════════════════════════════════════════════════════════════════════════════
class TestNothingIsDefaulted:
    def test_provenance_has_no_default(self):
        import inspect
        sig = inspect.signature(append_boundary_event)
        assert sig.parameters["provenance"].default is inspect.Parameter.empty
        assert sig.parameters["actor_label"].default is inspect.Parameter.empty
        assert sig.parameters["boundary"].default is inspect.Parameter.empty

    def test_a_blank_actor_label_is_refused(self, db):
        s = _session(db)
        for bad in ("", "   ", None, 7):
            with pytest.raises(MissingActorLabel):
                append_boundary_event(
                    db, session_id=s.id, user_id=WORKER,
                    boundary=BOUNDARY_CLOCK_OUT, boundary_at=NOW,
                    provenance=Provenance.WORKER, actor_label=bad)

    def test_a_naive_boundary_timestamp_is_refused(self, db):
        s = _session(db)
        with pytest.raises(NaiveBoundaryTimestamp):
            append_boundary_event(
                db, session_id=s.id, user_id=WORKER,
                boundary=BOUNDARY_CLOCK_OUT,
                boundary_at=datetime(2026, 9, 9, 10, 0),
                provenance=Provenance.WORKER, actor_label="x")

    def test_a_foreign_key_violation_is_not_retried_into_a_timeout(self, db):
        """An unknown session is an integrity failure, not contention."""
        with pytest.raises(IntegrityError):
            append_boundary_event(
                db, session_id=999999, user_id=WORKER,
                boundary=BOUNDARY_CLOCK_OUT, boundary_at=NOW,
                provenance=Provenance.WORKER, actor_label="x")
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# D15-3-A — append-only
# ═════════════════════════════════════════════════════════════════════════════
class TestAppendOnly:
    def test_the_package_exposes_no_mutation_or_delete_api(self):
        import app.clock_session_boundary as pkg
        for name in dir(pkg):
            low = name.lower()
            assert not low.startswith(("update_", "delete_", "remove_",
                                       "set_", "upsert_", "overwrite_")), name
        assert "append_boundary_event" in pkg.__all__

    def test_the_service_source_contains_no_update_or_delete_path(self):
        src = pathlib.Path(csb_service.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", None)
                assert attr not in {"delete", "merge"}, f"service calls .{attr}"
        assert "UPDATE " not in src.upper().replace("UPDATED", "")

    def test_updating_an_existing_event_is_rejected(self, db):
        s = _session(db)
        ev = append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        db.commit()

        ev.provenance = Provenance.ADMIN_OVERRIDE.value
        with pytest.raises(BoundaryEventImmutable):
            db.commit()
        db.rollback()
        assert _events(db, s.id)[0].provenance == "WORKER"

    def test_deleting_an_existing_event_is_rejected(self, db):
        s = _session(db)
        ev = append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        db.commit()

        db.delete(ev)
        with pytest.raises(BoundaryEventImmutable):
            db.commit()
        db.rollback()
        assert len(_events(db, s.id)) == 1

    def test_no_orm_relationship_can_cascade_delete_events(self):
        """Deliberately no relationship from ClockSession, so no Python-side
        cascade can delete events before the database RESTRICT is reached."""
        for rel in dbm.ClockSession.__mapper__.relationships:
            assert rel.mapper.class_ is not ClockSessionBoundaryEvent
        for rel in dbm.User.__mapper__.relationships:
            assert rel.mapper.class_ is not ClockSessionBoundaryEvent

    def test_deleting_the_parent_session_is_refused_by_the_database(self, db):
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        db.commit()

        with pytest.raises(IntegrityError):
            db.execute(sa.text("DELETE FROM clock_sessions WHERE id = :i"),
                       {"i": s.id})
        db.rollback()
        assert len(_events(db, s.id)) == 1

    def test_deleting_the_worker_cannot_silently_destroy_events(self, db):
        """`User.sessions` is delete-orphan, so an ORM delete walks to
        clock_sessions — and is stopped there by RESTRICT."""
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        db.commit()

        db.delete(db.get(dbm.User, WORKER))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert len(_events(db, s.id)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Sequence allocation
# ═════════════════════════════════════════════════════════════════════════════
class TestSequence:
    def _append(self, db, s, boundary, prov=Provenance.WORKER):
        return append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=boundary,
            boundary_at=NOW, provenance=prov, actor_label="w")

    def test_repeated_writes_increment(self, db):
        s = _session(db)
        for _ in range(3):
            self._append(db, s, BOUNDARY_CLOCK_OUT)
        db.commit()
        assert [e.sequence for e in _events(db, s.id, BOUNDARY_CLOCK_OUT)] \
            == [1, 2, 3]

    def test_the_two_boundaries_have_independent_sequences(self, db):
        s = _session(db)
        self._append(db, s, BOUNDARY_CLOCK_IN)
        self._append(db, s, BOUNDARY_CLOCK_OUT)
        self._append(db, s, BOUNDARY_CLOCK_IN)
        db.commit()
        assert [e.sequence for e in _events(db, s.id, BOUNDARY_CLOCK_IN)] == [1, 2]
        assert [e.sequence for e in _events(db, s.id, BOUNDARY_CLOCK_OUT)] == [1]

    def test_two_sessions_have_independent_sequences(self, db):
        a, b = _session(db), _session(db, user_id=OTHER)
        self._append(db, a, BOUNDARY_CLOCK_OUT)
        append_boundary_event(
            db, session_id=b.id, user_id=OTHER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="o")
        db.commit()
        assert _events(db, a.id)[0].sequence == 1
        assert _events(db, b.id)[0].sequence == 1

    def test_a_duplicate_sequence_is_unrepresentable(self, db):
        """The constraint is the authority; the retry is only liveness."""
        s = _session(db)
        self._append(db, s, BOUNDARY_CLOCK_OUT)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, 'clock_out', :at, 'WORKER', 'W', :at, 1)"),
                {"sid": s.id, "uid": WORKER, "at": NOW})
        db.rollback()

    def test_the_allocation_is_a_single_statement_not_a_read_then_write(self):
        """The unsafe form of MAX+1 is the one that reads the maximum into the
        application and inserts it later. Here the maximum is a scalar subquery
        evaluated by the database as part of the INSERT, so the value cannot go
        stale between being computed and being stored."""
        from sqlalchemy.dialects import postgresql, sqlite

        expr = csb_service._next_sequence_expression(41, BOUNDARY_CLOCK_OUT)
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            sql = str(expr.compile(dialect=dialect,
                                   compile_kwargs={"literal_binds": True}))
            assert "max(clock_session_boundary_event.sequence)" in sql.lower()
            assert "coalesce" in sql.lower()

    def test_the_allocation_sees_a_row_committed_by_another_connection(self, db,
                                                                       fk_engine):
        """Concurrency, as far as the test infrastructure permits: a separate
        connection commits sequence 1, and our allocation — computed inside the
        INSERT rather than read beforehand — lands on 2 rather than colliding."""
        s = _session(db)
        with SaSession(fk_engine) as other:
            other.execute(sa.text(
                "INSERT INTO clock_session_boundary_event "
                "(session_id, user_id, boundary, boundary_at, provenance, "
                " actor_label, written_at, sequence) VALUES "
                "(:sid, :uid, 'clock_out', :at, 'WORKER', 'W', :at, 1)"),
                {"sid": s.id, "uid": WORKER, "at": NOW})
            other.commit()

        ev = self._append(db, s, BOUNDARY_CLOCK_OUT)
        db.commit()
        assert ev.sequence == 2
        assert [e.sequence for e in _events(db, s.id, BOUNDARY_CLOCK_OUT)] == [1, 2]

    def test_the_service_never_swallows_an_integrity_error(self):
        """Fails CLOSED. If the constraint rejects the insert, the exception
        must reach the caller so its transaction cannot commit a boundary
        write with no recorded origin."""
        src = pathlib.Path(csb_service.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                caught = ast.unparse(node.type) if node.type else "bare"
                assert "IntegrityError" not in caught, caught
                assert caught != "bare", "a bare except would hide a refusal"

    def test_no_savepoint_is_used(self):
        """Withdrawn deliberately: pysqlite does not open a driver-level
        transaction for a SELECT, so RELEASE of an outermost SAVEPOINT COMMITS
        — which would publish the event ahead of the caller's transaction."""
        src = pathlib.Path(csb_service.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        body = code.split('"""', 2)[-1]      # past the module docstring
        assert "begin_nested" not in body
        assert "SAVEPOINT" not in body.upper()

    def test_ordering_does_not_rely_on_the_surrogate_id(self, db):
        """Two sessions interleaved: ids ascend globally, sequences do not."""
        a, b = _session(db), _session(db, user_id=OTHER)
        self._append(db, a, BOUNDARY_CLOCK_OUT)
        append_boundary_event(
            db, session_id=b.id, user_id=OTHER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="o")
        self._append(db, a, BOUNDARY_CLOCK_OUT)
        db.commit()
        rows = _events(db, a.id, BOUNDARY_CLOCK_OUT)
        assert [r.sequence for r in rows] == [1, 2]
        assert rows[1].id > rows[0].id + 1     # b's event sits between them


# ═════════════════════════════════════════════════════════════════════════════
# Atomicity
# ═════════════════════════════════════════════════════════════════════════════
class TestAtomicity:
    def test_the_service_never_commits(self, db):
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        # Still uncommitted: a second connection cannot see it.
        db.rollback()
        assert _events(db, s.id) == []

    def test_a_rolled_back_boundary_write_takes_its_event_with_it(self, db):
        s = _session(db)
        s.clock_out_time = NOW + timedelta(hours=8)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=s.clock_out_time, provenance=Provenance.WORKER,
            actor_label="w")
        db.rollback()
        db.expire_all()
        assert db.get(dbm.ClockSession, s.id).clock_out_time is None
        assert _events(db, s.id) == []

    def test_a_committed_boundary_write_keeps_its_event(self, db):
        s = _session(db)
        closed = NOW + timedelta(hours=8)
        s.clock_out_time = closed
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=closed, provenance=Provenance.WORKER, actor_label="w")
        db.commit()
        db.expire_all()
        assert db.get(dbm.ClockSession, s.id).clock_out_time is not None
        assert len(_events(db, s.id)) == 1

    def test_the_callers_pending_boundary_write_lands_with_the_event(self, db):
        """The append flushes the caller's pending ORM work rather than racing
        it, so the boundary write and the event are ordered inside the one
        transaction the caller commits."""
        s = _session(db)
        closed = NOW + timedelta(hours=8)
        s.clock_out_time = closed
        s.total_seconds = 4321
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=closed, provenance=Provenance.WORKER, actor_label="w")
        db.commit()
        db.expire_all()

        row = db.get(dbm.ClockSession, s.id)
        assert row.total_seconds == 4321
        assert row.clock_out_time is not None
        assert len(_events(db, s.id)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Historical data
# ═════════════════════════════════════════════════════════════════════════════
class TestNoBackfill:
    def test_a_pre_existing_session_has_zero_events(self, db):
        s = _session(db, clock_out=NOW + timedelta(hours=8))
        assert _events(db, s.id) == []

    def test_the_migration_creates_no_rows_and_opens_no_connection(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("INSERT", "UPDATE ", "bulk_insert", "create_engine",
                       "DATABASE_URL", "postgresql://", "sessionmaker",
                       "load_dotenv", "session.add"):
            assert banned not in body, f"migration mentions {banned}"

    def test_the_migration_derives_nothing_from_existing_data(self):
        """Scoped to `upgrade`: `downgrade` legitimately drops what upgrade
        created, so a repository-wide ban on drop_table would be vacuous."""
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        banned = {"bulk_insert", "execute", "add_column", "drop_column",
                  "alter_column", "drop_table", "rename_table"}
        called = {getattr(n.func, "attr", None) for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call)}
        assert called & banned == set(), called & banned

        downgrade = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "downgrade")
        down_called = {getattr(n.func, "attr", None) for n in ast.walk(downgrade)
                       if isinstance(n, ast.Call)}
        # Reversible, and it reverses ONLY what upgrade created.
        assert "drop_table" in down_called
        assert down_called & {"add_column", "alter_column", "bulk_insert"} == set()

    def test_the_migration_touches_only_the_new_table(self):
        module = self._module()
        assert module.TABLE == "clock_session_boundary_event"
        body = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(body)
        creates = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "create_table"]
        assert len(creates) == 1
        assert creates[0].args[0].id == "TABLE"

    def test_the_migration_runs_and_reverses_offline_on_sqlite(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        dbm.User.__table__.create(eng, checkfirst=True)
        dbm.Organization.__table__.create(eng, checkfirst=True)
        dbm.ClockSession.__table__.create(eng, checkfirst=True)

        module = self._module()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
                module.upgrade()               # idempotent
                assert "clock_session_boundary_event" in \
                    sa.inspect(conn).get_table_names()
                # Created empty: no backfill, by construction.
                assert conn.execute(sa.text(
                    "SELECT COUNT(*) FROM clock_session_boundary_event"
                )).scalar() == 0
                module.downgrade()
                assert "clock_session_boundary_event" not in \
                    sa.inspect(conn).get_table_names()

    def test_the_migration_and_the_model_agree_on_the_pairing_constraint(self):
        """Parity: a migration that drifted from the model would build a
        database whose constraint disagrees with the code's validation."""
        from app.clock_session_boundary.models import (
            _BOUNDARY_SQL, _PAIRING_SQL,
        )
        module = self._module()
        assert module.PAIRING_SQL == _PAIRING_SQL
        assert module.BOUNDARY_VOCABULARY_SQL == _BOUNDARY_SQL

    def test_the_revision_is_a_single_parent_child_of_the_prior_head(self):
        module = self._module()
        assert module.revision == "c4e9b7d2a583"
        assert module.down_revision == "b7c4e0a92d51"
        assert module.branch_labels is None
        assert module.depends_on is None

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "d15_migration", MIGRATION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


# ═════════════════════════════════════════════════════════════════════════════
# D15-4-A — protected history
# ═════════════════════════════════════════════════════════════════════════════
class TestProtectedHistory:
    def test_the_table_is_registered_in_the_existing_mechanism(self):
        from app.employees.router import _PROTECTED_HISTORY_TABLES
        assert ("clock_session_boundary_event", "user_id") in \
            _PROTECTED_HISTORY_TABLES

    def test_no_parallel_protection_mechanism_was_created(self):
        from app.employees import router as er
        src = pathlib.Path(er.__file__).read_text(encoding="utf-8")
        assert src.count("_PROTECTED_HISTORY_TABLES = (") == 1
        assert src.count("def _protected_history(") == 1

    def test_existing_protected_entries_are_unchanged(self):
        from app.employees.router import _PROTECTED_HISTORY_TABLES
        assert _PROTECTED_HISTORY_TABLES[:6] == (
            ("work_day_ledger", "user_id"),
            ("eco_day_record", "user_id"),
            ("leave_input_snapshot", "user_id"),
            ("leave_interval_set", "user_id"),
            ("worker_context_timeline", "user_id"),
            ("config_value", "scope_user_id"),
        )

    def test_a_worker_with_boundary_history_is_protected(self, db):
        from app.employees.router import _protected_history
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.WORKER, actor_label="w")
        db.commit()
        assert "clock_session_boundary_event" in _protected_history(db, WORKER)

    def test_a_worker_without_boundary_history_keeps_existing_behaviour(self, db):
        from app.employees.router import _protected_history
        _session(db, user_id=OTHER)          # a session, but no events
        assert _protected_history(db, OTHER) == []

    def test_the_probe_finds_the_subject_not_the_actor(self, db):
        """An admin who corrected someone else's session is NOT thereby
        protected: the probe keys on the subject Worker."""
        from app.employees.router import _protected_history
        s = _session(db)
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.ADMIN_OVERRIDE,
            actor_label="a@example.test")
        db.commit()
        assert _protected_history(db, WORKER) == ["clock_session_boundary_event"]
        assert _protected_history(db, ADMIN) == []

    def test_deleting_the_actor_neither_destroys_nor_mutates_the_event(self, db):
        """The whole point of actor_label carrying no foreign key."""
        s = _session(db)
        ev = append_boundary_event(
            db, session_id=s.id, user_id=WORKER, boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW, provenance=Provenance.ADMIN_OVERRIDE,
            actor_label="a@example.test")
        db.commit()
        event_id, label = ev.id, ev.actor_label

        db.delete(db.get(dbm.User, ADMIN))
        db.commit()                              # succeeds: no FK to block it
        db.expire_all()

        assert db.get(dbm.User, ADMIN) is None
        survivor = db.get(ClockSessionBoundaryEvent, event_id)
        assert survivor is not None
        assert survivor.actor_label == label     # not nulled, not rewritten
        assert survivor.provenance == "ADMIN_OVERRIDE"


# ═════════════════════════════════════════════════════════════════════════════
# WRITER INTEGRATION — the real production paths, driven end to end
# ═════════════════════════════════════════════════════════════════════════════
# `enforce_desktop_attendance` documents `request is None` as an in-process
# caller and returns, explicitly so a test may call the endpoint function
# directly. These drive the actual writers, not a reimplementation of them.

from fastapi import BackgroundTasks                     # noqa: E402
from app.employees import router as er                  # noqa: E402


def _worker(db):
    return db.get(dbm.User, WORKER)


def _admin(db):
    return db.get(dbm.User, ADMIN)


def _org_local_date(db, when) -> str:
    """The ORG-local calendar date of an instant, in the endpoint's own terms.

    ``admin_clock_override`` locates the session to adjust by the
    ORGANIZATION's local day. A test that means "adjust the session the Worker
    just opened" therefore has to name the day that session is on, and it
    cannot hardcode one: ``clock_in`` stamps ``datetime.now()``, so a literal
    date silently stops matching the moment that date passes. The endpoint then
    finds no session, takes its create branch, and the adjustment lands on a
    SECOND row carrying its own sequence 1 — which reads as an append-only
    defect while actually being a stale fixture.

    Resolved through ``er._tz`` rather than a local ``ZoneInfo`` so the test
    and the endpoint cannot disagree about the boundary: this org sets no
    ``default_timezone``, so both take the same documented fallback.
    """
    if when.tzinfo is None:                      # pysqlite reads back naive
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(er._tz.org_timezone(db, ORG)).date().isoformat()


class TestWorkerClockIn:
    def test_it_records_worker_on_the_open_boundary(self, db):
        er.clock_in(db=db, user=_worker(db), request=None)

        events = _events(db)
        assert len(events) == 1
        ev = events[0]
        assert ev.boundary == BOUNDARY_CLOCK_IN
        assert ev.provenance == "WORKER"
        assert ev.actor_label == "w@example.test"
        assert ev.user_id == WORKER
        assert ev.sequence == 1

    def test_the_event_timestamp_matches_the_row_exactly(self, db):
        er.clock_in(db=db, user=_worker(db), request=None)
        session = db.query(dbm.ClockSession).one()
        ev = _events(db)[0]
        assert ev.session_id == session.id
        assert ev.boundary_at.replace(tzinfo=timezone.utc) == \
            session.clock_in_time.replace(tzinfo=timezone.utc)


class TestWorkerClockOut:
    def test_it_records_worker_on_the_close_boundary(self, db):
        er.clock_in(db=db, user=_worker(db), request=None)
        er.clock_out(BackgroundTasks(), db=db, user=_worker(db),
                     body=None, request=None)

        closes = _events(db, boundary=BOUNDARY_CLOCK_OUT)
        assert len(closes) == 1
        assert closes[0].provenance == "WORKER"
        assert closes[0].actor_label == "w@example.test"

    def test_both_boundaries_end_up_recorded(self, db):
        er.clock_in(db=db, user=_worker(db), request=None)
        er.clock_out(BackgroundTasks(), db=db, user=_worker(db),
                     body=None, request=None)
        assert [(e.boundary, e.provenance) for e in _events(db)] == [
            (BOUNDARY_CLOCK_IN, "WORKER"), (BOUNDARY_CLOCK_OUT, "WORKER"),
        ]


class TestRollbackRecoveryPath:
    """The clock-out endpoint writes `clock_out_time` twice: once normally and
    once in the branch that runs after `db.rollback()` when the break/offline
    recompute fails. Exactly one event must survive."""

    def test_the_recovery_branch_still_records_exactly_one_worker_event(
            self, db, monkeypatch):
        er.clock_in(db=db, user=_worker(db), request=None)
        assert len(_events(db, boundary=BOUNDARY_CLOCK_OUT)) == 0

        def boom(*a, **k):
            raise RuntimeError("break recompute failed")

        monkeypatch.setattr(er, "_actual_break_seconds_for_sessions", boom)
        er.clock_out(BackgroundTasks(), db=db, user=_worker(db),
                     body=None, request=None)

        closes = _events(db, boundary=BOUNDARY_CLOCK_OUT)
        assert len(closes) == 1, [e.sequence for e in closes]
        assert closes[0].provenance == "WORKER"
        assert closes[0].sequence == 1

        session = db.query(dbm.ClockSession).one()
        assert session.clock_out_time is not None

    def test_a_failed_close_leaves_no_event_behind(self, db, monkeypatch):
        """The commit itself fails: boundary write AND event roll back."""
        er.clock_in(db=db, user=_worker(db), request=None)
        opens = len(_events(db, boundary=BOUNDARY_CLOCK_IN))

        real_commit = type(db).commit

        def failing_commit(self, *a, **k):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(type(db), "commit", failing_commit)
        with pytest.raises(Exception):
            er.clock_out(BackgroundTasks(), db=db, user=_worker(db),
                         body=None, request=None)
        monkeypatch.setattr(type(db), "commit", real_commit)
        db.rollback()

        assert _events(db, boundary=BOUNDARY_CLOCK_OUT) == []
        assert len(_events(db, boundary=BOUNDARY_CLOCK_IN)) == opens
        assert db.query(dbm.ClockSession).one().clock_out_time is None



class TestAdminAdjustment:
    def _override(self, db, **kw):
        body = er.AdminClockOverride(**kw)
        return er.admin_clock_override(WORKER, body, db=db, admin=_admin(db))

    def test_a_rewrite_appends_and_never_overwrites(self, db):
        """The centrepiece of D15-3-A, driven through the real endpoint.

        The Worker opens the session (WORKER, sequence 1); an admin then
        rewrites that same boundary (ADMIN_OVERRIDE, sequence 2). Both survive,
        and the Worker's event is not merely still present — every recorded
        field of it is unchanged.
        """
        er.clock_in(db=db, user=_worker(db), request=None)
        original = _events(db, boundary=BOUNDARY_CLOCK_IN)[0]
        before = (original.provenance, original.actor_label,
                  original.boundary_at, original.written_at, original.user_id)
        opened = db.query(dbm.ClockSession).one()

        self._override(db, date=_org_local_date(db, opened.clock_in_time),
                       clock_in_time="09:15")

        opens = _events(db, boundary=BOUNDARY_CLOCK_IN)
        assert [(e.sequence, e.provenance) for e in opens] == [
            (1, "WORKER"), (2, "ADMIN_OVERRIDE"),
        ]
        assert (opens[0].provenance, opens[0].actor_label, opens[0].boundary_at,
                opens[0].written_at, opens[0].user_id) == before
        assert opens[1].actor_label == "a@example.test"
        # The two events disagree about the instant, which is the point: the
        # session row now holds the admin's value, the history holds both.
        assert opens[1].boundary_at != opens[0].boundary_at

    def test_an_untouched_boundary_gets_no_event(self, db):
        """Only the boundary the adjustment actually moved is recorded."""
        er.clock_in(db=db, user=_worker(db), request=None)
        opened = db.query(dbm.ClockSession).one()
        self._override(db, date=_org_local_date(db, opened.clock_in_time),
                       clock_in_time="09:15")

        opens = _events(db, boundary=BOUNDARY_CLOCK_IN)
        assert len(opens) == 2
        # Both on the SAME session, which is what makes the count mean
        # anything: with a stale date the endpoint creates a second session and
        # the count still reads 2, one event on each. Asserting the session
        # keeps this test honest about adjusting the row the Worker opened.
        assert {e.session_id for e in opens} == {opened.id}
        assert _events(db, boundary=BOUNDARY_CLOCK_OUT) == []

    def test_a_close_rewrite_appends_a_second_close_event(self, db):
        """The close-boundary equivalent of the test above.

        Driven at the recorder rather than through ``admin_clock_override``,
        because that endpoint's PRE-EXISTING ``clock_out - clock_in``
        arithmetic cannot run under pysqlite: SQLite returns NAIVE datetimes
        for ``DateTime(timezone=True)`` while the admin's parsed value is
        aware, and mixing them raises. PostgreSQL — production — returns aware
        values, so the branch is exercised there. D-15 did not introduce or
        change that arithmetic, and narrowing it was out of scope, so the
        limitation is recorded here rather than worked around.

        What is verified is the part D-15 owns: a later close write appends and
        the earlier one is untouched.
        """
        er.clock_in(db=db, user=_worker(db), request=None)
        er.clock_out(BackgroundTasks(), db=db, user=_worker(db),
                     body=None, request=None)
        first = _events(db, boundary=BOUNDARY_CLOCK_OUT)[0]
        before = (first.provenance, first.actor_label, first.boundary_at)
        session = db.query(dbm.ClockSession).one()

        append_boundary_event(
            db, session_id=session.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT,
            boundary_at=NOW + timedelta(hours=10),
            provenance=Provenance.ADMIN_OVERRIDE,
            actor_label="a@example.test")
        db.commit()

        closes = _events(db, boundary=BOUNDARY_CLOCK_OUT)
        assert [(e.sequence, e.provenance) for e in closes] == [
            (1, "WORKER"), (2, "ADMIN_OVERRIDE"),
        ]
        assert (closes[0].provenance, closes[0].actor_label,
                closes[0].boundary_at) == before

    def test_a_session_created_by_an_admin_records_both_boundaries(self, db):
        """No session exists for that day, so the endpoint creates one. Neither
        boundary was authored by the Worker."""
        self._override(db, date="2026-09-11", clock_in_time="09:00",
                       clock_out_time="17:00")

        events = _events(db)
        assert {(e.boundary, e.provenance) for e in events} == {
            (BOUNDARY_CLOCK_IN, "ADMIN_OVERRIDE"),
            (BOUNDARY_CLOCK_OUT, "ADMIN_OVERRIDE"),
        }
        assert all(e.actor_label == "a@example.test" for e in events)
        assert all(e.user_id == WORKER for e in events)   # subject, not actor

    def test_the_admin_actor_is_recorded_not_the_subject(self, db):
        self._override(db, date="2026-09-11", clock_in_time="09:00")
        ev = _events(db)[0]
        assert ev.user_id == WORKER
        assert ev.actor_label == "a@example.test"


class TestStaleAutoClose:
    def test_it_records_stale_autoclose_with_a_system_actor(self, db):
        _session(db, clock_in=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc))
        er._auto_close_stale_sessions(
            db, org_id=ORG, now=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc))

        events = _events(db)
        assert len(events) == 1
        assert events[0].boundary == BOUNDARY_CLOCK_OUT
        assert events[0].provenance == "STALE_AUTOCLOSE"
        assert events[0].actor_label == SYSTEM_ACTOR_LABEL

    def test_the_event_records_the_synthesized_instant(self, db):
        _session(db, clock_in=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc))
        er._auto_close_stale_sessions(
            db, org_id=ORG, now=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc))
        session = db.query(dbm.ClockSession).one()
        ev = _events(db)[0]
        assert ev.boundary_at.replace(tzinfo=timezone.utc) == \
            session.clock_out_time.replace(tzinfo=timezone.utc)


class TestAbandonedAutoClose:
    def test_it_records_abandoned_autoclose_with_a_system_actor(
            self, db, monkeypatch):
        import app.attendance_policy as ap
        monkeypatch.setattr(ap, "policy_bool", lambda *a, **k: True)
        monkeypatch.setattr(ap, "policy_int", lambda *a, **k: 10)

        clock_in = datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)
        _session(db, clock_in=clock_in)
        db.add(dbm.LiveStatus(user_id=WORKER,
                              updated_at=clock_in + timedelta(minutes=5)))
        db.commit()

        er._auto_close_abandoned_sessions(
            db, org_id=ORG, now=clock_in + timedelta(hours=3))

        events = _events(db)
        assert len(events) == 1, [(e.boundary, e.provenance) for e in events]
        assert events[0].provenance == "ABANDONED_AUTOCLOSE"
        assert events[0].actor_label == SYSTEM_ACTOR_LABEL
        assert events[0].boundary == BOUNDARY_CLOCK_OUT


class TestOffboardingClosure:
    def _service(self, db, actor_id):
        from app.offboarding.rbac import AuthzContext, Role
        from app.offboarding.services import (
            AuditService, OffboardingContext, TrackingService,
        )
        ctx = OffboardingContext(
            db=db,
            authz=AuthzContext(user_id=actor_id, tenant_id=str(ORG),
                               roles=frozenset({Role.HR_ADMIN})),
            correlation_id="d15-test",
        )
        return TrackingService(ctx, AuditService(ctx))

    def _case(self, db):
        from app.offboarding import ids
        from app.offboarding.models import OffboardingCase
        case = OffboardingCase(
            id=ids.case_id(),
            tenant_id=str(ORG), engagement_id="eng-d15", worker_id=WORKER,
            status="ACCESS_CLOSED", legal_hold_status="NONE",
            revocation_mode="IMMEDIATE", meta={},
        )
        db.add(case)
        db.commit()
        return case

    def test_stop_tracking_records_offboarding(self, db):
        _session(db)
        service = self._service(db, ADMIN)
        service.stop_tracking(self._case(db))
        db.commit()

        events = _events(db)
        assert len(events) == 1
        assert events[0].provenance == "OFFBOARDING"
        assert events[0].boundary == BOUNDARY_CLOCK_OUT
        assert events[0].user_id == WORKER

    def test_it_records_the_real_actor_when_the_case_has_one(self, db):
        _session(db)
        service = self._service(db, ADMIN)
        service.stop_tracking(self._case(db))
        db.commit()
        assert _events(db)[0].actor_label == "user:%d" % ADMIN

    def test_it_falls_back_to_system_when_there_is_no_acting_user(self, db):
        _session(db)
        service = self._service(db, None)
        service.stop_tracking(self._case(db))
        db.commit()
        assert _events(db)[0].actor_label == SYSTEM_ACTOR_LABEL


# ═════════════════════════════════════════════════════════════════════════════
# Layering — what the production attendance route now depends on
# ═════════════════════════════════════════════════════════════════════════════
class TestLayering:
    """`tests/test_worker_history_retention.py` bans direct ECO-package imports
    from `app/employees/router.py`, so the retention probe stays independent of
    whether the ECO tables exist. §45.D-15 requires that same router to use the
    shared `Provenance` vocabulary, which lives in `app.work_ledger.states`.

    Both hold, and these tests state exactly how rather than leaving the guard
    to be read as "the route touches no ECO code at all":

    * the router imports only `app.clock_session_boundary`, which RE-EXPORTS
      the identical enum object — nothing is redefined or duplicated;
    * `app.work_ledger.states` is reached transitively, and it is pure
      vocabulary: enums and frozensets, no models, no tables, no database;
    * `app.work_ledger.models` is NOT reached, so nothing about the retention
      probe's independence from ECO tables has changed.
    """

    @staticmethod
    def _imports(path):
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
        return names

    def test_the_writers_import_their_vocabulary_from_one_place(self):
        for module in (er, __import__("app.offboarding.jobs", fromlist=["x"]),
                       __import__("app.offboarding.services", fromlist=["x"])):
            names = self._imports(module.__file__)
            assert "app.clock_session_boundary" in names, module.__name__
            assert not any(n.startswith("app.work_ledger") for n in names), \
                f"{module.__name__} reaches app.work_ledger directly"

    def test_it_is_the_identical_enum_and_not_a_copy(self):
        """Re-export, not redefinition: no second vocabulary can drift."""
        from app.clock_session_boundary import Provenance as reexported
        from app.work_ledger.states import Provenance as canonical
        assert reexported is canonical
        # 5 session-boundary + 3 interval-level + PRODUCER_DERIVED (D-18A).
        # D-15's five are unchanged by that addition, and the pairing CHECK
        # whitelists them explicitly, so the ninth value stays inadmissible
        # here — asserted in TestBoundaryProvenancePairing below.
        assert len(list(Provenance)) == 9

    def test_the_transitive_reach_is_vocabulary_only(self):
        """`states` must stay free of models and tables, or the retention
        probe's independence from the ECO schema would quietly become false."""
        from app.work_ledger import states
        names = self._imports(states.__file__)
        assert not any("models" in n for n in names), names
        assert "database.database" not in names
        assert not any(n.startswith("sqlalchemy") for n in names), names

    def test_the_router_does_not_reach_the_eco_models(self):
        names = self._imports(er.__file__)
        for banned in ("app.work_ledger.models", "app.eval_day", "app.eval_week",
                       "app.leave_intervals", "app.worker_context"):
            assert not any(n == banned or n.startswith(banned + ".")
                           for n in names), f"router imports {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# Actor labels — proven identical to the established rule, not a second one
# ═════════════════════════════════════════════════════════════════════════════
class TestActorLabelMatchesTheEstablishedRule:
    """`app/clock_session_boundary/service.py` applies ruling E/F's actor-label
    rule without importing `ActorContext`, because that lives in
    `app.eco_threshold_capture` — a package held to a not-wired scope guard
    that importing it from the production attendance path would break.

    So the equivalence is proven HERE instead. A test may import an ECO
    package; production code may not. If either rule ever changes, these fail.
    """

    @staticmethod
    def _user(**kw):
        return type("U", (), kw)()

    @pytest.mark.parametrize("email,user_id", [
        ("w@example.test", 8811),
        ("  spaced@example.test  ", 8811),
        (None, 8811),
        ("", 4242),
        ("   ", 99),
    ])
    def test_it_agrees_with_actor_context_for_every_shape(self, email, user_id):
        from app.eco_threshold_capture.outcomes import ActorContext
        from app.clock_session_boundary.service import (
            ACTOR_LABEL_MAX, actor_label_for_user,
        )

        user = self._user(email=email, id=user_id, role="admin",
                          organization_id=ORG)
        assert actor_label_for_user(user) == \
            ActorContext.for_user(user).label[:ACTOR_LABEL_MAX]

    def test_the_system_sentinel_is_the_same_string(self):
        from app.eco_threshold_capture.outcomes import (
            SYSTEM_ACTOR_LABEL as established,
        )
        assert SYSTEM_ACTOR_LABEL == established == "SYSTEM"

    def test_a_user_with_neither_email_nor_id_is_refused(self):
        """Never silently labelled SYSTEM: that would assert no person acted,
        about an authenticated request."""
        from app.clock_session_boundary.service import actor_label_for_user
        with pytest.raises(MissingActorLabel):
            actor_label_for_user(self._user(email=None, id=None))

    def test_a_long_label_is_truncated_to_the_column_width(self):
        from app.clock_session_boundary.service import (
            ACTOR_LABEL_MAX, actor_label_for_user,
        )
        long_email = ("x" * 80) + "@example.test"
        label = actor_label_for_user(self._user(email=long_email, id=1))
        assert len(label) == ACTOR_LABEL_MAX == 64

    def test_an_id_only_actor_uses_the_established_form(self):
        from app.clock_session_boundary.service import actor_label_for_user_id
        assert actor_label_for_user_id(8812) == "user:8812"
        assert actor_label_for_user_id(None) == SYSTEM_ACTOR_LABEL

    def test_production_does_not_import_the_eco_capture_package(self):
        """The reason this class exists. Pinned so the shortcut is not taken
        later."""
        import app.clock_session_boundary.service as svc
        for module in (svc, er):
            src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    name = node.module
                elif isinstance(node, ast.Import):
                    name = ",".join(a.name for a in node.names)
                if name:
                    assert "eco_threshold" not in name, \
                        f"{module.__name__} imports {name}"


# ═════════════════════════════════════════════════════════════════════════════
# The refusal the owner ruled: the endpoint's own 409, not just the probe
# ═════════════════════════════════════════════════════════════════════════════
class TestDeleteEmployeeRefusesOnBoundaryHistory:
    def test_a_worker_with_boundary_history_is_refused_with_the_ruled_code(
            self, db):
        from fastapi import HTTPException

        er.clock_in(db=db, user=_worker(db), request=None)
        assert _events(db, boundary=BOUNDARY_CLOCK_IN)          # precondition

        with pytest.raises(HTTPException) as exc:
            er.delete_employee(WORKER, db=db, current_user=_admin(db))

        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert detail["error"] == "EMPLOYEE_HAS_PROTECTED_HISTORY"
        assert "clock_session_boundary_event" in detail["tables"]

        # Refused, not partially applied.
        assert db.get(dbm.User, WORKER) is not None
        assert len(_events(db, boundary=BOUNDARY_CLOCK_IN)) == 1

    def test_a_worker_with_no_boundary_history_keeps_existing_behaviour(self, db):
        """D-15 adds no new restriction to a Worker it never recorded."""
        from fastapi import HTTPException

        _session(db, user_id=OTHER)      # a session, deliberately no events
        assert _events(db) == []
        try:
            er.delete_employee(OTHER, db=db, current_user=_admin(db))
        except HTTPException as exc:
            # Whatever the pre-existing behaviour is, it must not be OUR refusal.
            assert "clock_session_boundary_event" not in str(exc.detail)
