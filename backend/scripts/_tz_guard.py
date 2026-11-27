"""Refuse to run an India-bucketed maintenance script against a non-IST tenant.

These scripts rebuild DERIVED daily data (activity windows, daily active-time
scores) and they bucket rows with a hardcoded ``+05:30``. That was correct when
every workspace was in India. It is not correct now: run one of them today
against a New York organization and it writes India-bucketed daily rows into
that tenant's history, silently.

Rewriting the scripts to be organization-aware is the real fix and is deliberately
NOT done here — they are manual, offline tools, and a rushed rewrite of a data
backfill is a worse risk than a guard. What this does is make the unsafe case
impossible to run by accident: if any organization resolves to a zone whose
offset is not +05:30, the script stops and names them.

``--force`` exists for the operator who genuinely means it (e.g. a single-tenant
staging database) and it says so in the log.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

_IST_OFFSET = timedelta(hours=5, minutes=30)


def non_ist_organizations(db) -> list:
    """[(org_id, zone_name, utc_offset)] for every org not currently on +05:30.

    Resolution goes through ``app._tz``, the same helper the runtime uses, so an
    organization with a missing or invalid timezone resolves exactly as the
    product resolves it rather than being judged by a second implementation.
    """
    from database import models
    from app import _tz

    now = datetime.now(timezone.utc)
    out = []
    for (org_id,) in db.query(models.Organization.id).all():
        zone = _tz.org_timezone(db, org_id)
        offset = now.astimezone(zone).utcoffset()
        if offset != _IST_OFFSET:
            out.append((org_id, _tz.org_timezone_name(db, org_id), str(offset)))
    return out


def require_ist_only(db, log, *, force: bool = False) -> None:
    """Stop the script unless every organization is on +05:30.

    Call this at the top of ``main()``, before any write.
    """
    offenders = non_ist_organizations(db)
    if not offenders:
        return
    listed = ", ".join(f"org {o} = {name} ({off})" for o, name, off in offenders)
    if force:
        log.warning(
            "--force: proceeding although %d organization(s) are NOT on +05:30 "
            "(%s). This script buckets days on India time; their derived rows "
            "will be written on the wrong local day.", len(offenders), listed,
        )
        return
    log.error(
        "REFUSING TO RUN: %d organization(s) are not on +05:30 (%s). This "
        "script buckets days with a hardcoded +05:30 and would write their "
        "derived daily rows on the wrong local day. Make the script "
        "organization-aware, or re-run with --force if you accept that.",
        len(offenders), listed,
    )
    sys.exit(2)
