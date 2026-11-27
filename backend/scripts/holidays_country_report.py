"""
Holiday country classification  —  READ-ONLY by default.

Purpose
=======
Migration ``m7n2p5q8r1t4`` added ``holidays.country_code`` but deliberately
backfilled NOTHING. This script is the reviewable path from "every existing
holiday is unscoped" to "each holiday names the calendar it belongs to".

Why the migration does not do it
--------------------------------
The obvious backfill — copy ``org_settings.primary_country`` onto every row —
is wrong on real data. The Public Holidays audit found:

    org 11  "Demo"          primary_country = US   →  13 Indian gazetted holidays
    org 12  "ZR Solutions"  primary_country = US   →  13 Indian gazetted holidays

Those rows predate the country-aware seeding fix. A blanket backfill would
stamp ``US`` onto Diwali and Republic Day, making a live data error permanent
and invisible. So the script INFERS each organization's real calendar from the
holiday names themselves, compares that with the declared country, and only
offers to write where the two agree.

What it does
============
For every organization holding holidays it reports:

  * the declared ``primary_country``
  * the country INFERRED by matching stored holiday names against the curated
    calendars in ``app/holidays/catalog.py``
  * a verdict:

        SCOPED      already has country_code on every row — nothing to do
        AGREED      inferred == declared, or inference is neutral   → safe
        MISMATCH    inferred != declared                            → human call
        UNKNOWN     no country declared and nothing inferable       → human call

Only ``AGREED`` organizations are touched by ``--apply``. Everything else is
listed for a person to decide, with the counts they need to decide it.

Usage
=====
    cd backend
    python scripts/holidays_country_report.py                 # report (default)
    python scripts/holidays_country_report.py --json
    python scripts/holidays_country_report.py --org 11        # one organization
    python scripts/holidays_country_report.py --apply         # write AGREED only
    python scripts/holidays_country_report.py --apply --org 7
    python scripts/holidays_country_report.py --revert --org 7   # undo a backfill

``--apply`` is reversible: ``--revert`` sets ``country_code`` back to NULL for
an organization, restoring the pre-backfill state exactly. Neither mode ever
deletes a holiday, changes a name or moves a date.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, text  # noqa: E402

from database.database import SessionLocal  # noqa: E402
from database import models  # noqa: E402
from app.holidays import catalog  # noqa: E402

VERDICT_SCOPED = "SCOPED"
VERDICT_AGREED = "AGREED"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_UNKNOWN = "UNKNOWN"

#: A country must claim at least this share of an organization's holiday names
#: before we call the inference meaningful.
_INFERENCE_THRESHOLD = 0.5

#: And at least this many DISTINCTIVE matches in absolute terms. A single
#: recognisable name in a hand-built calendar is a coincidence, not evidence.
_MIN_DISTINCTIVE_MATCHES = 3

#: Holidays observed across much of the world. They must never count toward a
#: country, however few curated calendars happen to list them.
#:
#: Computing "shared" purely by intersecting the curated calendars is not
#: enough: with only India and the UAE curated, "Christmas" appears in exactly
#: one of them and would score as India-distinctive. It obviously is not. This
#: list is the floor; the computed intersection is added on top, so adding more
#: curated countries only ever makes the inference more conservative.
_INTERNATIONAL_NAMES = frozenset({
    "new year's day", "new years day", "new year",
    "christmas", "christmas day", "christmas eve", "boxing day",
    "good friday", "easter", "easter monday",
    "labour day", "labor day", "may day", "international workers' day",
})


def _curated_name_index() -> Dict[str, set]:
    """``{country_code: {casefolded holiday name}}`` across every curated year."""
    index: Dict[str, set] = defaultdict(set)
    for code, by_year in catalog.CURATED_CALENDARS.items():
        for entries in by_year.values():
            for entry in entries:
                index[code].add(entry["name"].casefold())
    return dict(index)


def _uninformative_names(index: Dict[str, set]) -> set:
    """Names that identify no country: internationally common, or present in
    more than one curated calendar."""
    shared = set(_INTERNATIONAL_NAMES)
    for code_a, set_a in index.items():
        for code_b, set_b in index.items():
            if code_a < code_b:
                shared |= (set_a & set_b)
    return shared


def _infer_country(names: List[str], index: Dict[str, set]) -> Optional[str]:
    """The curated country whose calendar best explains these holiday names.

    ``None`` means the evidence is not strong enough — a valid and common
    answer for a hand-built company calendar, and the safe one: an
    organization we cannot read is reported for a human decision rather than
    guessed at.

    Three guards, all of which must pass:
      * the name must be DISTINCTIVE (see ``_uninformative_names``),
      * at least ``_MIN_DISTINCTIVE_MATCHES`` of them must match,
      * they must cover at least ``_INFERENCE_THRESHOLD`` of the calendar,
      * and one country must win outright — a tie is not evidence.
    """
    folded = [n.casefold() for n in names if n]
    if not folded:
        return None

    uninformative = _uninformative_names(index)
    informative_total = sum(1 for n in folded if n not in uninformative)
    if informative_total == 0:
        return None

    scores: Dict[str, int] = {
        code: sum(1 for n in folded if n in (known - uninformative))
        for code, known in index.items()
    }
    if not scores:
        return None

    best = max(scores, key=lambda c: scores[c])
    if scores[best] < _MIN_DISTINCTIVE_MATCHES:
        return None
    # Measured against the informative names only: padding a real Indian
    # calendar with custom days should not dilute it below the threshold.
    if scores[best] / informative_total < _INFERENCE_THRESHOLD:
        return None
    if sorted(scores.values(), reverse=True)[1:2] == [scores[best]]:
        return None
    return best


def _classify(declared: Optional[str], inferred: Optional[str],
              unscoped: int) -> str:
    if unscoped == 0:
        return VERDICT_SCOPED
    if declared and inferred and declared != inferred:
        return VERDICT_MISMATCH
    if declared and (inferred is None or inferred == declared):
        return VERDICT_AGREED
    if not declared and inferred:
        # Nothing declared, but the calendar itself is unmistakable.
        return VERDICT_MISMATCH
    return VERDICT_UNKNOWN


def collect(db, org_filter: Optional[int] = None) -> List[Dict[str, Any]]:
    index = _curated_name_index()

    query = (
        db.query(
            models.Holiday.organization_id,
            func.count(models.Holiday.id),
        )
        .group_by(models.Holiday.organization_id)
        .order_by(models.Holiday.organization_id)
    )
    if org_filter is not None:
        query = query.filter(models.Holiday.organization_id == org_filter)

    report: List[Dict[str, Any]] = []
    for org_id, total in query.all():
        rows = (
            db.query(models.Holiday.name, models.Holiday.date,
                     models.Holiday.country_code)
            .filter(models.Holiday.organization_id == org_id)
            .all()
        )
        names = [r.name for r in rows]
        unscoped = sum(1 for r in rows if r.country_code is None)
        existing_codes = sorted({r.country_code for r in rows if r.country_code})

        org = db.query(models.Organization.name).filter(
            models.Organization.id == org_id).first()
        settings = db.query(models.OrgSettings.primary_country).filter(
            models.OrgSettings.organization_id == org_id).first()
        declared = ((settings[0] if settings else None) or "").strip().upper() or None

        inferred = _infer_country(names, index)
        verdict = _classify(declared, inferred, unscoped)

        report.append({
            "organization_id": org_id,
            "organization_name": (org[0] if org else None) or "(unknown)",
            "declared_country": declared,
            "inferred_country": inferred,
            "total_holidays": total,
            "unscoped_holidays": unscoped,
            "existing_country_codes": existing_codes,
            "years": sorted({r.date.year for r in rows if r.date}),
            "verdict": verdict,
            "sample_names": names[:8],
        })
    return report


def apply_backfill(db, report: List[Dict[str, Any]], *, dry_run: bool = False) -> Dict[str, Any]:
    """Set ``country_code`` on unscoped rows for AGREED organizations only."""
    changed: Dict[int, int] = {}
    for entry in report:
        if entry["verdict"] != VERDICT_AGREED:
            continue
        target = entry["declared_country"]
        if not target:
            continue
        count = (
            db.query(models.Holiday)
            .filter(
                models.Holiday.organization_id == entry["organization_id"],
                models.Holiday.country_code.is_(None),
            )
            .update({models.Holiday.country_code: target}, synchronize_session=False)
        )
        changed[entry["organization_id"]] = count
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return {"organizations": len(changed), "rows": sum(changed.values()), "detail": changed}


def revert(db, org_id: int) -> int:
    """Set ``country_code`` back to NULL for one organization."""
    count = (
        db.query(models.Holiday)
        .filter(
            models.Holiday.organization_id == org_id,
            models.Holiday.country_code.isnot(None),
        )
        .update({models.Holiday.country_code: None}, synchronize_session=False)
    )
    db.commit()
    return count


_VERDICT_NOTE = {
    VERDICT_SCOPED: "already classified",
    VERDICT_AGREED: "safe to backfill from primary_country",
    VERDICT_MISMATCH: "NEEDS A DECISION - stored calendar does not match the declared country",
    VERDICT_UNKNOWN: "NEEDS A DECISION - no country declared and none inferable",
}


def render(report: List[Dict[str, Any]]) -> str:
    if not report:
        return "No organizations hold any holidays.\n"

    lines = ["", "HOLIDAY COUNTRY CLASSIFICATION", "=" * 78, ""]
    header = f"{'org':>5}  {'organization':<26} {'decl':<5} {'infer':<5} {'rows':>5} {'unscoped':>9}  verdict"
    lines.append(header)
    lines.append("-" * 78)
    for e in sorted(report, key=lambda r: (r["verdict"], r["organization_id"])):
        lines.append(
            f"{e['organization_id']:>5}  {e['organization_name'][:26]:<26} "
            f"{(e['declared_country'] or '-'):<5} {(e['inferred_country'] or '-'):<5} "
            f"{e['total_holidays']:>5} {e['unscoped_holidays']:>9}  {e['verdict']}"
        )

    lines += ["", "VERDICTS", "-" * 78]
    for verdict in (VERDICT_SCOPED, VERDICT_AGREED, VERDICT_MISMATCH, VERDICT_UNKNOWN):
        count = sum(1 for e in report if e["verdict"] == verdict)
        lines.append(f"  {verdict:<9} {count:>3}   {_VERDICT_NOTE[verdict]}")

    needs = [e for e in report if e["verdict"] in (VERDICT_MISMATCH, VERDICT_UNKNOWN)]
    if needs:
        lines += ["", "ORGANIZATIONS NEEDING A HUMAN DECISION", "=" * 78]
        for e in needs:
            lines += [
                "",
                f"  org {e['organization_id']} - {e['organization_name']}",
                f"    declared primary_country : {e['declared_country'] or '(none)'}",
                f"    calendar looks like      : {e['inferred_country'] or '(unrecognised)'}",
                f"    holidays                 : {e['total_holidays']} across {e['years']}",
                f"    sample                   : {', '.join(e['sample_names'][:6])}",
                "    options:",
                "      (a) the declared country is right  -> correct the holidays, then re-run",
                "      (b) the calendar is right          -> fix org_settings.primary_country,",
                "                                            then re-run and --apply",
                "      (c) it is a deliberate mixed calendar -> set country_code per row by hand",
            ]

    lines += ["", "Nothing was written. Re-run with --apply to backfill AGREED "
                  "organizations only.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--org", type=int, default=None, help="limit to one organization id")
    parser.add_argument("--apply", action="store_true",
                        help="backfill country_code for AGREED organizations")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --apply, roll back instead of committing")
    parser.add_argument("--revert", action="store_true",
                        help="clear country_code for --org (undo a backfill)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.revert:
            if args.org is None:
                print("--revert requires --org <id>", file=sys.stderr)
                return 2
            cleared = revert(db, args.org)
            print(f"Cleared country_code on {cleared} holiday(s) for org {args.org}.")
            return 0

        report = collect(db, args.org)

        if args.apply:
            result = apply_backfill(db, report, dry_run=args.dry_run)
            verb = "Would update" if args.dry_run else "Updated"
            print(f"{verb} {result['rows']} holiday row(s) across "
                  f"{result['organizations']} organization(s).")
            for org_id, count in sorted(result["detail"].items()):
                print(f"  org {org_id}: {count} row(s)")
            skipped = [e for e in report
                       if e["verdict"] in (VERDICT_MISMATCH, VERDICT_UNKNOWN)]
            if skipped:
                print(f"\nSkipped {len(skipped)} organization(s) needing a decision: "
                      f"{', '.join(str(e['organization_id']) for e in skipped)}")
                print("Run without --apply for the full breakdown.")
            return 0

        print(json.dumps(report, indent=2, default=str) if args.json else render(report))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
