"""
Temveris – Catalog adoption collision detection
================================================
Deciding, for each catalog holiday, whether the organization ALREADY observes
it — before writing anything.

The bug this exists to fix
--------------------------
``org_calendar.find_duplicate`` answers a narrow question: "would the database
reject this INSERT?" It mirrors the unique index exactly, including an exact
match on ``country_code``:

    country_code = 'IN'   matches only rows whose country_code IS 'IN'
    country_code IS NULL  matches only rows whose country_code IS NULL

That is correct for the CRUD 409 pre-check and wrong for adoption, because
every holiday that predates ``m7n2p5q8r1t4`` has ``country_code IS NULL``.
Adopting India passes ``'IN'``, matches nothing, and re-imports the entire
calendar. Measured against organization 1's real 13-row calendar: the UI
offered "13 available to add", the backend inserted **20** and reported 20,
leaving 33 rows with **10 dates holding two holidays each** — 7 of them the
same name twice, differing only by NULL vs 'IN'.

The asymmetry is the root cause. The READ path already treats NULL as "applies
to every country" (``org_calendar.country_scope_filter``); the duplicate check
did not. This module uses the read path's rule, because that is the one that
decides whether an employee actually gets the day off.

The four verdicts
-----------------
``NEW``              nothing on that date, and nothing similarly named nearby
``EXACT_DUPLICATE``  same date, same name (case/punctuation-insensitive)
``ALREADY_COVERED``  same date, different name, but recognisably the same
                     observance — "Id ul Fitr" vs "Eid-ul-Fitr"
``DATE_CONFLICT``    the organization has something on that date that looks
                     like a DIFFERENT holiday, or the same observance on a
                     nearby but different date (the lunar-drift case)

Nothing here writes. It classifies, and the route decides what to do.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import models

STATUS_NEW = "new"
STATUS_EXACT_DUPLICATE = "exact_duplicate"
STATUS_ALREADY_COVERED = "already_covered"
STATUS_DATE_CONFLICT = "date_conflict"

#: Statuses that must never be written without an explicit admin decision.
NEEDS_DECISION = (STATUS_DATE_CONFLICT,)
#: Statuses that adoption skips by default.
SKIP_BY_DEFAULT = (STATUS_EXACT_DUPLICATE, STATUS_ALREADY_COVERED, STATUS_DATE_CONFLICT)

#: How far a lunar holiday may drift and still be recognised as the same one.
#: Eid dates move by a day or two depending on the sighting; three days is
#: generous enough to catch that without colliding with unrelated festivals.
NEARBY_DAYS = 3

#: Words that carry no identity. "Christmas Day" and "Christmas" are the same
#: holiday; "Republic Day" and "Independence Day" are not, which is why only
#: genuinely empty words belong here.
_STOPWORDS = frozenset({
    "day", "holiday", "holidays", "the", "of", "and", "st", "public",
    "observance", "festival", "eve", "celebration",
})

#: A SHARED token this long is strong evidence of the same observance —
#: "bakrid" in "Id ul Zuha (Bakrid)" and "Eid-ul-Adha (Bakrid)". Short shared
#: fragments like "eid" or "ul" are deliberately below the bar.
_SHARED_STRONG_MIN_LEN = 5

#: A token this long that appears on only ONE side is evidence of a DIFFERENT
#: observance. When both sides have one, the names disagree about something
#: that matters: "Eid-ul-Fitr" and "Eid-ul-Adha" share "eid"/"ul" but differ on
#: "fitr"/"adha", and they are different festivals. Without this rule their
#: token overlap alone (2 of 4) cleared the similarity threshold and one would
#: have been silently suppressed as "already covered".
_EXCLUSIVE_DISTINCTIVE_MIN_LEN = 4

_SIMILARITY_THRESHOLD = 0.5


def _tokens(name: str) -> set:
    """Identity-bearing tokens of a holiday name, accent- and punctuation-free."""
    folded = unicodedata.normalize("NFKD", str(name or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    parts = re.split(r"[^0-9a-zA-Z]+", folded.lower())
    return {p for p in parts if p and p not in _STOPWORDS}


def canonical_name(name: str) -> str:
    """A comparable form of a name — used for the EXACT_DUPLICATE test."""
    return " ".join(sorted(_tokens(name)))


def same_observance(name_a: str, name_b: str) -> bool:
    """Do these two names refer to the same holiday?

    Decided in this order, strongest signal first:

    1. identical once normalized               "Christmas Day" / "Christmas"     -> same
    2. one token set contains the other        "Mahatma Gandhi Jayanti" /
                                               "Gandhi Jayanti"                  -> same
    3. they share a long distinctive token     "…(Bakrid)" / "…(Bakrid)"         -> same
    4. EACH side has a long token the other
       lacks                                   "Eid-ul-Fitr" / "Eid-ul-Adha"     -> different
    5. otherwise, half their tokens overlap    "Id ul Fitr" / "Eid-ul-Fitr"      -> same

    Rule 4 exists because rule 5 alone got Eid wrong: the two Eids share
    "eid"+"ul" out of four tokens, which clears the 50% threshold, and merging
    them would have hidden a real festival behind "already covered".

    Errs toward "different": a false "same" silently suppresses a holiday,
    while a false "different" merely asks the admin to look.
    """
    a, b = _tokens(name_a), _tokens(name_b)
    if not a or not b:
        return False
    if a == b:
        return True
    if a <= b or b <= a:
        return True

    shared = a & b
    if any(len(t) >= _SHARED_STRONG_MIN_LEN for t in shared):
        return True

    only_a = {t for t in (a - b) if len(t) >= _EXCLUSIVE_DISTINCTIVE_MIN_LEN}
    only_b = {t for t in (b - a) if len(t) >= _EXCLUSIVE_DISTINCTIVE_MIN_LEN}
    if only_a and only_b:
        return False

    union = a | b
    return bool(union) and (len(shared) / len(union)) >= _SIMILARITY_THRESHOLD


def _applicable_rows(
    db: Session,
    org_id,
    country_code: Optional[str],
    start: date,
    end: date,
) -> List[Any]:
    """Existing holidays in a date window that APPLY to this country calendar.

    Uses the read path's rule — an unscoped (NULL) row applies to every
    country — which is what makes a legacy calendar visible to the classifier.
    """
    query = db.query(
        models.Holiday.id, models.Holiday.name, models.Holiday.date,
        models.Holiday.holiday_type, models.Holiday.country_code,
    ).filter(
        models.Holiday.organization_id == org_id,
        models.Holiday.date >= start,
        models.Holiday.date <= end,
    )
    target = (country_code or "").strip().upper() or None
    if target:
        query = query.filter(or_(
            models.Holiday.country_code.is_(None),
            models.Holiday.country_code == target,
        ))
    return query.order_by(models.Holiday.date, models.Holiday.name).all()


def _existing_ref(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "date": row.date.isoformat() if row.date else None,
        "holiday_type": row.holiday_type,
        "country_code": row.country_code,
    }


def classify_entries(
    db: Session,
    org_id,
    country_code: Optional[str],
    entries: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Classify every catalog entry against the organization's calendar.

    Returns the entries in order, each with ``status``, a human ``reason`` and
    ``existing`` (the row it collides with, when there is one).

    Scoping (PHASE 8): only holidays in the SAME country calendar are compared.
    A US holiday on 2026-07-04 cannot collide with an Indian one — unless the
    existing row is unscoped, in which case it applies to every calendar by
    definition and a collision is real. Years never cross: the comparison
    window is derived from the entry's own date.
    """
    parsed: List[Dict[str, Any]] = []
    for entry in entries:
        try:
            entry_date = date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            continue
        parsed.append({"entry": entry, "date": entry_date})

    if not parsed:
        return []

    window_start = min(p["date"] for p in parsed) - timedelta(days=NEARBY_DAYS)
    window_end = max(p["date"] for p in parsed) + timedelta(days=NEARBY_DAYS)
    rows = _applicable_rows(db, org_id, country_code, window_start, window_end)

    by_date: Dict[date, List[Any]] = {}
    for row in rows:
        by_date.setdefault(row.date, []).append(row)

    out: List[Dict[str, Any]] = []
    for item in parsed:
        entry, entry_date = item["entry"], item["date"]
        name = entry.get("name") or ""
        result = dict(entry)
        result["status"] = STATUS_NEW
        result["existing"] = None
        result["reason"] = ""

        same_day = by_date.get(entry_date, [])

        # 1. Exact duplicate — same date, same name once normalized.
        exact = next(
            (r for r in same_day if canonical_name(r.name) == canonical_name(name)), None)
        if exact is not None:
            result["status"] = STATUS_EXACT_DUPLICATE
            result["existing"] = _existing_ref(exact)
            result["reason"] = "already in your calendar"
            out.append(result)
            continue

        # 2. Same date, different name.
        if same_day:
            covered = next((r for r in same_day if same_observance(r.name, name)), None)
            if covered is not None:
                result["status"] = STATUS_ALREADY_COVERED
                result["existing"] = _existing_ref(covered)
                result["reason"] = f'already covered by "{covered.name}"'
            else:
                other = same_day[0]
                result["status"] = STATUS_DATE_CONFLICT
                result["existing"] = _existing_ref(other)
                result["reason"] = (
                    f'your calendar has "{other.name}" on this date — adding this '
                    "would put two holidays on one day"
                )
            out.append(result)
            continue

        # 3. Nothing on the date, but the same observance sits on a NEARBY date.
        #    This is the lunar-drift case: adding it would create a SECOND
        #    non-working day for one festival.
        nearby = None
        for offset in range(1, NEARBY_DAYS + 1):
            for candidate_date in (entry_date - timedelta(days=offset),
                                   entry_date + timedelta(days=offset)):
                for row in by_date.get(candidate_date, []):
                    if same_observance(row.name, name):
                        nearby = row
                        break
                if nearby:
                    break
            if nearby:
                break

        if nearby is not None:
            result["status"] = STATUS_DATE_CONFLICT
            result["existing"] = _existing_ref(nearby)
            delta = (entry_date - nearby.date).days
            result["reason"] = (
                f'your calendar has "{nearby.name}" on {nearby.date.isoformat()} '
                f"({abs(delta)} day{'s' if abs(delta) != 1 else ''} "
                f"{'later' if delta < 0 else 'earlier'}) — this looks like the same "
                "holiday on a different date, so adding it would create an extra "
                "non-working day"
            )
        out.append(result)

    return out


def summarize(classified: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Counts per status, plus a total. Drives the adoption header."""
    counts = {
        STATUS_NEW: 0,
        STATUS_ALREADY_COVERED: 0,
        STATUS_EXACT_DUPLICATE: 0,
        STATUS_DATE_CONFLICT: 0,
    }
    for item in classified:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    counts["total"] = len(classified)
    return counts


def entry_key(entry: Dict[str, Any]) -> str:
    """Stable identifier for one catalog entry, for per-row actions.

    Date plus canonical name: survives a re-fetch and does not depend on list
    position, so a per-row Add cannot act on the wrong row after a refresh.
    """
    return f"{entry.get('date')}|{canonical_name(entry.get('name') or '')}"
