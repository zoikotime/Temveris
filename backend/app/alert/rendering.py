"""
Rendering the immutable notification body, and the §31.1 gate it must pass.
===========================================================================

RENDERING HAPPENS AT RECORD CREATION, BEFORE ANY DELIVERY DECISION
------------------------------------------------------------------
Three things depend on that ordering:

* **GV-22 without MODEL-HORIZON.** The vector asserts that the notification
  names the workweek and the cause. Those are properties of the RENDERED
  RECORD. Were rendering deferred into a delivery path, it would sit behind a
  notification-eligibility decision that D3 makes the horizon's — and the
  horizon does not exist. Rendering here keeps GV-22 reachable without
  inventing one.
* **Deterministic historical reconstruction.** The body is frozen with the
  record, so a replay reproduces what was actually written rather than what
  today's copy register would produce.
* **R7.** The terminology gate runs on the FINAL body. A body that fails it is
  never recorded, so an ungated string cannot become deliverable later.

LOCKED COPY IS QUOTED, NEVER PARAPHRASED
-----------------------------------------
Every §31 string is read from ``app.early_clock_out.LOCKED_COPY`` — the single
transcription — and inserted verbatim. Nothing here shortens, rewrites or
"improves" one, and the keys used are recorded on the row so a reviewer can
check that claim without re-reading this module.

AN AUTHORITY GAP, RECORDED RATHER THAN FILLED
----------------------------------------------
The §31 register contains NO locked string for a threshold WITHDRAWAL. §33 is
not present in the repository, so there is no authority to quote for one.
Inventing prose for an ECO surface is exactly what §31/§31.1 exist to prevent,
so the withdrawal body carries FACTS ONLY — the workweek, the totals, the
lifecycle position — and no prose claim. GV-07 asserts recipients and event
retention, not copy, so this satisfies it without fabricating a sentence. If
§33 turns out to mandate withdrawal wording, this is the place it belongs.
"""

from __future__ import annotations

from typing import List, Mapping, Sequence, Tuple

from app.early_clock_out import (
    LOCKED_COPY,
    LOCKED_COPY_TERMINOLOGY_EXEMPT,
    find_prohibited_terms,
)

from .outcomes import NotificationAudience, NotificationType


class ProhibitedTerminology(Exception):
    """A rendered body contained §31.1 vocabulary.

    Raised rather than returned: a body that reaches this has already been
    composed, so the failure is a defect in the composition, not an ordinary
    outcome a caller should branch on. Nothing is recorded.
    """


#: §31 keys authorized for a NOTIFICATION surface, in render order.
#:
#: Only ``eco.status.threshold``. ``LOCKED_COPY_SURFACES`` — the §31 "WHERE"
#: column, recorded precisely so an implementer places each string on the
#: surface the specification assigns it to rather than guessing — marks it
#: "All surfaces", while every ``eco.advisory.*`` string is assigned to
#: **WF-ECO-02**, the clock-out advisory screen. Quoting a WF-ECO-02 string in
#: a notification would place it on a surface the specification did not give
#: it, which is the guess that column exists to prevent.
#:
#: ``eco.advisory.threshold`` is excluded for a second, independent reason: it
#: says the Worker "and your organisation's designated reviewer would both be
#: notified", and owner ruling R2 creates no reviewer notification. Quoting it
#: here would tell the Worker something this increment does not do.
WORKER_REACHED_COPY_KEYS: Tuple[str, ...] = (
    "eco.status.threshold",
)

#: No §31 string describes a withdrawal — see the module docstring.
WORKER_WITHDRAWN_COPY_KEYS: Tuple[str, ...] = ()


def assert_clean(body: str, *, exempt: Sequence[str] = ()) -> None:
    """Refuse a body carrying §31.1 vocabulary (R7).

    ``exempt`` holds EXACT registered strings that the specification itself
    mandates verbatim. Each is removed from the text before scanning, so the
    exemption covers only that string and never the surrounding body — a body
    is not laundered by containing an exempt string somewhere in it.

    Today no caller passes an exemption: the only exempt string is
    ``eco.admin.disclaimer``, and no administrator body exists yet. The
    parameter is the mechanism that will carry it when one does, and a test
    pins that it cannot be abused.
    """
    scanned = body
    for phrase in exempt:
        if phrase not in LOCKED_COPY.values():
            raise ValueError(
                f"only an exact registered locked string may be exempted; "
                f"{phrase[:40]!r} is not in the §31 register")
        scanned = scanned.replace(phrase, " ")

    hits = find_prohibited_terms(scanned)
    if hits:
        raise ProhibitedTerminology(
            f"rendered ECO notification body contains §31.1 vocabulary: "
            f"{sorted(set(hits))}"
        )


def _fact_lines(*, workweek_key: str, weekly_total_minutes: int,
                threshold_minutes: int, is_retroactive: bool,
                caused_by: Mapping[str, object]) -> List[str]:
    """The factual context every body carries.

    Data, not copy: identifiers and integers the event already established.
    GV-22 requires the notification to name the WORKWEEK and the CAUSE, and
    both come from the immutable event — the workweek from its identity, the
    cause from the retroactive lineage EVAL-EVENT froze into ``caused_by``.
    No clock is read and no horizon is consulted.
    """
    lines = [
        f"Workweek: {workweek_key}",
        f"Weekly total: {weekly_total_minutes} minutes",
        f"Review threshold: {threshold_minutes} minutes",
    ]
    if is_retroactive:
        corrected = list(caused_by.get("corrected_day_record_ids") or [])
        generations = list(caused_by.get("re_evaluated_generations") or [])
        # Names the CAUSE without asserting causation — the flag is a lineage
        # indicator, not proof that the correction produced the crossing.
        lines.append(
            "Cause: this workweek was re-evaluated after its recorded evidence "
            "was corrected."
        )
        if corrected:
            lines.append(f"Corrected day records: {corrected}")
        if generations:
            lines.append(f"Re-evaluated generations: {generations}")
    return lines


def render_body(
    *,
    audience: NotificationAudience,
    notification_type: NotificationType,
    workweek_key: str,
    weekly_total_minutes: int,
    threshold_minutes: int,
    is_retroactive: bool,
    caused_by: Mapping[str, object],
) -> Tuple[str, Tuple[str, ...]]:
    """Compose one audience's final body. Returns ``(body, copy_keys_used)``.

    The body is gated before it is returned, so an ungated string can never
    reach the record.
    """
    if audience is not NotificationAudience.WORKER:
        # Unreachable through the service, which refuses the audience earlier.
        # Stated here too so a future caller cannot obtain an administrator
        # body by calling the renderer directly before the recipient contract
        # that R2 defers actually exists.
        raise ValueError(
            f"no rendering contract exists for audience {audience!r}; only "
            f"WORKER is authorized (owner ruling R2)")

    if notification_type is NotificationType.THRESHOLD_REACHED:
        keys = WORKER_REACHED_COPY_KEYS
    elif notification_type is NotificationType.THRESHOLD_WITHDRAWN:
        keys = WORKER_WITHDRAWN_COPY_KEYS
    else:  # pragma: no cover - the enum has no other member
        raise ValueError(f"unknown notification type {notification_type!r}")

    parts: List[str] = [LOCKED_COPY[k] for k in keys]
    parts.extend(_fact_lines(
        workweek_key=workweek_key,
        weekly_total_minutes=weekly_total_minutes,
        threshold_minutes=threshold_minutes,
        is_retroactive=is_retroactive,
        caused_by=caused_by,
    ))
    if notification_type is NotificationType.THRESHOLD_WITHDRAWN:
        # Facts only: the §31 register has no withdrawal string to quote.
        parts.append("Status: the recorded threshold condition no longer "
                     "stands for this workweek.")

    body = "\n\n".join(parts)

    # WORKER bodies quote no exempt string, so the exemption list is empty:
    # the whole body is scanned.
    assert_clean(body, exempt=())
    return body, tuple(keys)


__all__ = [
    "render_body",
    "assert_clean",
    "ProhibitedTerminology",
    "WORKER_REACHED_COPY_KEYS",
    "WORKER_WITHDRAWN_COPY_KEYS",
    "LOCKED_COPY_TERMINOLOGY_EXEMPT",
]
