"""add ON DELETE RESTRICT org foreign keys to settings/schedule tables (D11)

Revision ID: e8b3c6d1f472
Revises: c4a7e2b915d3
Create Date: 2026-08-02 00:00:00.000000

``app_settings``, ``shifts``, ``shift_assignments`` and ``holidays`` all carry an
``organization_id`` and are all read through it for tenant isolation, but the
live database has **no foreign key on any of them** — the columns were added by
raw ALTER (or the tables by ``create_all`` before the column existed), so the
constraint was never created. Verified against production: none of the four
appears among the 14 FKs referencing ``organizations``.

Consequence today: deleting an organization row leaves these rows pointing at a
tenant that no longer exists — silent referential corruption.

Why RESTRICT and not CASCADE or SET NULL
----------------------------------------
Organizations are **soft-lifecycle** entities. Evidence gathered in the P2.5
audit:

  * no ``db.delete(Organization)``, no ``DELETE FROM organizations`` and no
    DELETE route for organizations anywhere in the codebase;
  * lifecycle is a state machine on ``CommercialOrganization.commercial_state``
    (15 states incl. ``suspended`` / ``cancelled`` / ``closed``), driven by a
    reversible ``/organizations/{id}/suspend`` + ``/restore`` pair;
  * ``Organization.is_active`` exists but is read by no code path;
  * the GDPR erasure flow is request-tracking only (``DataRightsRequest``); no
    code path hard-deletes tenant rows.

So a hard delete is never expected. Given that:

  * ``CASCADE`` would let one stray DELETE silently destroy a tenant's entire
    configuration — schedule policy, shifts, rosters and holiday calendar — with
    no recovery, on a product whose whole lifecycle model is retention.
  * ``SET NULL`` (what the models declared, though it was never created) would
    dump those rows into the shared NULL-organization bucket, where an org-less
    read can pick them up. That is the exposure repaired in P2/P2.5 — this must
    not re-introduce a path back to it.
  * ``RESTRICT`` makes an accidental hard delete **fail loudly** and leaves the
    data intact. Deleting an organization becomes a deliberate act requiring its
    dependent rows to be removed first.

``teams`` is intentionally untouched: it already has
``fk_teams_organization_id`` with ON DELETE SET NULL. Changing an existing
constraint is a different decision from adding a missing one and is not in this
migration's scope. (It also holds 2 rows with a NULL organization_id.)

Production safety
-----------------
* **Idempotent** — each constraint is added only if absent, so re-running is a
  no-op and a partial failure can simply be re-run.
* **Short locks** — added ``NOT VALID`` first (a brief catalog-only lock), then
  ``VALIDATE CONSTRAINT`` separately. VALIDATE takes SHARE UPDATE EXCLUSIVE,
  which permits concurrent reads *and* writes, so the row scan never blocks
  traffic. This matters for ``shift_assignments``, which grows per user per day.
* **Pre-flight guard** — aborts with an explicit per-table count if any dangling
  ``organization_id`` exists, rather than failing opaquely inside VALIDATE.
  Verified 0 dangling across all four tables before writing this.
* **No data rewrite** — constraints only. ``organization_id`` stays nullable;
  NULL is unconstrained by a foreign key, so the rows the backfill could not
  attribute are unaffected.
* **Reversible** — ``downgrade`` drops exactly these four constraints.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e8b3c6d1f472"
down_revision: Union[str, Sequence[str], None] = "c4a7e2b915d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> constraint name. Follows the fk_<table>_organization_id convention
# already used by fk_teams_organization_id / fk_org_settings_organization_id.
_TABLES = {
    "app_settings":      "fk_app_settings_organization_id",
    "shifts":            "fk_shifts_organization_id",
    "shift_assignments": "fk_shift_assignments_organization_id",
    "holidays":          "fk_holidays_organization_id",
}


def upgrade() -> None:
    conn = op.get_bind()

    # ── Pre-flight: a dangling organization_id would make VALIDATE fail with a
    # generic message. Surface exactly which table and how many rows instead.
    offenders = []
    for table in _TABLES:
        n = conn.exec_driver_sql(
            f"""
            SELECT COUNT(*) FROM {table} x
            WHERE x.organization_id IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM organizations o WHERE o.id = x.organization_id
              )
            """
        ).scalar()
        if n:
            offenders.append(f"{table}={n}")
    if offenders:
        raise RuntimeError(
            "Cannot add organization foreign keys: rows reference a missing "
            "organization (" + ", ".join(offenders) + "). Re-point or delete "
            "those rows first; this migration will not guess an owner."
        )

    for table, constraint in _TABLES.items():
        # ADD CONSTRAINT has no IF NOT EXISTS in PostgreSQL — guard on the catalog.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{constraint}'
                ) THEN
                    ALTER TABLE {table}
                        ADD CONSTRAINT {constraint}
                        FOREIGN KEY (organization_id)
                        REFERENCES organizations (id)
                        ON DELETE RESTRICT
                        NOT VALID;
                END IF;
            END $$;
            """
        )
        # Separate step: validates existing rows under a lock that still allows
        # reads and writes. Safe to repeat — validating an already-valid
        # constraint is a no-op.
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint};")


def downgrade() -> None:
    for table, constraint in _TABLES.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint};")
