"""
The canonical ECO threshold configuration identity.
===================================================

Owner-ruled literals. Nothing here is derived, defaulted or inferred.

WHY THESE LIVE HERE AND NOT IN ``config_versioning``
----------------------------------------------------
``config_value`` is a general key/value store shared by several configuration
domains. Its adapter owns the REQUIREMENT key and knows nothing about this one,
and a Step 6 test asserts that the package never learns ECO vocabulary. Keeping
these literals in an ECO-owned module is what makes the two domains separate at
module level rather than only inside a query.

THE TWO MEANINGS OF "TIER" — DO NOT CONFLATE THEM
-------------------------------------------------
There are two unrelated axes in this system that both use the word *tier*:

* ``config_value.tier`` — the §10.1 REQUIREMENT PRECEDENCE ladder:
  ``WORKER_OVERRIDE``, ``SHIFT``, ``SCHEDULE``, ``EMPLOYMENT_POLICY``,
  ``TENANT_TYPE_DEFAULT``, ``TENANT_FALLBACK``. A ladder: lowest rank wins.
* the ECO EVENT tier — ``PRIMARY``. Not stored here, not a member of anything
  below, and never written into ``config_value.tier``.

``ECO_THRESHOLD`` belongs to neither. It is a **configuration-domain label**:
it says "this row is the ECO threshold", not "this row outranks that one".
"""

from __future__ import annotations

#: The one configuration key that holds the ECO weekly threshold.
#:
#: Bare snake_case with a unit suffix, matching the only other key this store
#: holds (``required_minutes``) — deliberately NOT the dotted ``app_settings``
#: convention (``schedule.week_start_day``), which belongs to a different store
#: with a different cascade. ``eco_`` namespaces it so it can never collide.
#:
#: The name states the unit and nothing else: no scope (organization scope is
#: ``scope_user_id IS NULL``, not a name fragment), no tier, no value, and none
#: of the specification's notation indices.
ECO_THRESHOLD_KEY = "eco_weekly_attributable_shortfall_threshold_minutes"

#: The configuration-domain label carried by a threshold row's ``tier`` column.
#: See the module docstring: this is not a precedence tier and not an event tier.
ECO_THRESHOLD_TIER = "ECO_THRESHOLD"

#: The ``rank`` a canonical threshold row carries.
#:
#: STRUCTURAL ONLY. ``ConfigValue.rank`` is ``NOT NULL`` with
#: ``CHECK rank >= 0``, so every row must carry one — and a domain with exactly
#: one row per version has no precedence to express. It is emphatically NOT
#: requirement precedence, and it is NOT "highest priority".
#:
#: The reader cannot read it: ``ThresholdCandidate`` has no ``rank`` field, so
#: resolving ambiguity by rank is impossible rather than merely forbidden. This
#: constant exists to define the canonical row for whatever eventually writes
#: one, and for tests to assert against.
ECO_THRESHOLD_RANK = 0

#: Inclusive bounds, re-stated here rather than imported.
#:
#: They mirror ``ck_config_value_minutes_range`` and Step 5's
#: ``MAX_REQUIRED_MINUTES``, and are declared independently for the same reason
#: Step 5 declares its own: a database built by ``create_all`` and one built by
#: Alembic must not be trusted to agree, and a reader that only trusts a CHECK
#: constraint is not fail-closed against malformed storage.
#:
#: The lower bound of 1 means a zero threshold is not representable, which is
#: correct — a threshold of zero would be reached by every workweek.
MIN_THRESHOLD_MINUTES = 1
MAX_THRESHOLD_MINUTES = 1440


__all__ = [
    "ECO_THRESHOLD_KEY",
    "ECO_THRESHOLD_TIER",
    "ECO_THRESHOLD_RANK",
    "MIN_THRESHOLD_MINUTES",
    "MAX_THRESHOLD_MINUTES",
]
