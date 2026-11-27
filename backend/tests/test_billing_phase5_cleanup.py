"""Phase 5 — four billing statements that were untrue.

Each of these told the customer something the system could not back up:

1. Two annual Stripe prices were 20%-discounted derivations ($76.80, $144) while the
   UI quoted the canonical catalog ($79, $149) — so Stripe would have charged less
   than the price shown. Stripe prices are immutable, so the fix is a new price plus
   archiving the old one.
2. "N seats purchased" showed BillingPlan.user_limit — plan *capacity*, not a
   purchased quantity. Billing is per active user. The same variable also drove a
   fallback amount of price x capacity, overstating a Governed bill as
   100 x $15 = $1,500 instead of 35 x $15 = $525.
3. The Entitlements tab preferred the commercial capability profile over the
   authoritative billing plan. commercial_subscriptions is empty, so that profile is
   always _empty_profile(plan="verified", state="manual_review") — every feature
   false, every limit zero. A paying Governed customer was shown as Verified with
   nothing enabled.
4. The downgrade endpoint promised "a billing specialist will review it within one
   business day". Nothing reads BillingDowngradeRequest — not a job, not a screen.

app/commercial is deliberately untouched, so item 3 is fixed at the presentation
layer: billing is treated as authoritative and a contradicting profile is shown as
unprovisioned rather than as a definitive list of zeroes.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import router as billing_router
from app.billing import service as billing_service
from app.billing import worker_state
from database.database import get_db
from database.models import BillingPlan, BillingSubscription, Organization, User

API = "/api/admin/billing"
ORG = 7901

_JSX = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend/src/components/BillingModule.jsx")


def _jsx(strip_comments: bool = False) -> str:
    src = _JSX.read_text(encoding="utf-8")
    if strip_comments:
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    return src


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id == ORG).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id == ORG).delete(synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG, name="Cleanup Client", billing_type="customer"))
    gov = BillingPlan(code="governed", name="Governed", tier_order=2,
                      monthly_price_per_user=15.0, annual_price_per_user=144.0,
                      user_limit=100)
    ver = BillingPlan(code="verified", name="Verified", tier_order=1,
                      monthly_price_per_user=8.0, annual_price_per_user=76.80,
                      user_limit=25)
    db.add_all([gov, ver])
    db.flush()
    db.add(BillingSubscription(organization_id=ORG, plan_id=gov.id, billing_cycle="monthly",
                              subscription_status="active", active_users=3, currency="USD"))
    for i in range(3):
        db.add(User(name=f"c{i}", email=f"c{i}@cleanup.test", organization_id=ORG,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    yield gov, ver
    _clean()


def _client(db, actor):
    from app.auth.service import get_current_user
    app = FastAPI()
    app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _admin(db, email="cleanup-admin@cleanup.test"):
    u = User(name="Admin", email=email, organization_id=ORG, role="admin",
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ══════════════════════════════════════════════════════════════════════════════
# Item 1 — annual prices
# ══════════════════════════════════════════════════════════════════════════════
# Reversed on 2026-08-21. This file previously pinned Verified $79 / Governed $149 and
# asserted that $76.80 / $144.00 must NOT return, on the reasoning that $79 and $149
# looked like deliberate round-number list prices rather than a formula.
#
# That reasoning had a hole: it never checked the pricing page. temveris.example/pricing
# advertises annual as $6.40 / $12.00 / $20.00 per user per month with a flat "Save 20%"
# badge — which is exactly monthly x 12 x 0.8, i.e. $76.80 / $144.00 / $240.00. At $79
# and $149 a customer was quoted one number and charged a higher one: $2.20 and $5.00
# per user per year, or $1,250/yr on a 250-seat Governed account.
#
# The published price is the one a customer relies on before buying, so it wins. This
# also makes "Save 20%" literally true on all three plans for the first time.
CANONICAL = {"verified": (8.0, 76.80), "governed": (15.0, 144.0), "sovereign": (25.0, 240.0)}


def test_seed_catalog_matches_the_published_prices():
    seeded_by_code = {p["code"]: p for p in billing_service.SEED_PLANS}
    for code, (monthly, annual) in CANONICAL.items():
        assert seeded_by_code[code]["monthly_price_per_user"] == monthly
        assert seeded_by_code[code]["annual_price_per_user"] == annual


def test_annual_is_exactly_twenty_percent_off_on_every_plan():
    """The badge says "Save 20%". It has to be true, not approximately true.

    Previously verified was 17.71% and governed 17.22% off while the page claimed 20%.
    Asserting the arithmetic means a future round-number edit cannot silently make the
    advertised discount a lie again.
    """
    for plan in billing_service.SEED_PLANS:
        if plan.get("is_enterprise"):
            continue
        monthly = plan["monthly_price_per_user"]
        annual = plan["annual_price_per_user"]
        assert annual == round(monthly * 12 * 0.8, 2), plan["code"]


def test_the_overcharging_annual_prices_do_not_return():
    """$79 and $149 charged more than the pricing page advertises. Pinned out."""
    seeded_by_code = {p["code"]: p for p in billing_service.SEED_PLANS}
    assert seeded_by_code["verified"]["annual_price_per_user"] != 79.0
    assert seeded_by_code["governed"]["annual_price_per_user"] != 149.0


@pytest.mark.skipif(not (os.getenv("STRIPE_SECRET_KEY") or "").startswith("sk_test"),
                    reason="requires a Stripe TEST key")
def test_configured_stripe_prices_match_the_canonical_catalog():
    """Guards the real wiring: the price id in the environment must charge the amount
    the UI quotes. This is what was broken — catalog $149, Stripe $144."""
    import stripe
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    stripe.max_network_retries = 2
    mismatches = []
    for code, (monthly, annual) in CANONICAL.items():
        for cycle, want in (("MONTHLY", monthly), ("ANNUAL", annual)):
            pid = os.getenv(f"STRIPE_PRICE_{code.upper()}_{cycle}")
            if not pid:
                mismatches.append(f"{code}/{cycle}: not configured")
                continue
            p = stripe.Price.retrieve(pid)
            amt = (p["unit_amount"] or 0) / 100
            if abs(amt - want) > 0.005:
                mismatches.append(f"{code}/{cycle}: Stripe ${amt} != catalog ${want}")
            if not p["active"]:
                mismatches.append(f"{code}/{cycle}: price {pid} is archived")
    assert not mismatches, "; ".join(mismatches)


# ══════════════════════════════════════════════════════════════════════════════
# Item 2 — seat labelling
# ══════════════════════════════════════════════════════════════════════════════
def test_seat_metrics_included_is_plan_capacity_not_a_purchase(db, seeded):
    """Pins the meaning the label has to describe: `included` is the plan's user_limit
    and is unrelated to how many seats are billed."""
    actor = _admin(db)
    with _client(db, actor) as c:
        body = c.get(f"{API}/summary").json()
    sm = body["seat_metrics"]
    assert sm["included"] == 100          # this fixture's plan row, not the catalogue
    assert sm["active"] == 4              # 3 employees + this admin — what Stripe bills
    assert sm["included"] != sm["active"]


def test_billing_subscription_has_no_seat_count_column():
    """The old label read `sub.seat_count`, which never resolved — only
    app/commercial's models define seat_count."""
    assert "seat_count" not in BillingSubscription.__table__.columns


def test_frontend_no_longer_claims_seats_were_purchased():
    """Phase 5 renamed the label; the later UI simplification removed the concept.

    The original assertion required ``seatsIncluded`` to exist, because Phase 5's fix was
    a *rename* of ``seatsPurchased``. The billing UI has since dropped plan-capacity
    display from the customer view entirely — a strictly stronger outcome — so the
    variable is gone rather than renamed. What must remain true is that no
    seat-purchasing language survives, and that the page states what is actually
    billed.
    """
    src = _jsx(strip_comments=True)
    for banned in ("seats purchased", "seatsPurchased", "billingSeatsPurchased",
                   "Seat Usage", "Add seats", "Seats available"):
        assert banned not in src, f"seat-purchasing language survived: {banned!r}"
    # Billing is per active employee, and the page must say so.
    assert "Active Employees" in src or "Active employees" in src
    assert "active employees" in src


def test_frontend_fallback_amount_uses_billable_seats_not_capacity():
    """price x capacity overstated a Governed bill as $1,500 instead of $525."""
    src = _jsx(strip_comments=True)
    block = src[src.index("{nextInvoice"):]
    block = block[:block.index("</p>")]
    assert "* active," in block, "fallback must multiply by billable seats"
    assert "seatsIncluded" not in block, "fallback must not multiply by plan capacity"


# ══════════════════════════════════════════════════════════════════════════════
# Item 3 — entitlements split-brain
# ══════════════════════════════════════════════════════════════════════════════
def test_summary_reports_the_authoritative_plan(db, seeded):
    """Billing is the source of truth for which plan a tenant is on — it is what
    Stripe charges against."""
    actor = _admin(db, "ent-admin@cleanup.test")
    with _client(db, actor) as c:
        body = c.get(f"{API}/summary").json()
    assert body["subscription"]["plan"]["code"] == "governed"


def test_frontend_prefers_billing_plan_over_commercial_profile():
    src = _jsx(strip_comments=True)
    line = next(l for l in src.splitlines() if "const planLabel" in l)
    # currentPlan (billing) must be consulted before the commercial profile.
    assert line.index("currentPlan") < line.index("ent.plan_name"), (
        "planLabel must prefer the authoritative billing plan"
    )


def test_frontend_detects_a_contradicting_or_fallback_profile():
    src = _jsx(strip_comments=True)
    assert "entProfileStale" in src
    assert "manual_review_required" in src
    # A stale profile must suppress the capability list rather than render zeroes.
    hasdata = next(l for l in src.splitlines() if "const hasData" in l)
    assert "!entProfileStale" in hasdata


def test_frontend_names_the_active_plan_when_profile_is_missing():
    """So an unprovisioned profile never reads as a downgrade."""
    src = _jsx(strip_comments=True)
    assert "billingEntActivePlan" in src


def test_billing_does_not_depend_on_the_commercial_layer():
    """Item 3 is fixed at the presentation layer, not by coupling to app/commercial.

    This used to shell out to ``git status`` and assert app/commercial had no
    modifications. That tested the developer's working tree rather than the code, so it
    failed the moment anyone else legitimately changed app/commercial — which is exactly
    what happened when the self-service signup work landed. Whether a *different* team
    edits that package is not this suite's business.

    The durable invariant is the one that actually matters: nothing under app/billing
    may import app.commercial. The two billing systems must stay independent, so the
    inert commercial layer can be changed or removed without touching live billing.
    """
    offenders = []
    for path in pathlib.Path(__file__).resolve().parents[1].joinpath("app/billing").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "app.commercial" in stripped or "from app import commercial" in stripped:
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, (
        "app/billing must not import app/commercial — the two billing systems stay "
        f"independent: {offenders}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Item 4 — the false downgrade SLA
# ══════════════════════════════════════════════════════════════════════════════
def test_downgrade_response_promises_no_review_sla(db, seeded):
    gov, ver = seeded
    actor = _admin(db, "down-admin@cleanup.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/downgrade-request",
                   json={"requested_plan_code": "verified",
                         "reason": "cost reduction for the next quarter",
                         "acknowledged_risk": True})
    assert r.status_code == 200, r.text
    msg = r.json()["message"].lower()
    assert "business day" not in msg
    assert "specialist" not in msg
    assert "recorded" in msg


def test_downgrade_request_is_still_recorded(db, seeded):
    """Removing the promise must not remove the record."""
    from database.models import BillingDowngradeRequest
    actor = _admin(db, "down-keep@cleanup.test")
    with _client(db, actor) as c:
        c.post(f"{API}/downgrade-request",
               json={"requested_plan_code": "verified", "reason": "testing the record",
                     "acknowledged_risk": True})
    rows = db.query(BillingDowngradeRequest).filter_by(organization_id=ORG).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_downgrade_endpoint_makes_no_sla_promise():
    """Scoped to the downgrade response, which is what item 4 covered.

    KNOWN, NOT FIXED HERE: /enterprise-quote still returns "We've received your
    request and will respond within one business day." BillingEnterpriseQuote is also
    write-only — created in the endpoint, read by no code, and no email is sent. It is
    the same defect, but production has 0 rows so nobody has been let down yet, and
    widening item 4's scope was not authorised. Reported for a decision.
    """
    src = pathlib.Path(billing_router.__file__).read_text(encoding="utf-8")
    code_only = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    downgrade = code_only[code_only.index("def request_downgrade"):]
    downgrade = downgrade[:downgrade.index("@router.")]
    assert "within one business day" not in downgrade
    assert "billing specialist" not in downgrade


def test_frontend_downgrade_copy_promises_no_review_sla():
    src = _jsx(strip_comments=True)
    assert "A billing specialist will review before changes apply" not in src
    assert "not automatic" in src
