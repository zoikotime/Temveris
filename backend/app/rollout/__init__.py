"""
Temveris — Durable rollout controls (Step 8)
=============================================

STATUS: IMPLEMENTED, NOT WIRED, NOTHING ENABLED.

    Early Clock-Out v2 rollout = OFF, for every organization, with zero rows.

WHAT THIS IS
------------
Infrastructure for turning a capability on for one organization at a time,
durably, reversibly and auditably. It governs no behaviour of its own: this
package contains no Early Clock-Out logic, no calculation, and no knowledge of
what any control does. It answers one question — "is this control on for this
organization?" — and records how it came to be that way.

    OFF ──▶ SHADOW ──▶ ENABLED, with rollback from anywhere.

FIVE PROPERTIES THAT MATTER
---------------------------
1. **Durable.** State lives in one table and nowhere else. There is no
   in-memory cache — a cached rollout state is a rollback that does not take
   effect. It survives restart because it was never in the process.
2. **OFF by default, and by accident.** No row means OFF. A missing, empty,
   misspelled, wrongly-cased, wrongly-typed or corrupted value means OFF. A
   database error while reading means OFF. There is no input except the exact
   string ``"ENABLED"`` that produces an enabled control.
3. **No global switch.** State is keyed on ``(organization_id, control_key)``
   and there is no row shape that means "everyone". A broad rollout is N
   deliberate transitions, which is the point.
4. **Observation is structural.** ``OFF → ENABLED`` is not a transition. The
   only route to ENABLED is through SHADOW, and leaving SHADOW forwards
   requires the configured minimum period to have elapsed. Nothing promotes
   itself: there is no scheduler, no automatic advance, no code path that
   raises a state without being asked to.
5. **Every change is on the canonical audit chain.** Transitions write
   ``AuditLog`` rows carrying organization, previous state, new state, actor,
   time, reason and version — the repository's existing evidence chain, not a
   second audit system.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **A global or platform-wide switch.** If one is ever genuinely required, that
  is a governance decision to raise, not a convenience to add.
* **Canary SELECTION.** A canary is designated explicitly by an administrator.
  Nothing here infers one from organization size, activity, Worker count,
  configuration, geography, or a hash bucket.
* **Automatic promotion.** The observation window is a floor on time, never a
  trigger. It gates a human's request; it does not make one.
* **Any judgement about whether the thing being rolled out is CORRECT.** This
  layer measures elapsed time, not quality.
* **Any Early Clock-Out calculation, UI, API or notification**, and any import
  of the ECO packages. Asserted by an import-graph test.
* **A path to ECO v1.** v1's gate is the source constant
  ``EARLY_CLOCK_OUT_FEATURE_ENABLED = False``, checked before any durable
  setting is consulted. No rollout state can reach it, and a test pins that.

RELATIONSHIP TO THE EXISTING FLAG SYSTEM
----------------------------------------
``app/commercial/phase5.py`` holds feature flags in a process-global dict that
resets on restart, and ``app/commercial/enforcement.py`` layers an
off/shadow/enforce ladder on top of it plus an environment variable and an
in-process kill switch. That system is deliberately not extended or reused:
it is not durable (the property this step exists to provide), and it keys on
the STRING ``commercial_organizations.id`` rather than the integer
``organizations.id`` that owner ruling D-09 Option C makes canonical. Neither
is touched by this package.
"""

from .errors import (
    EnablementGatesNotSatisfied,
    InvalidRolloutTransition,
    ObservationWindowNotElapsed,
    RolloutError,
    RolloutOrganizationRequired,
    StaleRolloutVersion,
    UnknownRolloutControl,
)
from .models import RolloutControl
from .service import (
    KNOWN_CONTROLS,
    RolloutStatus,
    canary_organizations,
    get_status,
    is_enabled,
    is_shadow,
    organizations_in_state,
    rollout_state,
    set_canary,
    set_observation_days,
    transition,
)
from .states import (
    ALLOWED_TRANSITIONS,
    CONTROL_EARLY_CLOCK_OUT_V2,
    DEFAULT_MIN_OBSERVATION_DAYS,
    DEFAULT_STATE,
    ROLLBACK_TRANSITIONS,
    RolloutState,
    can_transition,
    coerce_state,
    is_rollback,
    is_valid_state,
    requires_observation_window,
)

__all__ = [
    # states
    "RolloutState",
    "ALLOWED_TRANSITIONS",
    "ROLLBACK_TRANSITIONS",
    "DEFAULT_STATE",
    "DEFAULT_MIN_OBSERVATION_DAYS",
    "CONTROL_EARLY_CLOCK_OUT_V2",
    "coerce_state",
    "is_valid_state",
    "can_transition",
    "is_rollback",
    "requires_observation_window",
    # model
    "RolloutControl",
    # service
    "RolloutStatus",
    "KNOWN_CONTROLS",
    "rollout_state",
    "is_enabled",
    "is_shadow",
    "get_status",
    "organizations_in_state",
    "canary_organizations",
    "transition",
    "set_canary",
    "set_observation_days",
    # errors
    "RolloutError",
    "EnablementGatesNotSatisfied",
    "InvalidRolloutTransition",
    "ObservationWindowNotElapsed",
    "StaleRolloutVersion",
    "UnknownRolloutControl",
    "RolloutOrganizationRequired",
]
