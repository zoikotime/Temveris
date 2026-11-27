"""
The GV-17 visibility surface — WF-ECO-03 and WF-ECO-04.
=======================================================

    render_eco_day(view, audience=Audience.WORKER)  -> RenderedEcoDay

§24.7 requires the deferred state to be "visible to both Worker and
administrator so that a week which appears artificially low is explainable".
This is the layer that turns the canonical :class:`EcoDayView` into what a
surface shows: a locked §28.1 Status label, and — for DEFERRED alone — the
approved explanation.

IT RENDERS; IT DOES NOT DECIDE
-------------------------------
Every semantic question was already answered upstream and is consumed here as a
fact: EVAL-DAY decided the state (§19), the ledger evidence records WHY, and
``read_eco_day`` authorized the reader and resolved the historical version.
Nothing here re-derives any of that. There is no second calculation path, no
recomputation, no clock read, and no configuration lookup — the input is a
frozen value object and the output is text.

FOUR OF THE SIX LABELS ARE REACHABLE, AND THE REST IS NOT A COPY PROBLEM
-------------------------------------------------------------------------
All six §28.1 labels are registered and owner-approved. Four are reachable
from an evaluation state; the other two are not, and never will be from here.

**Not derivable from any evaluation state.** "Under review" and "Excluded" are
WORKFLOW outcomes, not synonyms for a computed state. §28.1 defines "Under
review" as what a raised dispute sets (§9.4), and §9.4's resolution emits
``EARLY_CLOCK_OUT_EXCLUDED`` — so "Excluded" follows an administrator
recording an exclusion (§29), not a calculation. Neither workflow exists in
this platform, and owner rulings keep both out of the renderer even once they
do: they will be SET by those paths, never inferred here. In particular a day
with ``gross > 0``, ``attributable == 0`` and ``exclusion > 0`` renders
"Counted", not "Excluded".

**No established mapping.** These two render nothing:

    REQUIREMENT_UNRESOLVED has no §28.1 label at all
    EVALUABLE              is never persisted

``UNSUPPORTED_STATUS_STATES`` records the reason per state as data rather than
prose, so adding a mapping means deleting an entry and saying what changed.

COPY IS QUOTED, NEVER COMPOSED
-------------------------------
Every string is read from the registers verbatim, resolved through a key.
No production wording is written in this module. Nothing here shortens,
re-wraps, capitalises, appends to or interpolates into approved wording.

    status   eco.status.*        §28.1 owner authorization
    body     eco.deferred.body   owner-authored production copy (DEFERRED only)

AUDIENCE MODEL A
----------------
The owner ruled model A: ONE body serves both audiences. The administrator
rendering differs in exactly one respect — §29.1 and §33.4 require
``eco.admin.disclaimer`` to appear "on this surface and in every administrator
notification … in the body, not in a footer", so it is carried IN the body and
the type has no footer field to put it in instead.

THE §31.1 GATE RUNS ON THE FINAL BODY
--------------------------------------
Every composed body is scanned before it is returned, so an ungated string
cannot reach a surface. ``eco.admin.disclaimer`` is the one specification-
mandated exemption — it carries the single §31.1 term §29.1 requires verbatim,
in a NEGATING construction that is the operative expression of INV-9 — and the
exemption is applied by removing that exact registered string before scanning,
never by disabling the scan. A body is not laundered by containing an exempt
string somewhere in it.

The terminology primitive comes from ``app.early_clock_out``, NOT from ALERT.
ALERT is the notification path; this is the explanation path, and §34 keeps
those separate. Borrowing ALERT's renderer would couple a read surface to a
delivery package for the sake of five lines.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Tuple

from app.early_clock_out import (
    LOCKED_COPY,
    OWNER_AUTHORIZED_COPY,
    SECTION_28_1_STATUS_COPY,
    SECTION_28_1_STATUSES_WITH_BODY,
    find_prohibited_terms,
)

from .outcomes import EcoDayView

#: The §31 string §29.1 and §33.4 mandate in every administrator body.
ADMIN_DISCLAIMER_KEY = "eco.admin.disclaimer"

# ═════════════════════════════════════════════════════════════════════════════
# EcoDayState -> §28.1 Status label
# ═════════════════════════════════════════════════════════════════════════════
# ONLY the mappings the specification actually establishes. Four of the six
# §28.1 labels are NOT reachable from the persisted state model, and this table
# is deliberately short rather than complete — see ``UNSUPPORTED_STATUS_STATES``
# for each one's reason. Populating the column by collapsing distinct §28.1
# states into whichever label is nearest would make the Status column lie, and
# it would do so invisibly.
_STATUS_KEY_FOR_STATE = {
    # §24.7. The day is "neither counted nor excused" and its own §28.1 value
    # exists. Established by GV-17 and implemented in Step 2.
    "DEFERRED_PENDING_LEDGER": "eco.status.deferred",
    # §24.2. The ECO state and the §28.1 label are the same concept under the
    # same words; no interpretation sits between them.
    "NOT_A_WORKING_DAY": "eco.status.notaworkingday",
    # Owner Ruling 3. NO_SHORTFALL is the state for a day whose attributable
    # shortfall is zero, and "Complete" is §28.1's label for a day with no
    # shortfall. The owner ruled explicitly that "Complete" is NOT to be
    # distinguished from "Excluded" here: "Excluded" is a workflow outcome
    # (Ruling 2), not a synonym for an evaluation state, so no exclusion
    # reasoning enters this mapping.
    "NO_SHORTFALL": "eco.status.complete",
    # Owner ruling on SHORTFALL. §28's own day table renders a day with a
    # positive gross shortfall under this label — "Mon … Gross 8m, Excluded 0m,
    # Counted 8m, Status Counted" — so the mapping is the specification's own
    # illustration rather than an inference.
    #
    # "Counted" describes the CALCULATION, not the Worker: the day was counted
    # in the ECO arithmetic. It carries no fault, and §31.1's vocabulary stays
    # out of this path entirely.
    #
    # A day with `gross > 0`, `attributable == 0` and `exclusion > 0` still
    # maps here. The owner ruled explicitly that such a day is NOT "Excluded" —
    # that label belongs to the exclusion-recording workflow (§9.4 / §29), not
    # to an evaluation state — so no exclusion reasoning enters this mapping,
    # and the renderer reads no exclusion field to decide it.
    "SHORTFALL": "eco.status.counted",
}

#: Why each remaining state has no label. Kept as data, not prose, so a future
#: implementer must delete an entry to add a mapping — and has to say what
#: changed to make it derivable.
UNSUPPORTED_STATUS_STATES = {
    # §24.3 shows the Worker nothing for an unresolved requirement, and §28.1's
    # six values contain no label for it.
    "REQUIREMENT_UNRESOLVED": "no §28.1 label is defined for this state",
    # S1 readiness only; `DayEvaluation` raises rather than persisting it, so
    # it cannot reach a view. Listed so the refusal is total.
    "EVALUABLE": "a readiness state that is never persisted",
    # No applicable PolicyVersion for the date. §28.1's six values contain no
    # label for it, and the owner ruling that introduced the state declined to
    # map it onto any of them — "Deferred" in particular would be wrong, since
    # §24.7's deferral is a statement about EVIDENCE and this is a statement
    # about CONFIGURATION. Entry present so the refusal is deliberate and the
    # renderer fails closed until copy is separately authorized.
    "POLICY_UNRESOLVED": "no §28.1 label is authorized for this state",
}

#: Sentinel: distinguishes "reason not in the table" from a table entry whose
#: value is legitimately something falsy.
_MISSING = object()

#: WHICH deferred body a day gets, by its deferral reason (owner ruling D-2).
#:
#: The two bodies are NOT interchangeable and must never be substituted for one
#: another. NULL is the departure/exit deferral this state has always carried,
#: so it keeps the body authored for GV-17's §24.7 case; the classification
#: deferral gets the body authored for it, which differs in exactly the clause
#: that would otherwise be false. An unknown reason selects NEITHER — see
#: ``render_eco_day``, which fails closed rather than guessing.
_DEFERRED_BODY_KEY_FOR_REASON = {
    None: "eco.deferred.body",
    "CLASSIFICATION_UNRESOLVED": "eco.classification_deferred.body",
}

#: The one exemption from the §31.1 scan, and only within an administrator
#: body. Quoted from the register so the exemption can only ever cover the
#: exact mandated string.
_TERMINOLOGY_EXEMPT_KEYS: Tuple[str, ...] = (ADMIN_DISCLAIMER_KEY,)


class Audience(str, enum.Enum):
    """WHO the rendering is for. Not a permission — authority is resolved by
    ``require_eco_view_authority`` before a view ever reaches this module."""

    WORKER = "WORKER"
    """WF-ECO-03 — the Worker's own weekly detail."""

    ADMINISTRATOR = "ADMINISTRATOR"
    """WF-ECO-04 — an authorized tenant administrator's review screen."""


class ProhibitedTerminology(Exception):
    """A composed body carried §31.1 vocabulary.

    Raised rather than returned: the body has already been composed from
    registered strings, so this is a defect in composition, not an outcome a
    caller should branch on. Nothing is returned.
    """


@dataclass(frozen=True)
class RenderedEcoDay:
    """What a surface displays for one day. Text and nothing else.

    Carries no minutes, no evidence and no authorization detail — those stay on
    :class:`EcoDayView`, where a caller that needs them can read them with the
    provenance intact. Splitting them keeps a rendering from quietly becoming a
    second source of truth about the day.
    """

    audience: Audience
    #: The locked §28.1 Status label, or None for a state with no approved
    #: label yet. NEVER a fallback string: an unlabelled state renders no
    #: label rather than an invented one.
    status: Optional[str]
    #: The approved explanation, or None where none is approved.
    body: Optional[str]
    #: Which registered keys produced this rendering, in order. Recorded so a
    #: reviewer can confirm the quoting claim without re-reading this module.
    copy_keys: Tuple[str, ...]

    @property
    def is_renderable(self) -> bool:
        """True when this day has an approved §28.1 Status label.

        The LABEL is the test, not the body. Five of the six labels are
        label-only by owner ruling, so requiring a body would report a
        perfectly renderable "Not a working day" as unrenderable.
        """
        return self.status is not None

    @property
    def has_explanation(self) -> bool:
        """True when an approved explanatory body accompanies the label.

        Only DEFERRED has one (§24.7). Separate from :attr:`is_renderable` so a
        surface can ask the two questions independently rather than inferring
        one from the other.
        """
        return self.body is not None


def _assert_clean(body: str, *, exempt: Tuple[str, ...] = ()) -> None:
    """Refuse a body carrying §31.1 vocabulary.

    ``exempt`` holds EXACT registered strings the specification itself mandates
    verbatim. Each is removed before scanning, so an exemption covers only that
    string and never the surrounding text.
    """
    scanned = body
    for phrase in exempt:
        if phrase not in LOCKED_COPY.values():
            raise ValueError(
                "only an exact registered locked string may be exempted")
        scanned = scanned.replace(phrase, " ")
    hits = find_prohibited_terms(scanned)
    if hits:
        raise ProhibitedTerminology(
            f"rendered ECO day body contains §31.1 vocabulary: "
            f"{sorted(set(hits))}")


def render_eco_day(
    view: EcoDayView,
    *,
    audience: Audience = Audience.WORKER,
) -> RenderedEcoDay:
    """Render one already-authorized, already-evaluated day for one audience.

    ``view`` comes from ``read_eco_day``, which authorized the reader and
    resolved the historical ledger version. This function performs no
    authorization of its own and reads no database.

    FAILS CLOSED. A state with no established §28.1 mapping renders
    ``status=None, body=None`` rather than a placeholder or a nearest-fit
    label. All six labels are registered and approved — the gap is not copy,
    it is that four of them cannot be DERIVED from the persisted state model
    (``UNSUPPORTED_STATUS_STATES`` records why for each). Returning None is the
    honest answer; collapsing distinct §28.1 states to populate the column is
    the outcome this whole gate existed to prevent.
    """
    status_key = _STATUS_KEY_FOR_STATE.get(view.evaluation_state)
    if status_key is None:
        return RenderedEcoDay(
            audience=audience, status=None, body=None, copy_keys=())

    keys = [status_key]
    status = SECTION_28_1_STATUS_COPY[status_key]
    parts: list = []

    # Only DEFERRED carries an explanatory body (§24.7). The other labels were
    # ruled label-only, so no body is composed and none is invented.
    exempt: Tuple[str, ...] = ()
    if status_key in SECTION_28_1_STATUSES_WITH_BODY:
        body_key = _DEFERRED_BODY_KEY_FOR_REASON.get(
            view.evaluation_deferral_reason, _MISSING)
        if body_key is _MISSING:
            # An unknown deferral reason. FAILS CLOSED rather than falling back
            # to the departure body: D-2 is explicit that the two bodies may
            # never be substituted for one another, and a reason nobody governs
            # must not select production copy.
            return RenderedEcoDay(
                audience=audience, status=None, body=None, copy_keys=())
        parts.append(OWNER_AUTHORIZED_COPY[body_key])
        keys.append(body_key)

        if audience is Audience.ADMINISTRATOR:
            # §29.1 / §33.4: mandatory, in the body, not a footer.
            parts.append(LOCKED_COPY[ADMIN_DISCLAIMER_KEY])
            keys.append(ADMIN_DISCLAIMER_KEY)
            exempt = tuple(LOCKED_COPY[k] for k in _TERMINOLOGY_EXEMPT_KEYS)

    body = "\n\n".join(parts) if parts else None
    if body is not None:
        _assert_clean(body, exempt=exempt)

    return RenderedEcoDay(
        audience=audience,
        status=status,
        body=body,
        copy_keys=tuple(keys),
    )


__all__ = [
    "Audience",
    "RenderedEcoDay",
    "ProhibitedTerminology",
    "render_eco_day",
    "ADMIN_DISCLAIMER_KEY",
    "UNSUPPORTED_STATUS_STATES",
]
