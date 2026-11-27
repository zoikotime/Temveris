"""backfill organization_id on NULL-org app_settings rows (P2.5)

Revision ID: c4a7e2b915d3
Revises: d5e8a1c34b70
Create Date: 2026-08-02 00:00:00.000000

Two org-provisioning paths disagreed about how to scope a settings row:

  * ``app.commercial.provisioning`` set ``organization_id`` AND used an
    org-suffixed key (``shared_pool_enabled:<org_id>``).
  * ``app.super_admin.router.create_organization`` set only the suffixed key,
    leaving ``organization_id`` NULL.

Rows from the second path therefore landed in the shared NULL-organization
bucket, which any org-less read can pick up (verified in the P2 audit: a
``organization_id IS NULL`` filter returns them). The code path is fixed in P2;
this migration repairs the rows already written.

The owning org is recovered from the key suffix, which is authoritative — it is
how the leave readers locate the row today.

Data-only. No DDL, no schema change, no lock of consequence (6 rows in
production at the time of writing).

Safety properties
-----------------
* **Idempotent** — the WHERE clause only matches rows that are still NULL, so
  re-running is a no-op.
* **Never invents a tenant** — the ``EXISTS`` guard skips any row whose derived
  org id has no ``organizations`` row, so a dangling reference can never be
  written. Such rows are left NULL for manual review rather than guessed at.
* **Cannot violate uniqueness** — ``uq_app_settings_scope`` is
  ``(organization_id, COALESCE(team_key,''), COALESCE(user_id,0), key)``. The
  second guard skips any row whose target ``(org, key)`` already exists, so the
  UPDATE cannot collide. Verified zero collisions before writing this.
* **Backward compatible** — the leave readers match on the suffixed key alone
  and ignore ``organization_id``, so their behaviour is unchanged.
* **Downgrade is an intentional no-op** — see ``downgrade()``. Both provisioning
  paths produce the same end state (``organization_id`` == the id in the key), so
  a reverse UPDATE could not tell a row this migration repaired from one the
  commercial path always wrote correctly, and would corrupt the latter.

Scope is deliberately pinned to the two keys the buggy provisioning path wrote.
A broader "any NULL org" sweep could capture rows whose NULL is meaningful.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4a7e2b915d3"
down_revision: Union[str, Sequence[str], None] = "d5e8a1c34b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Only the keys written by the defective provisioning path.
_KEY_PATTERN = r"^(shared_pool_enabled|leave_waiting_period_months):[0-9]+$"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE app_settings AS s
           SET organization_id = CAST(split_part(s.key, ':', 2) AS INTEGER)
        WHERE s.organization_id IS NULL
          AND s.key ~ '{_KEY_PATTERN}'
          AND EXISTS (
                SELECT 1 FROM organizations o
                WHERE o.id = CAST(split_part(s.key, ':', 2) AS INTEGER)
          )
          AND NOT EXISTS (
                SELECT 1 FROM app_settings t
                WHERE t.key = s.key
                  AND t.organization_id = CAST(split_part(s.key, ':', 2) AS INTEGER)
          )
        """
    )


def downgrade() -> None:
    """Intentional no-op.

    The rows this migration repairs become indistinguishable from rows the
    commercial provisioning path always wrote correctly: both end up with
    ``organization_id`` equal to the id encoded in the key. A reverse UPDATE
    matching that shape would therefore also NULL out correctly-provisioned
    rows — re-creating the very cross-tenant exposure this fixes, for tenants
    that never had the bug.

    Leaving the repair in place on downgrade is safe: the leave readers match on
    the suffixed key and ignore ``organization_id``, so no code path behaves
    differently. To revert a specific tenant, NULL that row by id manually.
    """
    pass
