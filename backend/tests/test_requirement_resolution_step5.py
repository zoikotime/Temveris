"""
STEP 5 — strict requirement resolution.
=======================================

Owner ruling D-10 Option A: the clock-out path stays fail-open and
non-blocking; the evaluation path fails closed.

INDEPENDENT EXPECTED VALUES
---------------------------
Every expected requirement below is a number chosen in this file (525, 480,
240, 300 …), never imported from the implementation. The only implementation
constant referenced is ``MAX_REQUIRED_MINUTES``, and it is compared against an
independently stated 1440 rather than used as its own expected value — a test
that asserts ``X == X`` proves nothing.

The provider is a hand-built fake. That is the point of the port: the resolver's
behaviour is fully determined without a configuration store existing, which is
why Step 5 needed no schema change.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

import pytest

from app.requirement_resolution import (
    ConfigVersionRef,
    MAX_REQUIRED_MINUTES,
    RequirementCandidate,
    RequirementResolution,
    ResolutionOutcome,
    resolve_requirement_strict,
)

ORG = 4100
USER = 900
D = date(2026, 9, 4)


class FakeProvider:
    """A deterministic stand-in for the Step 6 configuration store."""

    def __init__(
        self,
        *,
        versions: Optional[Dict[int, List[tuple]]] = None,
        candidates: Optional[Sequence[RequirementCandidate]] = None,
        candidates_by_user: Optional[Dict[int, Sequence[RequirementCandidate]]] = None,
    ):
        # org -> [(effective_from, version_id), ...] ascending.
        # `is None` rather than `or`: an explicitly empty dict means "this
        # organization has captured no versions", which is a case under test.
        self.versions = (
            {ORG: [(date(2026, 1, 1), "cfg-v1")]} if versions is None else versions
        )
        self.candidates = [] if candidates is None else candidates
        self.candidates_by_user = {} if candidates_by_user is None else candidates_by_user
        self.calls: List[tuple] = []

    def version_as_of(self, *, organization_id, as_of):
        self.calls.append(("version_as_of", organization_id, as_of))
        rows = self.versions.get(organization_id, [])
        chosen = None
        for eff_from, vid in sorted(rows):
            if eff_from <= as_of:
                chosen = ConfigVersionRef(config_version_id=vid, effective_from=eff_from)
        return chosen  # None when as_of predates the first captured version

    def requirement_candidates(self, *, organization_id, user_id, as_of, version):
        self.calls.append(("candidates", organization_id, user_id, as_of,
                           version.config_version_id))
        if user_id in self.candidates_by_user:
            return self.candidates_by_user[user_id]
        return self.candidates


def _resolve(provider, *, org=ORG, user=USER, on=D) -> RequirementResolution:
    return resolve_requirement_strict(
        organization_id=org, user_id=user, as_of=on, provider=provider
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Successful resolution and precedence
# ══════════════════════════════════════════════════════════════════════════════

def test_resolves_the_configured_requirement():
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=480, source_ref="org:4100"),
    ])
    r = _resolve(p)
    assert r.outcome is ResolutionOutcome.RESOLVED
    assert r.required_minutes == 480
    assert r.source_tier == "org"
    assert r.source_ref == "org:4100"
    assert r.is_evaluable is True


def test_first_rank_wins_and_lower_ranks_are_not_blended():
    """§10.2: 'The first rank that yields a value wins. Lower ranks are not
    consulted and are not blended.'"""
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="assignment", rank=1, minutes=300),
        RequirementCandidate(tier="employee", rank=3, minutes=480),
        RequirementCandidate(tier="org", rank=5, minutes=525),
    ])
    r = _resolve(p)
    assert r.required_minutes == 300          # not 480, not 525, not an average
    assert r.source_rank == 1


def test_a_silent_tier_defers_to_the_next_rank():
    """A tier configured with no value is not an answer; it is a pass."""
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="assignment", rank=1, minutes=None),
        RequirementCandidate(tier="employee", rank=3, minutes=420),
    ])
    r = _resolve(p)
    assert r.required_minutes == 420
    assert r.source_tier == "employee"


def test_chain_evaluated_records_the_tiers_consulted():
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="assignment", rank=1, minutes=None),
        RequirementCandidate(tier="shift", rank=2, minutes=None),
        RequirementCandidate(tier="employee", rank=3, minutes=525),
        RequirementCandidate(tier="org", rank=5, minutes=480),
    ])
    r = _resolve(p)
    assert r.chain_evaluated == ("assignment", "shift", "employee")
    assert "org" not in r.chain_evaluated  # lower rank never consulted


# ══════════════════════════════════════════════════════════════════════════════
# 2. Determinism
# ══════════════════════════════════════════════════════════════════════════════

def test_provider_return_order_cannot_change_the_answer():
    """INV-8: a provider reordering its rows must not move the result."""
    forward = [
        RequirementCandidate(tier="assignment", rank=1, minutes=300),
        RequirementCandidate(tier="org", rank=5, minutes=525),
    ]
    a = _resolve(FakeProvider(candidates=forward))
    b = _resolve(FakeProvider(candidates=list(reversed(forward))))
    assert a.required_minutes == b.required_minutes == 300


def test_equal_ranks_break_ties_deterministically_by_tier():
    same = [
        RequirementCandidate(tier="zeta", rank=2, minutes=100),
        RequirementCandidate(tier="alpha", rank=2, minutes=200),
    ]
    a = _resolve(FakeProvider(candidates=same))
    b = _resolve(FakeProvider(candidates=list(reversed(same))))
    assert a.required_minutes == b.required_minutes == 200  # "alpha" sorts first


def test_repeated_resolution_is_identical():
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=525),
    ])
    assert _resolve(p) == _resolve(p)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Historical version selection and effective-date boundaries
# ══════════════════════════════════════════════════════════════════════════════

def _versioned_provider():
    return FakeProvider(
        versions={ORG: [
            (date(2026, 1, 1), "cfg-v3"),
            (date(2026, 9, 1), "cfg-v4"),
        ]},
        candidates=[RequirementCandidate(tier="org", rank=5, minutes=525)],
    )


def test_historical_date_selects_the_version_in_force_then():
    r = _resolve(_versioned_provider(), on=date(2026, 8, 28))
    assert r.config_version_id == "cfg-v3"
    assert r.effective_from == date(2026, 1, 1)


def test_later_date_selects_the_later_version():
    r = _resolve(_versioned_provider(), on=date(2026, 9, 4))
    assert r.config_version_id == "cfg-v4"


def test_effective_from_boundary_is_inclusive():
    """A version effective on the 1st governs the 1st."""
    r = _resolve(_versioned_provider(), on=date(2026, 9, 1))
    assert r.config_version_id == "cfg-v4"


def test_day_before_effective_from_uses_the_previous_version():
    r = _resolve(_versioned_provider(), on=date(2026, 8, 31))
    assert r.config_version_id == "cfg-v3"


def test_datetime_input_is_normalised_to_its_date():
    r = _resolve(_versioned_provider(), on=datetime(2026, 8, 28, 17, 30))
    assert r.as_of == date(2026, 8, 28)
    assert r.config_version_id == "cfg-v3"


# ══════════════════════════════════════════════════════════════════════════════
# 4. The historical floor — NO_VERSION, never a live fallback
# ══════════════════════════════════════════════════════════════════════════════

def test_date_before_the_first_captured_version_returns_no_version():
    """Gate 7 / C-06: 'resolves to NO VERSION. It must never silently fall
    through to live values.'"""
    r = _resolve(_versioned_provider(), on=date(2025, 12, 31))
    assert r.outcome is ResolutionOutcome.NO_VERSION
    assert r.required_minutes is None
    assert r.is_evaluable is False


def test_no_version_does_not_consult_candidates_at_all():
    """Falling through to tiers would be the live fallback by another route."""
    p = _versioned_provider()
    _resolve(p, on=date(2025, 6, 1))
    assert not any(c[0] == "candidates" for c in p.calls)


def test_no_version_carries_no_config_identity():
    r = _resolve(_versioned_provider(), on=date(2025, 6, 1))
    assert r.config_version_id is None
    assert r.effective_from is None


def test_no_version_does_not_alert_an_administrator():
    """Configuration history simply does not reach that far back — that is a
    property of the cutover, not a defect an administrator can fix."""
    r = _resolve(_versioned_provider(), on=date(2025, 6, 1))
    assert r.requires_administrator_alert is False


def test_an_organization_with_no_captured_versions_gets_no_version():
    p = FakeProvider(versions={}, candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=525),
    ])
    assert _resolve(p).outcome is ResolutionOutcome.NO_VERSION


# ══════════════════════════════════════════════════════════════════════════════
# 5. REQUIREMENT_UNRESOLVED — no silent default, no silent zero
# ══════════════════════════════════════════════════════════════════════════════

def test_no_rank_yielding_a_value_is_requirement_unresolved():
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="assignment", rank=1, minutes=None),
        RequirementCandidate(tier="org", rank=5, minutes=None),
    ])
    r = _resolve(p)
    assert r.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
    assert r.required_minutes is None


def test_no_candidates_at_all_is_requirement_unresolved():
    r = _resolve(FakeProvider(candidates=[]))
    assert r.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED


def test_unresolved_never_substitutes_a_default():
    """The platform's fail-open resolver terminates at 28800 seconds (480 min).
    The strict resolver must never produce that, or any other number."""
    r = _resolve(FakeProvider(candidates=[]))
    assert r.required_minutes is None
    assert r.required_minutes != 480
    assert r.required_minutes != 525


def test_unresolved_never_substitutes_zero():
    """Zero would silently mean 'no shortfall is possible'."""
    r = _resolve(FakeProvider(candidates=[]))
    assert r.required_minutes is None
    assert r.required_minutes != 0


def test_unresolved_retains_the_config_identity_it_did_resolve():
    """Configuration existed; no rank answered. That distinction is what makes
    it diagnosable, and is why NO_VERSION is a separate outcome."""
    r = _resolve(FakeProvider(
        versions={ORG: [(date(2026, 1, 1), "cfg-v3")]}, candidates=[]))
    assert r.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
    assert r.config_version_id == "cfg-v3"


def test_unresolved_alerts_the_administrator_and_not_the_worker():
    """§10.2 operational alert; §24.3 the Worker MUST NOT be shown anything."""
    r = _resolve(FakeProvider(candidates=[]))
    assert r.requires_administrator_alert is True
    assert r.is_worker_visible is False


def test_the_result_type_refuses_to_carry_a_number_when_unresolved():
    """Enforced by construction, so a future caller cannot bypass §10.2 by
    populating the field."""
    with pytest.raises(ValueError):
        RequirementResolution(
            outcome=ResolutionOutcome.REQUIREMENT_UNRESOLVED,
            organization_id=ORG, user_id=USER, as_of=D,
            required_minutes=480,
        )


def test_the_result_type_refuses_a_resolved_outcome_without_a_number():
    with pytest.raises(ValueError):
        RequirementResolution(
            outcome=ResolutionOutcome.RESOLVED,
            organization_id=ORG, user_id=USER, as_of=D,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 6. Invalid configuration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [0, -1, -480, 1441, 100000])
def test_out_of_range_values_are_rejected_not_used(bad):
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=bad),
    ])
    r = _resolve(p)
    assert r.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
    assert r.required_minutes is None
    assert len(r.invalid_candidates) == 1


@pytest.mark.parametrize("bad", ["480", 480.5, "8:00", [480]])
def test_non_integer_values_are_rejected(bad):
    """A requirement is integer minutes. §10.3 is explicit that decimal hours
    MUST NOT be stored or transmitted: '8:75 is not a time and will be typed
    by someone.'"""
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=bad),
    ])
    r = _resolve(p)
    assert r.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
    assert r.required_minutes is None


def test_bool_is_not_accepted_as_minutes():
    """bool subclasses int in Python; True would otherwise mean 1 minute."""
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=True),
    ])
    assert _resolve(p).outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED


def test_an_invalid_high_rank_does_not_silently_fall_to_a_lower_rank():
    """A tier that answered WRONGLY is a defect to surface, not a pass. The
    lower rank must not quietly supply a plausible number in its place."""
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="assignment", rank=1, minutes=-5),
        RequirementCandidate(tier="org", rank=5, minutes=525),
    ])
    r = _resolve(p)
    assert r.required_minutes == 525          # resolution continues
    assert r.source_tier == "org"
    assert len(r.invalid_candidates) == 1     # but the defect is recorded
    assert r.invalid_candidates[0].tier == "assignment"


def test_the_maximum_bound_is_a_full_day():
    assert MAX_REQUIRED_MINUTES == 1440


def test_a_full_day_requirement_is_accepted():
    p = FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=1440),
    ])
    assert _resolve(p).required_minutes == 1440


# ══════════════════════════════════════════════════════════════════════════════
# 7. Tenant / organization isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_resolution_is_organization_scoped():
    p = FakeProvider(versions={
        4100: [(date(2026, 1, 1), "cfg-A")],
        4200: [(date(2026, 5, 1), "cfg-B")],
    }, candidates=[RequirementCandidate(tier="org", rank=5, minutes=525)])

    assert _resolve(p, org=4100).config_version_id == "cfg-A"
    assert _resolve(p, org=4200).config_version_id == "cfg-B"


def test_one_organizations_config_cannot_serve_another():
    p = FakeProvider(versions={4100: [(date(2026, 1, 1), "cfg-A")]},
                     candidates=[RequirementCandidate(tier="org", rank=5, minutes=525)])
    assert _resolve(p, org=4999).outcome is ResolutionOutcome.NO_VERSION


def test_the_organization_id_is_passed_through_to_the_provider():
    p = FakeProvider()
    _resolve(p, org=4321)
    assert p.calls[0][1] == 4321


def test_resolution_is_per_worker():
    p = FakeProvider(candidates_by_user={
        901: [RequirementCandidate(tier="employee", rank=3, minutes=240)],
        902: [RequirementCandidate(tier="employee", rank=3, minutes=525)],
    })
    assert _resolve(p, user=901).required_minutes == 240
    assert _resolve(p, user=902).required_minutes == 525


def test_the_result_carries_its_identity():
    r = _resolve(FakeProvider(candidates=[
        RequirementCandidate(tier="org", rank=5, minutes=525)]), org=4100, user=907)
    assert (r.organization_id, r.user_id, r.as_of) == (4100, 907, D)


# ══════════════════════════════════════════════════════════════════════════════
# 8. Fail-open clock-out vs fail-closed evaluation — not collapsed
# ══════════════════════════════════════════════════════════════════════════════

def test_the_clock_out_path_resolver_is_untouched_and_still_fail_open():
    """D-10: clock-out stays non-blocking. Step 5 added a sibling; it did not
    change the function the clock-out path calls."""
    import app.attendance_policy.resolvers as legacy
    src = inspect.getsource(legacy.resolve_expected_work_seconds)
    assert "Always returns a positive int; never raises" in src
    assert "requirement_resolution" not in inspect.getsource(legacy)


def test_the_legacy_resolver_still_terminates_in_a_default():
    """Pinned deliberately: that default is what keeps clock-out non-blocking,
    and Step 5 must not have removed it."""
    import app.attendance_policy.resolvers as legacy
    assert legacy.DEFAULT_EXPECTED_WORK_SECONDS == 28800
    assert "return DEFAULT_EXPECTED_WORK_SECONDS" in inspect.getsource(legacy)


def test_the_strict_resolver_shares_no_default_with_the_legacy_one():
    """The two paths must not converge on the same fallback."""
    import app.requirement_resolution.resolver as strict
    src = inspect.getsource(strict)
    assert "DEFAULT_EXPECTED_WORK_SECONDS" not in src
    assert "28800" not in src


def test_unresolved_cannot_produce_a_shortfall():
    """There is no number to subtract from, by construction."""
    r = _resolve(FakeProvider(candidates=[]))
    assert r.required_minutes is None
    assert r.is_evaluable is False


def test_neither_non_resolved_outcome_can_produce_a_threshold_decision():
    """A threshold decision needs an attributable shortfall, which needs a
    requirement. Neither non-resolved outcome supplies one."""
    unresolved = _resolve(FakeProvider(candidates=[]))
    no_version = _resolve(_versioned_provider(), on=date(2025, 1, 1))

    assert unresolved.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
    assert no_version.outcome is ResolutionOutcome.NO_VERSION
    for r in (unresolved, no_version):
        assert r.is_evaluable is False
        assert r.required_minutes is None


# ══════════════════════════════════════════════════════════════════════════════
# 9. Architectural guards
# ══════════════════════════════════════════════════════════════════════════════

def _code_identifiers(mod) -> set:
    import ast
    tree = ast.parse(inspect.getsource(mod))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.update(a.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_no_worked_time_calculation_was_introduced():
    from app.requirement_resolution import resolver, outcomes, ports
    for mod in (resolver, outcomes, ports):
        ids = _code_identifiers(mod)
        for forbidden in ("session_worked_seconds", "_worked", "worked_seconds",
                          "total_seconds", "break_seconds", "offline_seconds"):
            assert forbidden not in ids, f"{mod.__name__} references {forbidden!r}"


def test_no_activity_or_screenshot_dependency():
    from app.requirement_resolution import resolver, outcomes, ports
    for mod in (resolver, outcomes, ports):
        ids = _code_identifiers(mod)
        for forbidden in ("active_time", "Activity", "ActivityWindow",
                          "BrowserActivity", "AppSession", "Screenshot"):
            assert forbidden not in ids


def test_no_dependency_on_the_legacy_session_aggregation():
    from app.requirement_resolution import resolver
    ids = _code_identifiers(resolver)
    for forbidden in ("ClockSession", "BreakSession", "OfflineInterval",
                      "resolve_expected_work_seconds"):
        assert forbidden not in ids


def test_step_5_did_not_wire_worked_py_to_anything():
    import pathlib
    backend = pathlib.Path(__file__).resolve().parent.parent
    worked = (backend / "app" / "_worked.py").read_text(encoding="utf-8")
    assert "requirement_resolution" not in worked
    assert "work_ledger" not in worked


def test_step_5_did_not_re_enable_eco_v1():
    import pathlib
    backend = pathlib.Path(__file__).resolve().parent.parent
    router = (backend / "app" / "employees" / "router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
