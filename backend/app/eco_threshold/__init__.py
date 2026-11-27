"""
Temveris — the ECO weekly threshold reader (read path only)
============================================================

ZT-SPEC-ECO-002 v2.0, W5. Answers one question and nothing else:

    what ECO threshold governed this organization's workweek?

STATUS: IMPLEMENTED, NOT WIRED. No route, service or scheduler uses it.

WHAT THIS PACKAGE IS
--------------------
* the canonical threshold configuration identity (``keys``);
* a typed, fail-closed result (``outcomes``);
* a storage seam of its own, separate from Step 5's (``ports``);
* the resolution rules (``reader``);
* a database-backed provider over the existing Step 6 tables (``adapter``);
* an in-memory reference provider, the executable specification (``reference``).

WHAT THIS PACKAGE DELIBERATELY IS NOT
-------------------------------------
* **No threshold comparison.** ``W >= T`` belongs to EVAL-EVENT. This package
  produces T and forms no opinion about any W.
* **No capture.** Nothing here writes configuration. Who may set a threshold,
  under what authority, with what actor and what audit record, is unruled — so
  no capture path, no route, no API, no permission.
* **No default.** There is no initial value, no fallback, and no borrowed
  figure from the daily expected-hours, break or risk settings. An organization
  with no captured threshold resolves to a non-resolved outcome, permanently,
  until one is captured. That is the intended behaviour, not a gap.
* **No event, notification, sequence, withdrawal or re-crossing.** All
  EVAL-EVENT lifecycle, all still blocked.
* **No workweek derivation.** The caller supplies the workweek start date,
  which is the persisted W4 ``workweek_key`` parsed. Deriving one here would
  read current configuration and undo W4.
* **No migration and no new table.** The threshold is a row in the existing
  immutable ``config_version``/``config_value`` mechanism.

THE TWO "TIER" AXES
-------------------
``config_value.tier`` carries ``ECO_THRESHOLD``, a configuration-domain label.
The ECO EVENT tier is ``PRIMARY``, is not stored by this package, and never
appears in that column. See ``keys`` for the full note.
"""

from .keys import (
    ECO_THRESHOLD_KEY,
    ECO_THRESHOLD_RANK,
    ECO_THRESHOLD_TIER,
    MAX_THRESHOLD_MINUTES,
    MIN_THRESHOLD_MINUTES,
)
from .outcomes import (
    ADMINISTRATOR_ACTIONABLE_OUTCOMES,
    NON_EVALUABLE_OUTCOMES,
    ThresholdOutcome,
    ThresholdResolution,
)
from .ports import ThresholdCandidate, ThresholdProvider
from .reader import (
    DEFECT_AMBIGUOUS_ROWS,
    DEFECT_TIER_NOT_CANONICAL,
    DEFECT_VALUE_ABSENT,
    DEFECT_VALUE_OUT_OF_RANGE,
    DEFECT_WORKER_SCOPED,
    resolve_threshold,
)
from .adapter import DatabaseThresholdProvider
from .reference import InMemoryThresholdProvider, StoredThresholdVersion

__all__ = [
    # Identity
    "ECO_THRESHOLD_KEY",
    "ECO_THRESHOLD_TIER",
    "ECO_THRESHOLD_RANK",
    "MIN_THRESHOLD_MINUTES",
    "MAX_THRESHOLD_MINUTES",
    # Result
    "ThresholdOutcome",
    "ThresholdResolution",
    "NON_EVALUABLE_OUTCOMES",
    "ADMINISTRATOR_ACTIONABLE_OUTCOMES",
    # Seam
    "ThresholdCandidate",
    "ThresholdProvider",
    # Reader
    "resolve_threshold",
    "DEFECT_TIER_NOT_CANONICAL",
    "DEFECT_AMBIGUOUS_ROWS",
    "DEFECT_VALUE_ABSENT",
    "DEFECT_VALUE_OUT_OF_RANGE",
    "DEFECT_WORKER_SCOPED",
    # Providers
    "DatabaseThresholdProvider",
    "InMemoryThresholdProvider",
    "StoredThresholdVersion",
]
