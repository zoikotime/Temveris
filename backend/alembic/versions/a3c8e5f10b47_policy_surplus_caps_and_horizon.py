"""D-6.8 / D-7.1 — §15.1's surplus caps and §21.5's retroactive horizon.

Revision ID: a3c8e5f10b47
Revises: f4a7c2b91e68
Create Date: 2026-09-22

ADDITIVE. Three nullable columns on ``policy_version``, each with a
domain CHECK. No existing column is altered, no table is dropped, no index
changes, no row is rewritten, and **no backfill is performed**.

WHAT THESE COLUMNS ARE
----------------------
``surplus_cap_per_day_minutes`` and ``surplus_cap_per_period_minutes`` are
§15.1's two caps, verbatim:

    "Surplus MUST be capped per day and per period, per Tenant configuration,
     to prevent one long day erasing a week."

TWO caps, not one. They are the LIMITS a netting fold is computed under.

``retroactive_horizon_days`` is §21.5's bound:

    "A Tenant-configurable retroactive horizon (default 90 days) bounds how far
     back a retroactive threshold event may be raised. Beyond the horizon the
     recomputation still occurs and the record is still corrected, but no
     notification is sent."

It lives on the versioned policy because the horizon that governs a correction
is the one in force for the WEEK being corrected, not the one in force today —
a question a mutable setting cannot answer.

**THIS REVISION CREATES NO BANK.**
----------------------------------
§15.1 / INV-3: "The bank balance is itself a fold over day records, never a
stored counter." No balance table, counter column, accumulator or persisted
weekly/period bank is introduced here or anywhere else. These three columns are
policy CONTENT — the limits and the horizon — and nothing in this revision
stores a quantity that accumulates.

**NO BACKFILL, DELIBERATELY.**
------------------------------
Every ``policy_version`` row captured before this revision recorded no caps and
no horizon, because the columns did not exist. NULL is therefore the truthful
value and a real answer rather than a missing one.

Writing 90 into the historical rows would be the tempting mistake: §21.5 does
give 90 as the default, so it would look defensible. It is not. The default is
what a Tenant is GIVEN when they state nothing at capture time; asserting it
retroactively claims that every past policy version was configured with a
90-day horizon, which nobody decided. The service applies the default going
forward, at capture, where it is recorded explicitly and visibly. Same
reasoning as ``f4a7c2b91e68`` (scheduled end) and ``f1a6b9c4d283`` (leave
intervals).

WHY NULLABLE RATHER THAN NOT NULL
----------------------------------
For the horizon, NOT NULL is impossible without exactly that fabrication.

For the two caps, NULL carries an ongoing meaning as well: it is "no cap
recorded", which is distinct from a cap of 0 ("bank nothing"). Under
``netting_mode = 'NONE'`` there is no surplus to cap, so NULL is truthful;
under a bank mode a NULL cap is a policy gap that MODEL-NETTING must refuse
rather than paper over. A NOT NULL column with a 0 default would erase that
distinction and silently turn every uncapped bank policy into a bank-nothing
policy.

THE CHECKS ADMIT NULL ON PURPOSE
---------------------------------
``IS NULL OR >= 0`` rather than ``>= 0``: a CHECK that rejected NULL would
force the backfill this revision refuses to perform. The domain is still
enforced for every value that exists, and the resolver applies the same rule.

CONTENT HASH
------------
All three join the EXISTING canonical projection in
``compute_policy_content_hash`` — one digest over the whole policy, not a
second hashing mechanism. Rows captured before this revision keep the hash they
were stored with; nothing recomputes or rewrites them.
"""
from alembic import op
import sqlalchemy as sa


revision = "a3c8e5f10b47"
down_revision = "f4a7c2b91e68"
branch_labels = None
depends_on = None

TABLE = "policy_version"

#: column name -> its non-negativity CHECK constraint name
COLUMNS = {
    "surplus_cap_per_day_minutes":
        "ck_policy_version_surplus_cap_day_nonnegative",
    "surplus_cap_per_period_minutes":
        "ck_policy_version_surplus_cap_period_nonnegative",
    "retroactive_horizon_days":
        "ck_policy_version_retroactive_horizon_nonnegative",
}


def _existing(bind) -> set:
    """Columns already present, so a partial re-run is safe."""
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)

    # SQLite cannot ADD a column and ADD a CHECK separately, so the constraint
    # rides on the column itself. On PostgreSQL this produces the same DDL the
    # ORM model declares, and on both the constraint is named explicitly so a
    # later revision can address it.
    for column, check in COLUMNS.items():
        if column in present:
            continue
        op.add_column(
            TABLE,
            sa.Column(
                column,
                sa.Integer(),
                sa.CheckConstraint(f"{column} IS NULL OR {column} >= 0",
                                   name=check),
                nullable=True,
            ),
        )

    # No UPDATE. No backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)

    # Dropping loses every cap and horizon captured since the upgrade, and
    # nothing can reconstruct them. Guarded so a downgrade on a database that
    # never ran the upgrade is a no-op rather than an error.
    for column in reversed(list(COLUMNS)):
        if column in present:
            op.drop_column(TABLE, column)
