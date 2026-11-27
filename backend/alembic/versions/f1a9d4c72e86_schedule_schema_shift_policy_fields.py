"""schedule schema: shift policy fields, roster off-days, holiday calendars (P3)

Revision ID: f1a9d4c72e86
Revises: e8b3c6d1f472
Create Date: 2026-08-03 00:00:00.000000

Schema only. No business logic, no data change, no backfill (that is P6).

``Shift`` today carries only a scheduled window (``start_time`` / ``end_time`` /
``days_of_week``). The Schedule Resolver needs three more facts from it, and the
P6 backfill needs a fourth:

  * ``expected_work_seconds`` — required WORKED time for the shift, excluding
    breaks. Deliberately NOT ``end_time - start_time``: the window includes
    lunch, so it is not required worked time. This fills the precedence slot
    ``attendance_policy/resolvers.py`` has documented as "intentionally skipped
    — no such field exists yet" since Phase 1.
  * ``crosses_midnight`` — an explicit flag for a night shift (22:00–06:00).
    The resolver currently infers it from ``end <= start``; the column lets an
    org state it for a window where inference is ambiguous.
  * ``config`` — open-ended per-shift rules as JSON. This is the approved
    extensibility contract: future features (overtime multipliers, night
    differential, hybrid/remote day patterns, rotation cycles, break windows)
    become JSON keys, NOT new columns and NOT new tables.
  * ``is_default`` — marks the one shift P6 seeds per organisation.

Scope: FEWER columns than architecture section 4.2
--------------------------------------------------
4.2 also listed ``grace_minutes``, ``late_after_minutes``,
``very_late_after_minutes``, ``half_day_after_minutes``,
``min_hours_required_seconds``, ``allow_early_clock_in``,
``allow_late_clock_out`` and ``timezone`` on ``shifts``. Those are deliberately
NOT added:

  * The resolver has no precedence hook for any of them — verified against
    ``app/schedule_policy/resolver.py``, which reads only ``start_time``,
    ``end_time``, ``days_of_week``, ``expected_work_seconds``,
    ``crosses_midnight``, ``config`` and ``name``. They would be columns nothing
    can read.
  * Per-shift threshold overrides are exactly what ``config`` exists for. Adding
    both a column and a JSON key for the same rule would be duplicate
    configuration.
  * ``timezone`` on a shift would be a SECOND source of truth for a fact that
    ``OrgSettings.default_timezone`` already owns. Validated timezone handling
    belongs to P12.

Each is a nullable additive column if a concrete consumer ever appears.

Production safety
-----------------
* ``shifts`` and ``shift_assignments`` hold 0 rows in production; ``holidays``
  holds 52. Nothing is rewritten and no value changes.
* Every column is nullable or DEFAULT-backed. On PostgreSQL 11+ a
  ``NOT NULL DEFAULT`` addition is a catalog-only change — no table rewrite,
  no long lock.
* ``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` throughout, so
  the migration is idempotent and a partial failure can simply be re-run.
* The holiday uniqueness swap is expression-based and behaviour-preserving:
  ``COALESCE(calendar_key,'')`` is ``''`` for every existing row, so
  ``(organization_id, date)`` remains effectively unique. Postgres DDL is
  transactional, so there is no window in which duplicates could be inserted.
* ``downgrade`` reverses all three parts, including restoring the original
  named UNIQUE constraint.
* ``config`` uses ``json`` rather than ``jsonb`` to match the existing
  ``shifts.days_of_week`` column and stay portable to the SQLite test database.
  Nothing queries inside it; a later ``json``→``jsonb`` conversion is a simple
  ALTER if indexing is ever needed.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f1a9d4c72e86"
down_revision: Union[str, Sequence[str], None] = "e8b3c6d1f472"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HOLIDAY_UNIQUE_CONSTRAINT = "uq_holidays_org_date"
_HOLIDAY_UNIQUE_INDEX = "uq_holidays_org_calendar_date"


def upgrade() -> None:
    # ── A. shifts — policy fields the resolver reads ─────────────────────────
    op.execute(
        """
        ALTER TABLE shifts
            ADD COLUMN IF NOT EXISTS expected_work_seconds INTEGER NULL,
            ADD COLUMN IF NOT EXISTS crosses_midnight BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS config JSON NULL
        """
    )
    # Supports "the default shift for this org", which P6 looks up per tenant.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_shifts_org_default "
        "ON shifts (organization_id, is_default)"
    )

    # ── B. shift_assignments — per-date overrides ───────────────────────────
    # is_off_day expresses a rostered non-working day (rotating shifts, weekly
    # offs that do not follow the weekly pattern) without overloading `status`.
    op.execute(
        """
        ALTER TABLE shift_assignments
            ADD COLUMN IF NOT EXISTS expected_work_seconds INTEGER NULL,
            ADD COLUMN IF NOT EXISTS is_off_day BOOLEAN NOT NULL DEFAULT FALSE
        """
    )

    # ── C. holidays — multiple calendars per org (stored, not yet consumed) ──
    op.execute("ALTER TABLE holidays ADD COLUMN IF NOT EXISTS calendar_key VARCHAR(100) NULL")
    # A UNIQUE CONSTRAINT cannot be expression-based, so the equivalent unique
    # INDEX replaces it — the same approach `uq_app_settings_scope` already uses.
    op.execute(f"ALTER TABLE holidays DROP CONSTRAINT IF EXISTS {_HOLIDAY_UNIQUE_CONSTRAINT}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_HOLIDAY_UNIQUE_INDEX} "
        "ON holidays (organization_id, COALESCE(calendar_key, ''), date)"
    )


def downgrade() -> None:
    # C — restore the original named constraint. Safe because calendar_key is
    # uniformly NULL while nothing writes it, so no row can violate the
    # narrower (organization_id, date) form.
    op.execute(f"DROP INDEX IF EXISTS {_HOLIDAY_UNIQUE_INDEX}")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{_HOLIDAY_UNIQUE_CONSTRAINT}'
            ) THEN
                ALTER TABLE holidays
                    ADD CONSTRAINT {_HOLIDAY_UNIQUE_CONSTRAINT}
                    UNIQUE (organization_id, date);
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE holidays DROP COLUMN IF EXISTS calendar_key")

    # B
    op.execute(
        """
        ALTER TABLE shift_assignments
            DROP COLUMN IF EXISTS is_off_day,
            DROP COLUMN IF EXISTS expected_work_seconds
        """
    )

    # A
    op.execute("DROP INDEX IF EXISTS ix_shifts_org_default")
    op.execute(
        """
        ALTER TABLE shifts
            DROP COLUMN IF EXISTS config,
            DROP COLUMN IF EXISTS is_default,
            DROP COLUMN IF EXISTS crosses_midnight,
            DROP COLUMN IF EXISTS expected_work_seconds
        """
    )
