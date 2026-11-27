"""
Implementation copy for the §24.3 administrator alert — NOT §31-locked.
=======================================================================

WHAT THIS IS, AND WHAT IT IS EMPHATICALLY NOT
----------------------------------------------
This is **owner-authorized implementation copy under Ruling 3 = B**. It is NOT
a §31 locked string, it is NOT registered in
``app.early_clock_out.copy_register``, and it MUST NOT be treated as one or
moved into one without a fresh owner ruling.

The distinction is the whole reason this module exists rather than a new entry
in the locked register. §31 strings are *"legally load-bearing… MUST NOT be
paraphrased, shortened for layout, or rewritten by a content pass without Legal
review"*, and the register is the byte-for-byte comparison target for AC-20.
Putting unregistered text there would make this copy indistinguishable from
copy that survived that review. It has not.

WHY IT IS NOT LOCKED
--------------------
A final authoritative-source audit established that ZT-SPEC-ECO-002 v2.0
defines §24.3's behaviour and §33.1's audience for this alert but registers no
body for it. The specification contains exactly two notification identifiers —
``NOTIF-W-01`` (§33.2, Worker · threshold reached) and ``NOTIF-A-01`` (§33.3,
Administrator · threshold reached) — and neither is this alert. §31's register
lists twelve ``eco.*`` keys and none covers this state.

The owner therefore ruled that §31's locked-copy gate does not apply here, and
authorized implementation copy instead. Neither locked notification was
borrowed, and no locked string was invented.

CONSTRAINTS THIS COPY IS HELD TO
--------------------------------
Ruling 3 = B lifted the §31 comparison gate. It lifted nothing else:

* **§31.1 prohibited terminology still applies.** The body is scanned by
  ``app.early_clock_out.terminology`` in a test, against all sixteen entries.
* **§4.1's neutral vocabulary is used**: "Tenant", not organization or company;
  "Worker", not employee or user.
* **No employment judgment.** §24.3 is explicit that an unresolved requirement
  is *"a Tenant configuration defect"* and that surfacing it to the Worker as
  an attendance condition *"would be a false statement about them"*. The body
  therefore describes the configuration, never the person — it says what could
  not be resolved and what an administrator should look at, and attributes
  nothing to anybody.
* **No figures.** Nothing was computed for this day: no shortfall, no
  requirement, no total. The body states that, rather than rendering a zero
  that would read as a measurement.
"""

from __future__ import annotations

from typing import Dict

#: Marks every string here as implementation copy, so a reader who finds it
#: quoted somewhere can tell at a glance which regime it belongs to.
COPY_REGIME = "IMPLEMENTATION_COPY_RULING_3B"

#: Not a §31 key. A module-local identifier for the one body below.
REQUIREMENT_UNRESOLVED_BODY_ID = "eco_day_alert.requirement_unresolved.body"

#: The surface this body is written for. WF-ECO-04 is the administrator
#: surface the locked register already names for administrator notifications;
#: recorded as data so the mapping is inspectable, NOT as an
#: ``OWNER_AUTHORIZED_SURFACES`` entry — that structure belongs to the locked
#: register and this copy is not registered there.
REQUIREMENT_UNRESOLVED_SURFACE = "administrator operational alert"

#: The body. See the module docstring for the regime it belongs to.
REQUIREMENT_UNRESOLVED_BODY = (
    "The working-time requirement for this day could not be resolved, so no "
    "shortfall was calculated and nothing was shown to the Worker. Review "
    "this Tenant's working-time configuration for the affected date."
)

#: Every implementation string this package renders, by id. One entry today.
IMPLEMENTATION_COPY: Dict[str, str] = {
    REQUIREMENT_UNRESOLVED_BODY_ID: REQUIREMENT_UNRESOLVED_BODY,
}


def render_requirement_unresolved_body() -> str:
    """The body, frozen at alert creation and never re-rendered afterwards.

    Deliberately takes NO arguments. The alert carries no figures — nothing was
    computed for this day — and no Worker identifier, so there is nothing to
    interpolate and no way for a caller to inject content into it.
    """
    return REQUIREMENT_UNRESOLVED_BODY


__all__ = [
    "COPY_REGIME",
    "IMPLEMENTATION_COPY",
    "REQUIREMENT_UNRESOLVED_BODY",
    "REQUIREMENT_UNRESOLVED_BODY_ID",
    "REQUIREMENT_UNRESOLVED_SURFACE",
    "render_requirement_unresolved_body",
]
