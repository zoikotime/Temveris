"""D18-F2 narrowed: admit synthesized departure provenance for §24.7

§24.7 "Unverified Exit — Pending Review" requires a day whose exit exists but
was not performed by the Worker or an administrator to be SEALED and its
evaluation DEFERRED. §19 step 5 loads sealed intervals and step 6 then defers,
so the day must have a ledger row — and that row must be able to record the
exit it actually has.

D18-F2 previously admitted VERIFIED provenance only. That left two impossible
choices for such a day: discard it, or record it as MISSING_CLOCK_OUT. §19.1
forbids the second outright — "A deferred day is not a missing clock-out and
must not be reported as one" — and the first loses evidence the platform
already holds. The owner therefore narrowly superseded the constraint.

ADMISSIBLE IS NOT VERIFIED
--------------------------
This widens STORAGE, nothing else. ``VERIFIED_DEPARTURE_PROVENANCE`` is
unchanged and every verified-departure decision still reads it, so a
synthesized close still cannot satisfy a verified-departure requirement
anywhere in the application. The constraint's job is to stop an unrecognised
value reaching the column; it was never the thing that made a departure
verified.

    before: WORKER, ADMIN_OVERRIDE
    after : WORKER, ADMIN_OVERRIDE, STALE_AUTOCLOSE, ABANDONED_AUTOCLOSE,
            OFFBOARDING

The vocabulary is not otherwise broadened: no sixth provenance value is
created, and an unknown string is still rejected.

SAFETY
------
Constraint-only and strictly widening. No column is added, altered or dropped;
no row is read, rewritten or deleted; every row valid before is valid after.

DIALECT
-------
SQLite cannot ALTER a CHECK constraint, and its reflection does not carry the
constraint's name, so a batch rebuild cannot find it to drop. The D-18
correction revision (``e7c2b9d4f815``) already met this and settled the
pattern: emit the constraint DDL only on dialects that support it, and let
SQLite take the definition from the model through ``create_all``. The model
already carries the widened CHECK, so a SQLite database built either way ends
up with identical rules.

The downgrade REFUSES rather than corrupting. Narrowing the constraint while a
synthesized row exists would make already-persisted, append-only evidence
invalid — so it counts first and raises with the count. An empty table
downgrades cleanly.

Revision ID: c3f7a1e58d2b
Revises: b2e6d9f04a17
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision = "c3f7a1e58d2b"
down_revision = "b2e6d9f04a17"
branch_labels = None
depends_on = None

_TABLE = "work_day_ledger"
_NAME = "ck_work_day_ledger_departure_verified"

# Restated rather than imported: a migration must keep running against the
# schema of its own moment, and importing the model would couple it to whatever
# that model becomes several revisions later. A test asserts these agree with
# the enums.
_VERIFIED = ("ADMIN_OVERRIDE", "WORKER")
_SYNTHESIZED = ("ABANDONED_AUTOCLOSE", "OFFBOARDING", "STALE_AUTOCLOSE")
_ADMISSIBLE = tuple(sorted(_VERIFIED + _SYNTHESIZED))


def _check(values) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"departure_provenance IS NULL OR departure_provenance IN ({rendered})"


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Nothing to alter: SQLite got its CHECK from the model at create_all,
        # and the model already carries the widened one.
        return
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _check(_ADMISSIBLE))


def downgrade() -> None:
    bind = op.get_bind()
    # The refusal is a data question, not a DDL one, so it is checked on every
    # dialect — including the ones that will then skip the DDL below.
    blocking = bind.execute(sa.text(
        f"SELECT count(*) FROM {_TABLE} "  # noqa: S608 - fixed identifier
        f"WHERE departure_provenance IN "
        f"({', '.join(repr(v) for v in _SYNTHESIZED)})"
    )).scalar()
    if blocking:
        raise RuntimeError(
            f"cannot downgrade: {blocking} sealed row(s) carry synthesized "
            f"departure provenance, which the narrower D18-F2 constraint "
            f"would reject. The ledger is append-only, so those rows cannot "
            f"be rewritten to satisfy it — resolve the exits first, or stay "
            f"on this revision."
        )
    if bind.dialect.name == "sqlite":
        return
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _check(_VERIFIED))
