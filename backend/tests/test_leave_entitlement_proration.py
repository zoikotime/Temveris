"""Joining-date proration of Annual Leave entitlement (ZT-WFM-LEAVE-001 §4).

Two layers, both deterministic:

  * ``prorated_annual_entitlement`` — the pure calendar-day formula. Asserted
    against the spec's worked examples to 6-decimal internal precision, plus
    leap-year February, a non-January leave year, and the empty-window edge.
  * ``_annual_base_entitlement`` — the router helper that decides WHEN to
    prorate: only an accrual-based type (accrual > 0) held by an employee with a
    recorded joining date; everything else keeps the flat rulebook cap. This is
    the guarantee that a null joining date, a fixed/per-event type, or a missing
    user can never be over- or under-granted.

Nothing here touches the database or the clock.
"""
from datetime import date, datetime

import pytest

from app.leave import policy as leave_policy


JAN_2026 = datetime(2026, 1, 1)


def _annual(start, *, end=None, ys=JAN_2026, rate=1.25, cap=15):
    return leave_policy.prorated_annual_entitlement(
        start=start, leave_year_start_dt=ys, monthly_rate=rate,
        annual_cap=cap, end=end,
    )


# ── The spec's worked examples (§4.3 / §16), 6-decimal internal precision ─────
@pytest.mark.parametrize("start, expected", [
    (date(2026, 1, 1),  15.000000),   # full year
    (date(2026, 2, 1),  13.750000),   # 11 complete months
    (date(2026, 7, 1),   7.500000),   # 6 complete months
    (date(2026, 8, 28),  5.161290),   # Aug 4/31 + Sep–Dec
    (date(2026, 12, 31), 0.040323),   # Dec 1/31
    (date(2026, 5, 15),  9.435484),   # May 17/31 + Jun–Dec
])
def test_worked_examples_joining_dates(start, expected):
    assert round(_annual(start), 6) == expected


def test_worked_example_termination_end_date():
    # 01 Jan – 30 Sep employment → 11.25
    assert round(_annual(date(2026, 1, 1), end=date(2026, 9, 30)), 6) == 11.250000


def test_full_year_hits_exactly_the_cap_not_over():
    # A complete year sums to exactly the cap even though 12 × 1.25 == 15.
    assert _annual(date(2026, 1, 1)) == 15.0
    # datetime inputs behave identically to date inputs.
    assert _annual(datetime(2026, 1, 1)) == 15.0


def test_continuing_employee_joined_before_leave_year_gets_full_year():
    # Joined mid-2025; for the 2026 leave year they are eligible all year.
    assert _annual(date(2025, 6, 10)) == 15.0


def test_leap_year_february_uses_29_days():
    # 2028 is a leap year. A late-Feb joiner must divide by 29, not 28.
    ys = datetime(2028, 1, 1)
    # Feb 28–29 = 2 eligible days / 29 + Mar–Dec (10 months).
    got = leave_policy.prorated_annual_entitlement(
        start=date(2028, 2, 28), leave_year_start_dt=ys,
        monthly_rate=1.25, annual_cap=15)
    assert round(got, 6) == round(2 / 29 * 1.25 + 10 * 1.25, 6)
    # A full February still contributes exactly one month.
    assert round(_annual(date(2028, 2, 1), ys=ys), 6) == 13.750000


def test_non_january_leave_year_april_start():
    # April–March leave year. Full year from Apr 1 = cap; Oct 1 = 6 months.
    apr = datetime(2026, 4, 1)
    assert _annual(date(2026, 4, 1), ys=apr) == 15.0
    assert round(_annual(date(2026, 10, 1), ys=apr), 6) == 7.500000


def test_empty_window_returns_zero():
    # Joining after the leave year has ended → nothing accrues (no negatives).
    assert _annual(date(2027, 1, 1)) == 0.0
    # end before start → 0.
    assert _annual(date(2026, 6, 1), end=date(2026, 5, 1)) == 0.0


def test_configurable_rate_and_cap():
    # The formula is rate/cap agnostic — a 2.0/day, 24-cap policy still works.
    assert round(_annual(date(2026, 1, 1), rate=2.0, cap=24), 6) == 24.0
    assert round(_annual(date(2026, 7, 1), rate=2.0, cap=24), 6) == 12.0


# ── The router helper: WHEN proration applies ─────────────────────────────────
class _FakeUser:
    def __init__(self, start):
        self.official_start_date = start


def _base(user, policy, monkeypatch=None):
    from app.leave import router
    if monkeypatch is not None:
        monkeypatch.setattr(leave_policy, "leave_year_start",
                            lambda db, org_id: JAN_2026)
    return router._annual_base_entitlement(user, policy, db=None, org_id=1)


def test_helper_prorates_accrual_type_with_joining_date(monkeypatch):
    got = _base(_FakeUser(datetime(2026, 8, 28)),
                {"limit": 15, "accrual": 1.25}, monkeypatch)
    assert round(got, 6) == 5.161290


def test_helper_null_start_date_is_grandfathered_to_full(monkeypatch):
    assert _base(_FakeUser(None), {"limit": 15, "accrual": 1.25}, monkeypatch) == 15.0


def test_helper_non_accrual_type_is_never_prorated(monkeypatch):
    # Maternity (182, accrual 0) must keep its fixed cap even for a mid-year joiner.
    got = _base(_FakeUser(datetime(2026, 8, 28)),
                {"limit": 182, "accrual": 0.0}, monkeypatch)
    assert got == 182.0


def test_helper_missing_user_falls_back_to_flat_limit(monkeypatch):
    assert _base(None, {"limit": 15, "accrual": 1.25}, monkeypatch) == 15.0
