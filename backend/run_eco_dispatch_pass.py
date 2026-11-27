"""
ECO V2 — run ONE notification dispatch pass for ONE organization.

Owner ruling D-F: B. The authorized operational invocation of the D-C trigger,
following this repository's existing top-level operational-script convention
(``create_admin.py``, ``put_all_on_leave_today.py``).

Usage (from backend/):
    python run_eco_dispatch_pass.py --organization-id 42

Exit status:
    0   a pass ran
    3   rollout prevented it (OFF or SHADOW) — not a failure
    2   usage error (argparse)
    1   genuine failure; the traceback is printed, never swallowed

ON DEMAND ONLY. One invocation is one pass. If more than
``MAX_HANDOFFS_PER_PASS`` (50) recipients are pending, this exits with
``truncated`` set and ANOTHER explicit invocation is required. There is no
loop here, no scheduler, no background worker and no startup task — D-C
withheld all of them and D-F did not grant any.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
It does not select work, resolve a recipient, choose a channel, resolve
preferences, suppress, retry, transport, record a delivery, or read the rollout
control. Every one of those belongs to a layer below it. Its entire job is to
parse one argument, open a session, call ``run_eco_dispatch_pass`` exactly
once, and print what came back — so there is no behaviour here that could drift
from the layer that owns it.

There is no ``--all``, no ``--batch-size`` and no way to force ENABLED. An
organization cannot be inferred, defaulted or wildcarded, the 50-handoff
ceiling lives in the processor, and the rollout gate lives in the trigger.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from dotenv import load_dotenv

load_dotenv()

#: Exit codes. Named so the operational meaning is not a bare integer.
EXIT_OK = 0
EXIT_ROLLOUT_PREVENTED = 3
EXIT_FAILURE = 1


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eco_dispatch_pass.py",
        description="Run ONE ECO notification dispatch pass for ONE "
                    "organization. On demand only.",
    )
    # REQUIRED, and deliberately has no default: D-F forbids inferring an
    # organization, processing all of them, or accepting a wildcard. A missing
    # value is a usage error, not a reason to guess.
    parser.add_argument(
        "--organization-id", type=int, required=True, dest="organization_id",
        help="The organization to process. Required; there is no --all.",
    )
    return parser.parse_args(argv)


def _report(result) -> List[str]:
    """Operator-facing lines. PII-free by construction.

    Only counts, states and identifiers the operator needs. No address, no
    phone number, no Worker name, no notification body, no provider id, no
    provider diagnostic, no correlation id — the per-recipient summary reports
    OUTCOMES, never content or contact data.
    """
    lines = [
        f"organization_id : {result.organization_id}",
        f"rollout_state   : {result.rollout_state}",
        f"processed       : {result.processed}",
    ]
    if not result.processed:
        lines.append(f"detail          : {result.detail or ''}")
        return lines

    inner = result.result
    lines += [
        f"handoffs        : {inner.handoffs}",
        f"recorded        : {inner.recorded}",
        f"skipped_shadow  : {inner.skipped_shadow}",
        f"truncated       : {inner.truncated}",
    ]
    if inner.truncated:
        lines.append("note            : the 50-handoff ceiling was reached; "
                     "run again for the remainder")
    if inner.outcomes:
        lines.append("outcomes        :")
        for outcome in inner.outcomes:
            # Identifiers and results only. `recipient_user_id` is an identity,
            # not contact data, and is what an operator needs to correlate.
            lines.append(
                f"  event={outcome.eco_event_id} "
                f"recipient={outcome.recipient_user_id} "
                f"channel={outcome.channel or '-'} "
                f"outcome={outcome.outcome} "
                f"reason={outcome.reason_code} "
                f"recorded={outcome.recorded}")
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
    from app.alert import run_eco_dispatch_pass

    db = session_factory()
    try:
        # EXACTLY ONE invocation. The organization is passed straight through:
        # the trigger is the tenant scope and the rollout gate, and this script
        # resolves neither a second time.
        result = run_eco_dispatch_pass(db, organization_id=args.organization_id)
        db.commit()
    except Exception:
        # Not swallowed. Roll back what this pass attempted, then re-raise so
        # the traceback reaches the operator and the process exits non-zero.
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
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
