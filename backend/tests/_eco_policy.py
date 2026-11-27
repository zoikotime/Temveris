"""
Shared PolicyVersion fixture helper for ECO v2 evaluation tests.
================================================================

``evaluate_and_record_day`` requires an applicable historical PolicyVersion and
refuses with ``POLICY_UNRESOLVED`` when none exists — a production fail-closed
rule the owner set explicitly, and one no test may weaken.

So every suite that records a day must CAPTURE a policy, exactly as a real
Tenant would. This helper exists so that capture is one explicit call per suite
instead of twelve divergent copies, and so that a future change to the required
attributes lands in one place.

WHY THE VALUES ARE WHAT THEY ARE
--------------------------------
``netting_mode="NONE"`` and ``grace_minutes=0`` are the specification's own
defaults (§15 recommends NONE; §11 gives grace a default of 0). They are chosen
EXPLICITLY here rather than assumed: §15's "DO NOT SHIP `NONE` AS AN ASSUMPTION"
is a rule about what a Tenant is silently given at onboarding, not about what a
test may state outright.

``grace_minutes=0`` is no longer inert: MODEL-GRACE applies it, and a non-zero
value here would zero the attributable shortfall of every small-gap day in
every suite that uses this helper. Zero is chosen because it is §11's own
default and because it makes the grace comparison a no-op, leaving each suite
testing what it was written to test. A suite that wants grace to BITE passes
its own value — ``test_gv24_execution.py`` does exactly that.

Netting, break treatment and rounding profiles still have no behaviour; those
values exist to satisfy the seam.

``effective_from`` defaults to well before every date the ECO suites use, so one
call covers a whole suite regardless of which work dates it exercises. A suite
that needs to test the historical floor should pass its own date instead.
"""

from __future__ import annotations

from datetime import date

DEFAULT_EFFECTIVE_FROM = date(2020, 1, 1)
DEFAULT_POLICY_KEY = "test-policy-v1"
#: Named, because MODEL-GRACE made it load-bearing: a test that reproduces a
#: boundary-written evaluation by calling ``evaluate_day`` directly must pass
#: the SAME grace, or it computes a different result and the two content
#: hashes disagree. One constant so they cannot drift apart.
DEFAULT_GRACE_MINUTES = 0


def capture_test_policy(
    db,
    *,
    organization_id: int,
    policy_version: str = DEFAULT_POLICY_KEY,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    counts_toward_week: bool = True,
    netting_mode: str = "NONE",
    netting_decision_provenance: str = "EXPLICIT_TENANT_CHOICE",
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    captured_by: str = "fixture@test",
    commit: bool = True,
):
    """Capture one explicit PolicyVersion for a test organization.

    Returns the row. Commits by default, because most suites call this from a
    fixture that has already committed its organization.
    """
    from app.policy_version import capture_policy_version

    row = capture_policy_version(
        db,
        organization_id=organization_id,
        # Owner ruling D5 / OD-02: A. A new capture must record HOW the mode
        # was selected; EXPLICIT_TENANT_CHOICE is the only thing a capture may
        # claim, so the fixture says what a real onboarding would.
        netting_decision_provenance=netting_decision_provenance,
        policy_version=policy_version,
        effective_from=effective_from,
        counts_toward_week=counts_toward_week,
        netting_mode=netting_mode,
        grace_minutes=grace_minutes,
        captured_by=captured_by,
    )
    if commit:
        db.commit()
    return row


def ensure_test_policy(db, *, organization_id: int, **kw):
    """Capture a policy only if the organization has none.

    Suites that share a session-scoped engine keep rows between tests, so a
    plain capture would collide on the second one. Idempotent by existence
    rather than by catching the collision: the collision is a real refusal and
    swallowing it would hide a genuine double-capture.
    """
    from app.policy_version import PolicyVersion

    existing = (db.query(PolicyVersion)
                .filter(PolicyVersion.organization_id == organization_id)
                .first())
    if existing is not None:
        return existing
    return capture_test_policy(db, organization_id=organization_id, **kw)


__all__ = ["capture_test_policy", "ensure_test_policy",
           "DEFAULT_EFFECTIVE_FROM", "DEFAULT_POLICY_KEY",
           "DEFAULT_GRACE_MINUTES"]
