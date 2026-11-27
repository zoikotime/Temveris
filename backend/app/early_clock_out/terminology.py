"""
ZT-SPEC-ECO-002 §31.1 — Prohibited terminology, and a pure scanner for it.
==========================================================================

§31.1 verbatim:

    "The following MUST NOT appear in any Early Clock-Out surface, notification,
    export, API field name, log message, analytics label, support macro or
    commercial collateral: violation, breach, infraction, offence, misconduct,
    unauthorised, non-compliant, failure, offender, owed, debt, deficit,
    penalty, warning (as a noun describing the event), strike, flagged worker.
    Where a neutral word is needed, use the vocabulary at §4.1."

§32 extends the same rule to event names, field names and enum values:
``ECO_SHORTFALL_RECORDED`` is acceptable; ``ECO_VIOLATION_LOGGED`` is not, "and
cannot be fixed later without a breaking change".

WHY THE SCOPE IS NARROW
-----------------------
These are ordinary English words. "failure", "warning" and "breach" appear
legitimately throughout an attendance/billing/security codebase — a repository-
wide scan would produce hundreds of false positives and would be switched off
within a week, which is worse than no check at all.

So this module provides a PURE FUNCTION over text. Deciding *what* to scan is the
caller's job. ``tests/test_eco_terminology_guard.py`` applies it to the files
Early Clock-Out actually controls. That is regression prevention: it stops the
vocabulary entering ECO-controlled copy in the first place, which is the only
point at which §31.1 is cheap to enforce (§32: "cannot be fixed later without a
breaking change").

MATCHING RULES
--------------
* Case-insensitive, whole-word (so "strikethrough" is not a "strike", and
  "authorised" is not "unauthorised").
* The boundary is LETTER-based, not the regex ``\b`` word boundary, because
  ``\b`` treats ``_`` as a word character — which would make
  ``ECO_VIOLATION_LOGGED`` invisible to the scanner. §32 names that exact
  identifier as the thing to catch ("cannot be fixed later without a breaking
  change"), so snake_case and SCREAMING_SNAKE_CASE identifiers must split on
  underscores. Digits are also treated as separators, so ``violation2`` matches.
* Hyphen/space/underscore tolerant for the two multi-word entries
  ("non-compliant", "flagged worker"), so "non compliant", "non-compliant" and
  the identifier ``is_non_compliant`` all match.
* KNOWN LIMITATION: camelCase is not split, so ``isNonCompliant`` is not
  detected. Splitting on case would flag ordinary identifiers throughout the
  codebase; the ECO naming convention is snake_case, and §43 AC-06 pairs this
  check with human review rather than replacing it.
* "warning" is listed by the specification as prohibited *as a noun describing
  the event*. This scanner cannot do part-of-speech analysis, so it flags every
  occurrence and leaves the judgement to review. Over-flagging inside
  ECO-controlled copy is the safe direction.

This module performs no I/O and knows nothing about the domain.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

# ── §31.1, transcribed exactly and in the specification's own order ──────────
# The single-word entries, matched on LETTER boundaries (see MATCHING RULES).
# The two multi-word entries live in PROHIBITED_PHRASES below; together the two
# tuples are the complete list of sixteen. ``PROHIBITED_ENTRY_COUNT`` pins that,
# so an entry cannot be dropped silently.
PROHIBITED_TERMS: Tuple[str, ...] = (
    "violation",
    "breach",
    "infraction",
    "offence",
    "misconduct",
    "unauthorised",
    "failure",
    "offender",
    "owed",
    "debt",
    "deficit",
    "penalty",
    "warning",
    "strike",
)

# Multi-word entries from the same list. Matched insensitively to whether the
# parts are separated by a space, a hyphen or an underscore.
PROHIBITED_PHRASES: Tuple[str, ...] = (
    "non-compliant",
    "flagged worker",
)

# §31.1 lists sixteen entries. Pinned so that deleting one — the only way this
# guard can silently weaken — fails a test rather than passing quietly.
PROHIBITED_ENTRY_COUNT: int = 16

# §4.1 — the locked vocabulary, transcribed in full so a reviewer reaching for a
# neutral word has the whole table rather than an abridged version of it. Each
# entry is (term to USE -> the full NEVER USE list for that row). Not consulted
# by the scanner; present because §31.1 ends by pointing at §4.1.
NEUTRAL_VOCABULARY: Dict[str, Tuple[str, ...]] = {
    "Tenant": ("Organization", "Org", "Company", "Account", "Customer"),
    "Worker": ("Employee", "User", "Staff member", "Headcount", "Resource"),
    "Workforce Truth Infrastructure": (
        "Workforce Assurance", "Workforce Assurance Intelligence",
        "Workforce Financial Control Infrastructure",
    ),
    "active worker": ("user", "seat", "licence", "employee"),
    "Attributable shortfall": (
        "violation", "infraction", "breach", "offence", "deficit", "debt",
        "owed time",
    ),
    "Threshold reached": (
        "threshold breached", "threshold violated", "over limit",
        "non-compliant",
    ),
    "Under review": (
        "flagged", "escalated", "disputed by worker", "contested",
    ),
    "BRK_M / BRK_R": (
        "break", "lunch", "rest", "unpaid break", "paid break",
    ),
}


# Letter-based boundaries. See "MATCHING RULES" above: ``\b`` would let
# ``ECO_VIOLATION_LOGGED`` through, which is the one case §32 calls out by name.
_LEFT = r"(?<![A-Za-z])"
_RIGHT = r"(?![A-Za-z])"


def _word_pattern(term: str) -> re.Pattern:
    """Whole-word matcher for a single prohibited term."""
    return re.compile(_LEFT + re.escape(term) + _RIGHT, re.IGNORECASE)


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Whole-phrase matcher tolerating a hyphen, whitespace or an underscore
    between the parts, so "non-compliant", "non compliant" and the identifier
    ``is_non_compliant`` all match."""
    parts = [re.escape(p) for p in re.split(r"[\s_-]+", phrase) if p]
    return re.compile(_LEFT + r"[\s_-]+".join(parts) + _RIGHT, re.IGNORECASE)


_TERM_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = tuple(
    (t, _word_pattern(t)) for t in PROHIBITED_TERMS
)
_PHRASE_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = tuple(
    (p, _phrase_pattern(p)) for p in PROHIBITED_PHRASES
)


def find_prohibited_terms(text: str) -> List[str]:
    """Every §31.1 term present in ``text``, deduplicated, in specification order.

    Pure. Returns the canonical specification spelling of each term found, not
    the casing used in the text, so callers get a stable, comparable result.
    """
    if not text:
        return []
    found: List[str] = []
    for term, pattern in _TERM_PATTERNS:
        if pattern.search(text):
            found.append(term)
    for phrase, pattern in _PHRASE_PATTERNS:
        if pattern.search(text):
            found.append(phrase)
    return found


def scan_text(text: str) -> List[Tuple[int, str, str]]:
    """Line-level detail for a reviewer: ``(line_number, term, line_text)``.

    ``find_prohibited_terms`` answers "is this clean?"; this answers "where?".
    Line numbers are 1-based to match an editor.
    """
    if not text:
        return []
    hits: List[Tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for term in find_prohibited_terms(line):
            hits.append((lineno, term, line.strip()))
    return hits


def is_clean(text: str) -> bool:
    """True when ``text`` contains no §31.1 terminology."""
    return not find_prohibited_terms(text)


def format_hits(hits: Sequence[Tuple[int, str, str]], *, source: str = "") -> str:
    """Render ``scan_text`` output as a readable assertion message."""
    if not hits:
        return ""
    head = f"{source}: " if source else ""
    lines = [f"{head}{len(hits)} ZT-SPEC-ECO-002 §31.1 terminology hit(s):"]
    for lineno, term, line in hits:
        lines.append(f"  line {lineno}: {term!r} -> {line}")
    return "\n".join(lines)
