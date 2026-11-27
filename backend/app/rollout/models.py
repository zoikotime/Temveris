"""
Durable rollout control — SQLAlchemy model.
===========================================

STEP 8 of the owner-authorised Phase 2 foundation.

ONE ROW PER (ORGANIZATION, CONTROL). NO ROW MEANS OFF.
------------------------------------------------------
Absence is the default, not a special case. A control nobody has ever
transitioned has no row, and the read path returns OFF for it — so a fresh
database, a new organization, and a restored backup all start OFF without
anything having to be seeded. At the end of Step 8 the Early Clock-Out control
has zero rows, which is precisely the required final state.

WHY THIS IS NOT AppSettings
---------------------------
``app_settings`` is durable and organization-scoped and would technically hold a
string. It is the wrong home:

* it is written by 194 call sites across 30 modules through generic key/value
  endpoints, so a rollout state living there could be changed by any code path
  that writes settings — which is exactly the accidental enablement this control
  exists to prevent;
* it carries no version column, so two concurrent administrators silently
  overwrite each other; and
* it has no notion of a transition, so "OFF → ENABLED" could be written
  directly, skipping observation.

A dedicated table makes the state reachable only through a service that
validates the transition.

WHY THERE IS NO CHECK CONSTRAINT ON ``state``
---------------------------------------------
Deliberate, and the one place this schema is less strict on purpose. The
load-bearing guarantee is that a corrupted or unrecognised value READS as OFF
(``states.coerce_state``). A CHECK would make that path unreachable from a test,
leaving the guarantee asserted but never exercised — and the guarantee matters
precisely in the situation where something has already gone wrong and written a
value the application did not expect. The write path validates; the read path
defends; both are tested end to end.

VOCABULARY
----------
``organization_id`` is the INTEGER ``organizations.id``, per owner ruling D-09
Option C. Note that ``commercial.phase5.is_enabled`` takes the STRING
``commercial_organizations.id`` — a different identifier for a different table.
That vocabulary is deliberately not adopted here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)

from database.database import Base

from .states import DEFAULT_MIN_OBSERVATION_DAYS, RolloutState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RolloutControl(Base):
    """Durable rollout state for one control in one organization.

    Survives process restart by construction — the state lives here and nowhere
    else. There is no in-memory cache of it, deliberately: a cached rollout
    state is a rollback that does not take effect.
    """

    __tablename__ = "rollout_control"

    id = Column(Integer, primary_key=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Which control. Declared in ``states``; unknown keys are refused by the
    #: service rather than created on demand, so a typo cannot produce a second
    #: permanently-OFF control that looks real in an admin list.
    control_key = Column(String(64), nullable=False)

    #: OFF | SHADOW | ENABLED, as text. Not a database enum: §32 warns enum
    #: values cannot be corrected later without a breaking change, and a
    #: PostgreSQL enum type would need a migration to add a state. See the
    #: module docstring for why there is no CHECK constraint either.
    state = Column(String(16), nullable=False, default=RolloutState.OFF.value)

    #: Optimistic-concurrency token. Incremented on every accepted transition;
    #: a transition computed against a stale value is rejected, never merged.
    state_version = Column(Integer, nullable=False, default=1)

    #: Explicitly designated by an administrator. Never inferred from size,
    #: activity, Worker count, geography or a hash — designating a canary is a
    #: decision somebody makes, not a bucket somebody falls into.
    is_canary = Column(Boolean, nullable=False, default=False)

    #: Days required in SHADOW before ENABLED may be requested. Per-control so a
    #: longer observation can be demanded without a code change.
    min_observation_days = Column(
        Integer, nullable=False, default=DEFAULT_MIN_OBSERVATION_DAYS,
    )

    #: When the CURRENT period of observation began. Reset on each fresh entry
    #: into SHADOW, so a control that was rolled back and re-shadowed observes
    #: again from that point rather than inheriting credit for the earlier run.
    shadow_entered_at = Column(DateTime(timezone=True), nullable=True)

    #: When this control most recently went live. Cleared on rollback.
    enabled_at = Column(DateTime(timezone=True), nullable=True)

    last_transition_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    #: Free-text actor label. An id where one exists, "SYSTEM" otherwise. The
    #: acting user id proper goes on the AuditLog row; this column is for
    #: showing "who last touched this" without a join.
    last_transition_by = Column(String(64), nullable=False, default="SYSTEM")
    last_transition_reason = Column(Text, nullable=True)

    __table_args__ = (
        # One row per control per organization. This is also what makes a
        # global switch unrepresentable: there is no row shape that means
        # "every organization".
        UniqueConstraint(
            "organization_id", "control_key",
            name="uq_rollout_control_org_key",
        ),
        Index("ix_rollout_control_key_state", "control_key", "state"),
        CheckConstraint("state_version >= 1",
                        name="ck_rollout_control_version_positive"),
        CheckConstraint("min_observation_days >= 0",
                        name="ck_rollout_control_observation_nonnegative"),
        CheckConstraint("length(control_key) > 0",
                        name="ck_rollout_control_key_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<RolloutControl org={self.organization_id} "
            f"{self.control_key}={self.state} v={self.state_version}>"
        )


__all__ = ["RolloutControl"]
