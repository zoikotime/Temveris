"""
Temveris — the §11 Policy artifact (POLICY-VERSION).
=====================================================

STATUS: IMPLEMENTED, NOT WIRED. No route, scheduler, evaluator or existing
service calls into this package. ``evaluateDay()`` is untouched,
``EcoDayRecord.policy_version`` stays NULL, and **GV-25 remains blocked**.

WHY IT EXISTS
-------------
The specification's ``evaluateDay()`` resolves a POLICY at step 1 and a
REQUIREMENT at step 2, and returns both identities in the DayRecord at step 15.
The platform had only the requirement. Five §11 policy attributes —
``counts_toward_week``, ``break_treatment_ref``, ``netting_mode``,
``rounding_profile_ref``, ``grace_minutes`` — had nowhere to live, which is why
MODEL-NETTING, MODEL-GRACE and part of P-2 could not be started. This package
gives them an immutable, effective-dated home.

WHAT IT OWNS — AND THE TWO IT DOES NOT
---------------------------------------
    daily_minutes   -> stays in ``ConfigValue`` / Requirement Resolution.
                       GV-27's classification scoping hangs off that row.
    is_working_day  -> stays on ``WorkDayLedger``, frozen at seal (D-18D).

Neither is duplicated here. One fact, one authoritative home.

FOUR VERSION IDENTITIES, KEPT SEPARATE
---------------------------------------
    policy_version        which §11 policy governed the day   (THIS package)
    config_version        which configuration snapshot        (Step 6)
    ledger_version        which sealed evidence               (Work Ledger)
    evaluation_generation which evaluation of that evidence   (EVAL-DAY)

``EcoDayRecord`` already models all four as distinct identity components, and
its docstring records that leaving ``policy_version`` unpopulated "rather than
aliasing it to ``config_version`` avoids inventing a second meaning for one
fact". This package supplies the missing one WITHOUT collapsing any of them.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **Netting, grace, break-classification or rounding BEHAVIOUR.** This records
  WHICH rule was in force; applying it is MODEL-NETTING, MODEL-GRACE, P-2 and
  §16 respectively, each its own increment.
* **Break-treatment or rounding-profile artifacts.** Both are deferred. The two
  ``*_ref`` columns are opaque and must not be used to encode a profile's
  contents.
* **Any live-configuration fallback.** Below the first captured version the
  answer is ``NO_VERSION``, never today's policy.
* **Any requirement or minutes calculation.** ``daily_minutes`` is not here.
"""

from .errors import (
    InvalidPolicyVersion,
    PolicyVersionCollision,
    PolicyVersionError,
    PolicyVersionImmutable,
)
from .models import PolicyVersion, compute_policy_content_hash
from .outcomes import PolicyOutcome, PolicyResolution, UNRESOLVED_OUTCOMES
from .service import capture_policy_version, resolve_policy_as_of
from .vocabulary import (
    DEFAULT_RETROACTIVE_HORIZON_DAYS,
    NETTING_DECISION_PROVENANCES,
    NettingDecisionProvenance,
    NETTING_MODES,
    NettingMode,
    is_valid_netting_mode,
    parse_netting_mode,
)

__all__ = [
    # model + hashing
    "PolicyVersion",
    "compute_policy_content_hash",
    # outcomes
    "PolicyOutcome",
    "PolicyResolution",
    "UNRESOLVED_OUTCOMES",
    # service
    "capture_policy_version",
    "resolve_policy_as_of",
    # vocabulary
    "NettingMode",
    "NETTING_DECISION_PROVENANCES",
    "NettingDecisionProvenance",
    "NETTING_MODES",
    "DEFAULT_RETROACTIVE_HORIZON_DAYS",
    "is_valid_netting_mode",
    "parse_netting_mode",
    # errors
    "PolicyVersionError",
    "InvalidPolicyVersion",
    "PolicyVersionImmutable",
    "PolicyVersionCollision",
]
