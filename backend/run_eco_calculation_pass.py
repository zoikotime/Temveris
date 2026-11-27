"""
ECO V2 — run ONE calculation pass for ONE organization and ONE local work date.

The authorized operational invocation of the ECO v2 calculation chain,
following the convention D-F established for the dispatch pass and this
repository's existing top-level operational scripts (``create_admin.py``,
``put_all_on_leave_today.py``, ``run_eco_dispatch_pass.py``).

Usage (from backend/):
    python run_eco_calculation_pass.py --organization-id 42 --work-date 2026-09-21
    python run_eco_calculation_pass.py --organization-id 42 --work-date 2026-09-21 --user-id 7

Exit status:
    0   a pass ran
    3   rollout prevented it (OFF) — not a failure
    4   a pass ran and at least one Worker-day FAILED a stage
    2   usage error (argparse)
    1   genuine failure; the traceback is printed, never swallowed

WHY A COMMAND AND NOT AN EVENT HOOK
-----------------------------------
The producer's own D18-F3 gate 1 refuses any day whose organization-local day
has not ENDED (``DAY_NOT_ENDED``). A hook on clock-out therefore cannot work:
a Worker clocking out at 18:10 local does so hours before the day closes, and
the producer would refuse every such call. Sealing is a day-completion event,
not a clock-out event, so the invocation has to happen after the local day ends
— and this repository's answer to "something must happen after a boundary" is
an explicit operational command, not a daemon. ``app/alert/__init__.py`` states
the precedent: "No scheduler."

ON DEMAND ONLY. One invocation is one organization, one local date. There is no
loop, no scheduler, no background worker and no startup task.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
It does not seal a ledger, evaluate a day, fold a week, compare a threshold,
raise an alert, record a notification, deliver anything, or read the rollout
control. Every one of those belongs to a layer below it. Its entire job is to
parse three arguments, open a session, call ``run_eco_calculation_pass``
exactly once, and print what came back.

There is no ``--all``, no date range and no way to force ENABLED. An
organization cannot be inferred, defaulted or wildcarded, and the rollout gate
lives in the driver.

DELIVERY IS A SEPARATE PASS. This script records; it sends nothing. Under an
ENABLED rollout, ``run_eco_dispatch_pass.py`` is what delivers, and it is
separately gated.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from typing import List, Optional, Sequence

from dotenv import load_dotenv

load_dotenv()

#: Exit codes. Named so the operational meaning is not a bare integer.
EXIT_OK = 0
EXIT_ROLLOUT_PREVENTED = 3
EXIT_STAGE_FAILURES = 4
EXIT_FAILURE = 1


def _work_date(value: str) -> date:
    """A calendar date, strictly. No 'today', no offsets, no natural language.

    The work date is an organization-LOCAL calendar label, and inferring it
    from the server's clock would make the same command mean different days in
    different deployments.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eco_calculation_pass.py",
        description="Run ONE ECO calculation pass for ONE organization and "
                    "ONE organization-local work date. On demand only.",
    )
    parser.add_argument(
        "--organization-id", type=int, required=True, dest="organization_id",
        help="The organization to process. Required; there is no --all.",
    )
    parser.add_argument(
        "--work-date", type=_work_date, required=True, dest="work_date",
        help="The organization-local calendar date, YYYY-MM-DD. Required; "
             "there is no default and no range.",
    )
    parser.add_argument(
        "--user-id", type=int, default=None, dest="user_id",
        help="Optional. Narrow the pass to ONE Worker. Omit to process every "
             "Worker with clock evidence on that local date.",
    )
    return parser.parse_args(argv)


def _report(result) -> List[str]:
    """Operator-facing lines. PII-free by construction.

    Only counts, stages, component outcomes and identifiers. No Worker name, no
    address, no contact detail, no notification body, no rendered copy and no
    reason-for-absence — the per-Worker summary reports WHICH STAGE and WHAT IT
    ANSWERED, never content.
    """
    lines = [
        f"organization_id : {result.organization_id}",
        f"work_date       : {result.work_date.isoformat()}",
        f"rollout_state   : {result.rollout_state}",
        f"processed       : {result.processed}",
    ]
    if not result.processed:
        lines.append(f"detail          : {result.detail or ''}")
        return lines

    lines += [
        f"mode            : {result.mode}",
        f"workers         : {result.workers_selected}",
        f"failed          : {len(result.failures)}",
    ]
    if result.results:
        lines.append("worker-days     :")
        for r in result.results:
            # `user_id` is an identity, not contact data, and is what an
            # operator needs to correlate a stage outcome back to a Worker.
            lines.append(
                f"  user={r.user_id} stage={r.stage.value} "
                f"ledger={r.ledger_outcome or '-'} "
                f"day={r.day_outcome or '-'}/{r.day_state or '-'} "
                f"alert={'yes' if r.day_alert_raised else '-'} "
                f"event={r.event_outcome or '-'} "
                f"notification={r.notification_outcome or '-'}"
                + (f" failure={r.failure_stage.value}:{r.failure_type}"
                   if r.failed else ""))
    return lines


def main(argv: Optional[Sequence[str]] = None,
         session_factory=None) -> int:
    """One pass. Returns the exit status; raises on a genuine failure.

    ``session_factory`` exists so a test can supply an isolated session. It
    defaults to the application's own ``SessionLocal`` — this script introduces
    no second database architecture.
    """
    args = _parse_args(argv)

    if session_factory is None:
        from database.database import SessionLocal

        session_factory = SessionLocal

    # Imported here, after `load_dotenv`, matching the existing scripts.
    from app.eco_driver import run_eco_calculation_pass

    db = session_factory()
    try:
        # EXACTLY ONE invocation. Scope and gate are both passed straight
        # through: the driver is the tenant scope and the rollout gate, and
        # this script resolves neither a second time. It does NOT commit — the
        # driver commits per stage, which is what makes a retry resumable.
        result = run_eco_calculation_pass(
            db, organization_id=args.organization_id,
            work_date=args.work_date, user_id=args.user_id)
    except Exception:
        # Not swallowed. Roll back anything uncommitted, then re-raise so the
        # traceback reaches the operator and the process exits non-zero.
        db.rollback()
        raise
    finally:
        db.close()

    for line in _report(result):
        print(line)

    if not result.processed:
        # Rollout declined. A distinct status, because "nothing ran because the
        # control says not to" is not a failure and must not page anybody.
        return EXIT_ROLLOUT_PREVENTED
    if result.any_failed:
        # The pass ran and the rest of the tenant was processed, but at least
        # one Worker-day hit a defect. Distinct from both success and from a
        # process-level crash, so it can be alerted on without masking either.
        return EXIT_STAGE_FAILURES
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
