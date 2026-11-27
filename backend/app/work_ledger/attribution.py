"""
The shift-attribution rule — one constant, one meaning.
=======================================================

R-12.3.2 makes the attribution rule part of how a shift becomes a
``work_date``; R-12.3.4 and §35 require the rule in force to be PERSISTED on
the day record, in its provenance block, beside ``timezone_iana`` and
``utc_offset_seconds``. The spec's reason is the one it gives for every field
in §35: "a specific question must be answerable years later without access to
the systems that produced it".

The question here is *"under which rule was this day's date chosen?"*, and a
row that does not carry the answer cannot be replayed — the rule would have to
be assumed from whatever the code says today, which is exactly the class of
drift §17 forbids.

WHY THERE IS NO SETTING
-----------------------
R-12.3.2 names three rules — ``START_DATE`` (default), ``END_DATE`` and
``MAJORITY_HOURS`` — and makes the choice Tenant-level. The owner ruling for
this increment supports exactly ONE of them and creates no tenant
configuration: ``START_DATE``, the spec's own default.

So the rule is persisted (R-12.3.4, §35) without being configurable. Those are
separate properties, and conflating them is what would have produced either a
missing provenance field or an unused configuration framework. A later
increment that adds a second rule changes where the value COMES FROM; it does
not change the fact that each sealed day already carries its own.

NO IMPORTS, DELIBERATELY
------------------------
An Alembic migration must emit the same literal this module defines, and a
migration may not import application code — the model it targets can be
several revisions ahead of the schema the migration runs against. A module
with no imports can be read by both without that hazard, which is the idiom
this repository already uses for its append-only DDL constants.
"""

from __future__ import annotations

#: The only attribution rule this platform produces. §42/GV-09 names it as the
#: rule in force for the cross-week vector, and §12.3 makes it the default.
SHIFT_ATTRIBUTION_START_DATE = "START_DATE"

#: Named but NOT implemented. Present so the vocabulary is visible in one
#: place and a future increment does not re-invent the spelling; a value
#: outside SUPPORTED_ATTRIBUTION_RULES must never be written.
SHIFT_ATTRIBUTION_END_DATE = "END_DATE"
SHIFT_ATTRIBUTION_MAJORITY_HOURS = "MAJORITY_HOURS"

#: What the producer may actually stamp on a row today. The guard that uses it
#: fails closed: an unsupported rule is refused, never coerced to the default.
SUPPORTED_ATTRIBUTION_RULES = frozenset({SHIFT_ATTRIBUTION_START_DATE})

#: Every rule §12.3 defines, supported or not — for validation messages and
#: for the check constraint, so an END_DATE row is refused as *unsupported*
#: rather than as *unrecognised*.
SPECIFIED_ATTRIBUTION_RULES = (
    SHIFT_ATTRIBUTION_START_DATE,
    SHIFT_ATTRIBUTION_END_DATE,
    SHIFT_ATTRIBUTION_MAJORITY_HOURS,
)

__all__ = [
    "SHIFT_ATTRIBUTION_START_DATE",
    "SHIFT_ATTRIBUTION_END_DATE",
    "SHIFT_ATTRIBUTION_MAJORITY_HOURS",
    "SUPPORTED_ATTRIBUTION_RULES",
    "SPECIFIED_ATTRIBUTION_RULES",
]
