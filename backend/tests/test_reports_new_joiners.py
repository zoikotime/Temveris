"""New Joiners report — date-range + org-scoping regression test.

Covers the /reports/new-joiners logic added for "how many employees joined
between X and Y": only users whose official_start_date falls in [from, to]
AND belong to the caller's org are counted.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from database import models
from app.reports.router import new_joiners

import itertools
import uuid


# The in-memory DB is SESSION-scoped and shared by every test module, so this
# module cannot own ids 101-107 or organizations 1 and 2 — another module
# reaching them first made this test fail with UNIQUE constraint violations,
# but only when the full suite ran. Private id ranges and unique names keep the
# test order-independent without changing what it asserts.
_ID = itertools.count(770001)
_ORG = itertools.count(770001)
_TAG = uuid.uuid4().hex[:6]


def _mk_user(db, org_id, name, joined):
    u = models.User(
        id=next(_ID), name=f"{name}-{_TAG}", email=f"{name.lower()}-{_TAG}@ex.com",
        role="employee", organization_id=org_id, is_active=True,
        official_start_date=datetime(*joined) if joined else None,
    )
    db.add(u)
    return u


def test_new_joiners_filters_by_range_and_org(db):
    mine, other = next(_ORG), next(_ORG)
    _mk_user(db, mine,  "InRangeA",   (2026, 5, 20))   # in range          ✓
    _mk_user(db, mine,  "OnStart",    (2026, 5, 16))   # boundary start    ✓
    _mk_user(db, mine,  "OnEnd",      (2026, 6, 21))   # boundary end      ✓
    _mk_user(db, mine,  "TooEarly",   (2026, 5, 15))   # before range      ✗
    _mk_user(db, mine,  "TooLate",    (2026, 6, 22))   # after range       ✗
    _mk_user(db, mine,  "NoDate",     None)            # no start date     ✗
    _mk_user(db, other, "OtherOrg",   (2026, 5, 20))   # different org     ✗
    db.commit()

    res = new_joiners(
        from_date="2026-05-16", to_date="2026-06-21",
        db=db, current_org=SimpleNamespace(id=mine),
    )

    assert res["count"] == 3
    names = {r["name"] for r in res["rows"]}
    assert names == {f"InRangeA-{_TAG}", f"OnStart-{_TAG}", f"OnEnd-{_TAG}"}
    # Sorted ascending by join date, and each row carries the join date.
    assert [r["joined_on"] for r in res["rows"]] == ["2026-05-16", "2026-05-20", "2026-06-21"]
