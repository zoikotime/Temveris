"""holidays: country-scoped calendars (country_code + widened uniqueness)

An organization could hold exactly ONE holiday per date, ever. The uniqueness
rule was ``(organization_id, COALESCE(calendar_key,''), date)`` and there was no
country column at all, so:

  * a workspace operating in both the US and India could not record
    2026-07-04 twice — one calendar silently blocked the other;
  * two genuinely distinct holidays on one date were unstorable (Gandhi Jayanti
    and Dussehra both fell on 2025-10-02);
  * nothing recorded WHICH country a stored holiday belonged to, so the admin
    UI's country selector could only ever preview an external calendar it was
    unable to save.

This migration adds ``holidays.country_code`` (ISO 3166-1 alpha-2, nullable) and
widens the unique index to include both ``country_code`` and ``name``.

Deliberately NOT backfilling
----------------------------
Existing rows keep ``country_code IS NULL``, meaning "unscoped". Reads treat
NULL as belonging to every country filter, so no holiday disappears from the
employee calendar, attendance, the schedule resolver, reports or payroll.

Backfilling from ``org_settings.primary_country`` was considered and rejected:
the audit found organizations 11 (Demo) and 12 (ZR Solutions) carry
``primary_country = 'US'`` while holding the full Indian gazetted calendar. A
blanket backfill would have stamped "US" onto Diwali and Republic Day and made
the existing data error permanent and invisible. Classification is therefore an
explicit, reviewable operator step:

    python scripts/holidays_country_report.py            # read-only inventory
    python scripts/holidays_country_report.py --apply    # only unambiguous orgs

Widening is safe on populated data
----------------------------------
Every column added to the key makes it MORE permissive. Any row set that
satisfied ``(org, calendar_key, date)`` also satisfies
``(org, calendar_key, country_code, date, name)``, so the new index cannot fail
to build on an existing database.

All DDL is ``IF EXISTS`` / ``IF NOT EXISTS`` guarded and re-runnable — this
repository has a documented history of ``alembic_version`` sitting ahead of DDL
that never executed (see migration c1d4e7f2a9b6), so idempotency is the house
rule, not a nicety.

Revision ID: m7n2p5q8r1t4
Revises: c7f4a1b83d95
Create Date: 2026-08-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m7n2p5q8r1t4"
down_revision: Union[str, Sequence[str], None] = "c7f4a1b83d95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Index names, held as constants so upgrade and downgrade cannot drift apart.
_OLD_UNIQUE_INDEX = "uq_holidays_org_calendar_date"
_NEW_UNIQUE_INDEX = "uq_holidays_org_calendar_country_date_name"
_COUNTRY_INDEX = "ix_holidays_country_code"
_ORG_COUNTRY_INDEX = "ix_holidays_org_country_date"


def upgrade() -> None:
    # 1 — the column. Nullable, no server default: NULL is a meaningful value
    #     here ("unscoped"), not a placeholder waiting to be filled.
    op.execute("ALTER TABLE holidays ADD COLUMN IF NOT EXISTS country_code VARCHAR(2) NULL")

    # 2 — the widened uniqueness rule, created BEFORE the old one is dropped so
    #     the table is never briefly unconstrained.
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_NEW_UNIQUE_INDEX} "
        "ON holidays (organization_id, COALESCE(calendar_key, ''::character varying), "
        "COALESCE(country_code, ''::character varying), date, name)"
    )

    # 3 — retire the narrower rules. Both historical spellings are dropped:
    #     the expression index from f1a9d4c72e86 and the plain named constraint
    #     from c1d4e7f2a9b6, since a given database may carry either.
    op.execute(f"DROP INDEX IF EXISTS {_OLD_UNIQUE_INDEX}")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE holidays DROP CONSTRAINT uq_holidays_org_date;
        EXCEPTION WHEN undefined_object THEN NULL;
        END $$
        """
    )
    # Belt and braces: the same name may exist as a bare index rather than a
    # constraint on databases materialised by create_all.
    op.execute("DROP INDEX IF EXISTS uq_holidays_org_date")

    # 4 — read paths. The admin page and every consumer filter by
    #     (organization_id, country_code) and usually order by date.
    op.execute(f"CREATE INDEX IF NOT EXISTS {_COUNTRY_INDEX} ON holidays (country_code)")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_ORG_COUNTRY_INDEX} "
        "ON holidays (organization_id, country_code, date)"
    )


def downgrade() -> None:
    # Restoring the narrower key can only succeed if no organization has taken
    # advantage of the wider one. Fail loudly rather than destroying rows: an
    # operator who really wants to go back must first decide what to do with the
    # holidays that the old rule cannot represent.
    conn = op.get_bind()
    clashes = conn.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT organization_id, COALESCE(calendar_key, '') AS ck, date
                FROM holidays
                GROUP BY 1, 2, 3
                HAVING count(*) > 1
            ) AS dupes
            """
        )
    ).scalar()
    if clashes:
        raise RuntimeError(
            f"Cannot downgrade: {clashes} (organization, calendar, date) group(s) now "
            "hold more than one holiday, which the previous uniqueness rule forbids. "
            "Resolve or export those rows before downgrading."
        )

    op.execute(f"DROP INDEX IF EXISTS {_ORG_COUNTRY_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_COUNTRY_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_OLD_UNIQUE_INDEX} "
        "ON holidays (organization_id, COALESCE(calendar_key, ''::character varying), date)"
    )
    op.execute(f"DROP INDEX IF EXISTS {_NEW_UNIQUE_INDEX}")
    op.execute("ALTER TABLE holidays DROP COLUMN IF EXISTS country_code")
