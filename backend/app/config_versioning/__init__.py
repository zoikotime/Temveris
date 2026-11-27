"""
Temveris — Immutable, effective-dated configuration (Step 6, non-schema part)
=============================================================================

STATUS: IMPLEMENTED, NOT WIRED. No route, service or scheduler uses it.

Step 6 delivers four things:

* the **semantics** of configuration versioning, written as executable code;
* a **reference provider** (in-memory) satisfying the Step 5
  ``ConfigVersionProvider`` port — the executable specification;
* the **tables** ``config_version`` / ``config_value``, additive, created by one
  Alembic revision under owner authorisation; and
* the **database-backed adapter**, held to the reference provider's exact
  semantics by one shared contract suite rather than by tests of its own.

THE REFERENCE PROVIDER IS NOT SCAFFOLDING
-----------------------------------------
The hard part of Step 6 was never the two tables — it is deciding exactly what
"the configuration in force on that date" means at the edges: the day a version
starts, a date below the historical floor, two versions claiming one date, a
correction arriving after the fact. Those decisions were settled in the
reference provider first, and the database adapter is now required to reproduce
them under the same tests. The reference implementation stays as the
specification and as a test double for steps that should not need a database.

WHAT THIS PACKAGE MUST NEVER GAIN
---------------------------------
* A read of live ``AppSettings`` for a historical date. That is the Gate 7
  floor, and it is the whole reason this layer exists rather than a date
  parameter bolted onto ``settings.scope.resolve_effective``.
* A default requirement, or any value that could become one.
* Any worked-time calculation (that is the ledger's job, Steps 1-2).
* Any Activity, screenshot or application-name dependency (INV-6, INV-7).
"""

from .errors import (
    ConfigVersionError,
    EffectiveDateCollision,
    ImmutableVersionError,
)
from .models import (
    ConfigValue,
    ConfigVersion,
    ImmutableConfigurationError,
    compute_content_hash,
)
from .adapter import DatabaseConfigVersionProvider
from .reference import InMemoryConfigVersionProvider

__all__ = [
    "InMemoryConfigVersionProvider",
    "DatabaseConfigVersionProvider",
    "ConfigVersion",
    "ConfigValue",
    "compute_content_hash",
    "ConfigVersionError",
    "EffectiveDateCollision",
    "ImmutableVersionError",
    "ImmutableConfigurationError",
]
