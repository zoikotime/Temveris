"""Worker billing states — ZT-COM-BILL-001 §17 / §A4 / §H3.

The legacy ``users.is_active`` / ``users.is_invited`` booleans cannot express
the commercial policy:

* ``is_invited`` is never cleared on acceptance, so it means "was invited at
  some point", not "invitation is pending". In production every one of the 21
  org-1 users carrying ``is_invited=True`` has login *and* clock-in history.
* ``is_active=False`` collapses suspended, deactivated, terminated and archived
  into one value, but suspension stays billable while the rest do not.

``users.billing_state`` is therefore the single authority for whether a worker
occupies a paid seat. Never infer billability from the booleans again.
"""

from __future__ import annotations

from typing import FrozenSet

# ── Policy states (§17) ───────────────────────────────────────────────────────
INVITED_PENDING = "INVITED_PENDING"      # invited, not yet accepted — reserves capacity, not billed
ACTIVE_BILLABLE = "ACTIVE_BILLABLE"      # accepted and working — billed
SUSPENDED_BILLABLE = "SUSPENDED_BILLABLE"  # temporary access block, employment continues — billed (§A4)
DEACTIVATED = "DEACTIVATED"              # access ended — not billed
TERMINATED = "TERMINATED"                # employment ended — not billed
ARCHIVED = "ARCHIVED"                    # retained for records only — not billed

# ── Migration safety valve (§2) ───────────────────────────────────────────────
# Applied where the legacy booleans genuinely cannot be resolved to a policy
# state. Deliberately NOT billable, so an unresolved row can never silently
# inflate an invoice. Every row in this state is an open business question.
MIGRATION_REVIEW = "MIGRATION_REVIEW"

ALL_STATES: FrozenSet[str] = frozenset({
    INVITED_PENDING,
    ACTIVE_BILLABLE,
    SUSPENDED_BILLABLE,
    DEACTIVATED,
    TERMINATED,
    ARCHIVED,
    MIGRATION_REVIEW,
})

# The only states that occupy a paid seat.
BILLABLE_STATES: FrozenSet[str] = frozenset({
    ACTIVE_BILLABLE,
    SUSPENDED_BILLABLE,
})

# States that hold plan capacity without being charged (§A5 — 7-day reservation).
# Capacity enforcement itself is a later phase; this records the intent.
RESERVING_STATES: FrozenSet[str] = frozenset({
    INVITED_PENDING,
})


def is_billable(state: str | None) -> bool:
    """True only for states that occupy a paid seat.

    ``None`` (a row the backfill has not reached) is treated as not billable —
    unknown must never mean chargeable.
    """
    return state in BILLABLE_STATES
