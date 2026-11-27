"""Phase 3.9 — Golden entitlement matrix + drift baseline (SPEC.txt §20, §21.1 step 9).

An exhaustive, committed snapshot of the resolver's observable output across every
plan x lifecycle-state x role, for all 38 capabilities: the {decision, presentation}
each combination produces today. The test regenerates the grid from the REAL
resolver and projection and fails with a readable per-cell diff on any drift.

This is an observation of the current system, not a second entitlement engine. It
calls ``entitlements.get`` and ``projection.build_actor_projection`` — it never
recomputes a decision or re-derives a presentation. It captures only
{decision, presentation}; the non-deterministic fields (computed_at, snapshot_hash,
tenant_id) are deliberately excluded so the snapshot is stable.

Regenerating the snapshot is a deliberate developer action, never automatic:

    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_entitlement_matrix.py

A normal run only compares against the committed file. When a Product decision
intentionally changes a grant, regenerate and review the diff — that diff is the
blast-radius report for the change.
"""
import json
import os
import pathlib

import pytest

from app.commercial import entitlements, ids, manifests, projection
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

GOLDEN_PATH = pathlib.Path(__file__).resolve().parent / "golden" / "entitlement_matrix.json"

# ── Canonical dimensions, read from the implementation (never invented) ──────
# Grid A: a subscribed tenant can carry any of these commercial states. These are
# exactly the lifecycle states the resolver distinguishes for a subscribed org.
SUBSCRIBED_STATES = ("trial_active", "active", "past_due",
                     "suspended", "cancelled", "closed")
SUBSCRIBED_PLANS = manifests.PLANS                      # verified/governed/sovereign/enterprise

# Grid B: special profiles that are NOT a plan x state cross-product.
#   internal  — the vendor tenant: billing_type=internal, no subscription.
#   unresolved — no subscription at all -> _empty_profile. This is the real
#                production `manual_review` (Phase 3.2 backfill) and the
#                fail-closed case for an unknown tenant; both resolve identically.
SPECIAL_PROFILES = ("internal", "unresolved")

# Roles: worker vs admin-class. Names are the actual operational vocabulary.
ROLES = ("admin", "worker")
ROLE_VALUE = {"admin": "admin", "worker": "employee"}


# ── Faithful builders: real rows, real resolver ─────────────────────────────
def _subscribed_org(db, *, plan, state):
    op = models.Organization(name=f"golden {ids.org_id()[:8]}", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state=state,
        operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
        billing_cycle="monthly", seat_count=10, state=state, provider="stub"))
    db.flush()
    return org_id


def _internal_org(db):
    op = models.Organization(name=f"golden internal {ids.org_id()[:6]}",
                             billing_type="internal")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state="active",
        operational_org_id=op.id))          # deliberately no subscription
    db.flush()
    return org_id


def _capabilities_by_role(db, org_id):
    """{role: {capability: {decision, presentation}}} for one resolved tenant."""
    profile = entitlements.get(db, org_id)
    out = {}
    for role in ROLES:
        projected = projection.build_actor_projection(profile, role=ROLE_VALUE[role])
        out[role] = {
            row["capability"]: {
                "decision": row["decision"],
                "presentation": row["presentation"],
            }
            for row in projected["capabilities"]
        }
    return out


def build_matrix(db):
    """The full grid, deterministically ordered. Calls the real resolver only."""
    matrix = {}

    # Grid A — subscribed plan x state.
    for plan in SUBSCRIBED_PLANS:
        for state in SUBSCRIBED_STATES:
            org_id = _subscribed_org(db, plan=plan, state=state)
            matrix[f"{plan}|{state}"] = _capabilities_by_role(db, org_id)

    # Grid B — special profiles.
    internal_id = _internal_org(db)
    matrix["internal|active"] = _capabilities_by_role(db, internal_id)

    # Unresolved: no such org -> _empty_profile (the real production manual_review).
    unresolved = entitlements.compute(db, "org_does_not_exist_golden")
    matrix["unresolved|manual_review"] = {
        role: {
            key: {
                "decision": (unresolved["capabilities"] or {}).get(key, manifests.DENY),
                "presentation": projection.build_actor_projection(
                    unresolved, role=ROLE_VALUE[role])["capabilities"][i]["presentation"],
            }
            for i, key in enumerate(sorted(manifests.FEATURES))
        }
        for role in ROLES
    }
    return matrix


def _canonical(matrix):
    """Stable JSON string: sorted keys, fixed indentation."""
    return json.dumps(matrix, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _flatten(matrix):
    """{(row, role, capability): (decision, presentation)} for diffing."""
    flat = {}
    for row, by_role in matrix.items():
        for role, caps in by_role.items():
            for cap, cell in caps.items():
                flat[(row, role, cap)] = (cell["decision"], cell["presentation"])
    return flat


def _diff_report(expected, actual):
    """Human-readable blast-radius diff between two matrices."""
    exp, act = _flatten(expected), _flatten(actual)
    lines = []
    for cellkey in sorted(set(exp) | set(act)):
        e = exp.get(cellkey)
        a = act.get(cellkey)
        if e == a:
            continue
        row, role, cap = cellkey
        plan, _, state = row.partition("|")
        if e is None:
            lines.append(f"  + NEW  plan={plan} state={state} role={role} cap={cap} "
                         f"-> decision={a[0]} presentation={a[1]}")
        elif a is None:
            lines.append(f"  - GONE plan={plan} state={state} role={role} cap={cap} "
                         f"(was decision={e[0]} presentation={e[1]})")
        else:
            parts = []
            if e[0] != a[0]:
                parts.append(f"decision: {e[0]} -> {a[0]}")
            if e[1] != a[1]:
                parts.append(f"presentation: {e[1]} -> {a[1]}")
            lines.append(f"  ~ plan={plan} state={state} role={role} cap={cap} | "
                         + "; ".join(parts))
    return "\n".join(lines)


# ── Tests ────────────────────────────────────────────────────────────────────
def test_golden_entitlement_matrix(db):
    """Regenerate the grid from the live resolver and compare to the snapshot.

    With GOLDEN_UPDATE=1 the snapshot is rewritten instead of asserted — the only
    way it ever changes. A normal run is pure comparison.
    """
    current = build_matrix(db)

    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(_canonical(current), encoding="utf-8")
        pytest.skip("GOLDEN_UPDATE=1 — snapshot regenerated, not asserted")

    assert GOLDEN_PATH.exists(), (
        f"golden snapshot missing at {GOLDEN_PATH}. "
        f"Generate it once with GOLDEN_UPDATE=1.")

    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if expected != current:
        report = _diff_report(expected, current)
        raise AssertionError(
            "Entitlement matrix drifted from the committed golden snapshot.\n"
            "If this change is intentional (a Product decision), review the diff "
            "below then regenerate with GOLDEN_UPDATE=1.\n\n" + report)


def test_matrix_shape_is_exhaustive(db):
    """The grid covers every plan x state x role x capability, with no gaps."""
    matrix = build_matrix(db)

    expected_rows = {f"{p}|{s}" for p in SUBSCRIBED_PLANS for s in SUBSCRIBED_STATES}
    expected_rows |= {"internal|active", "unresolved|manual_review"}
    assert set(matrix) == expected_rows

    for row, by_role in matrix.items():
        assert set(by_role) == set(ROLES), row
        for role, caps in by_role.items():
            assert set(caps) == set(manifests.FEATURES), (row, role)
            for cell in caps.values():
                assert cell["decision"] in manifests.DECISIONS
                assert cell["presentation"] in manifests.PRESENTATIONS


def test_matrix_cell_count(db):
    """Documented cell count: Grid A (4x6x2x38) + Grid B (2x2x38)."""
    matrix = build_matrix(db)
    cells = sum(len(caps) for by_role in matrix.values() for caps in by_role.values())
    grid_a = len(SUBSCRIBED_PLANS) * len(SUBSCRIBED_STATES) * len(ROLES) * len(manifests.FEATURES)
    grid_b = len(SPECIAL_PROFILES) * len(ROLES) * len(manifests.FEATURES)
    assert cells == grid_a + grid_b == 1976


def test_presentation_never_broader_than_decision_across_the_grid(db):
    """A safety invariant re-checked over the whole grid: only ALLOW shows, and a
    denied capability is never SHOW. This is what makes the presentation column
    safe to enforce against later."""
    matrix = build_matrix(db)
    for row, by_role in matrix.items():
        for role, caps in by_role.items():
            for cap, cell in caps.items():
                if cell["presentation"] == manifests.SHOW:
                    assert cell["decision"] == manifests.ALLOW, (row, role, cap)
                if cell["decision"] == manifests.DENY:
                    assert cell["presentation"] in (manifests.LOCK, manifests.HIDE), (row, role, cap)


def test_privacy_denied_everywhere_in_the_grid(db):
    """The one rule that must never have a hole: globally denied keys are DENY and
    HIDE in every cell, every plan, every state, every role."""
    matrix = build_matrix(db)
    for row, by_role in matrix.items():
        for role, caps in by_role.items():
            for key in manifests.GLOBALLY_DENIED:
                assert caps[key]["decision"] == manifests.DENY, (row, role, key)
                assert caps[key]["presentation"] == manifests.HIDE, (row, role, key)
