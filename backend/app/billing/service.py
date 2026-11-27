"""Temveris — Billing Service.

Bootstraps the launch plan catalog (Verified / Governed / Sovereign /
Enterprise) on first request, ensures the org has a subscription row, and
provides shared helpers used by the billing router.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session

from database import models
from app.billing import worker_state


_log = logging.getLogger("zoiko.billing.service")


class StripePlanChangeError(Exception):
    """A plan change could not be applied at Stripe.

    Carries the HTTP status the router should surface. Raised *before* any local
    write, so the caller can be certain the subscription row is untouched.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# Stripe subscription states that can still be modified in place. Anything else
# (canceled, incomplete_expired, unpaid) needs a fresh checkout, and we say so
# rather than quietly creating a second subscription.
MODIFIABLE_STRIPE_STATES = frozenset({"active", "trialing", "past_due"})


class MissingOrganization(Exception):
    """A billing operation was attempted without an organization.

    ZT-COM-BILL-001 §4 — billing is always tenant-scoped. The former
    ``DEFAULT_ORG_ID = "default"`` sentinel made every tenant share one
    subscription row, so one organization was billed for every user on the
    platform while the others could not be billed at all. There is deliberately
    no fallback organization: callers must supply a real one.
    """


def require_org_id(org_id: Optional[int]) -> int:
    """Validate that a caller supplied a real organization id."""
    if org_id is None:
        raise MissingOrganization(
            "billing requires an organization_id — there is no default organization"
        )
    return int(org_id)


# ── Internal vs customer tenants ─────────────────────────────────────────────
# Temveris's owner runs the product it sells: it needs full plan entitlements but
# must never be charged. Before this existed, that safety was implicit — the Stripe
# write paths only refuse when stripe_subscription_id/stripe_customer_id are NULL,
# and organization 1 has both populated. One click of "Upgrade" would have modified
# a live subscription.
BILLING_TYPE_INTERNAL = "internal"
BILLING_TYPE_CUSTOMER = "customer"

INTERNAL_TENANT_DETAIL = (
    "This organization is an internal Temveris tenant and is not billed. "
    "No payment method, subscription or plan purchase applies to it."
)


class InternalTenantBilling(Exception):
    """A Stripe-charging operation was attempted for a non-billable tenant.

    Raised rather than returned so a new caller that forgets the guard fails loudly
    instead of silently charging the platform owner.
    """


def is_internal_tenant(db: Session, org_id: Optional[int]) -> bool:
    """True when this organization must never reach a Stripe write path.

    Fails *closed* for a missing organization: if the row cannot be found we cannot
    prove the tenant is billable, so it is treated as internal. A caller that
    charges an unknown tenant is a worse outcome than one that refuses.
    """
    if org_id is None:
        return True
    org = (
        db.query(models.Organization)
          .filter(models.Organization.id == int(org_id))
          .first()
    )
    if org is None:
        _log.error("billing: organization %s not found — treating as non-billable", org_id)
        return True
    return (org.billing_type or BILLING_TYPE_CUSTOMER).strip().lower() == BILLING_TYPE_INTERNAL


class PlanCapacityExceeded(Exception):
    """An operation would leave an organization above its plan's seat ceiling.

    ``BillingPlan.user_limit`` used to be advisory: it only coloured an alert in
    /summary. Nothing stopped an organization exceeding it, and nothing stopped a plan
    change onto a tier whose limit was far below the current headcount — Stripe would
    then bill 57 seats on a plan sold as 25.
    """

    def __init__(self, message: str, *, billable: int, limit: int):
        super().__init__(message)
        self.billable = billable
        self.limit = limit


def plan_capacity_shortfall(db: Session, org_id: Optional[int],
                            plan: Optional[models.BillingPlan],
                            *, extra_seats: int = 0) -> Optional[Tuple[int, int]]:
    """(billable, limit) when the plan cannot hold this org, else None.

    ``extra_seats`` asks the question prospectively — "could we activate one more
    person?" — so callers can refuse *before* creating the over-capacity state.
    A plan with ``user_limit`` NULL is unlimited.
    """
    if plan is None or plan.user_limit is None or org_id is None:
        return None
    billable = count_billable_seats(db, org_id) + max(0, extra_seats)
    limit = int(plan.user_limit)
    return (billable, limit) if billable > limit else None


def smallest_plan_holding(db: Session, headcount: int,
                          *, above_tier: Optional[int] = None) -> Optional[models.BillingPlan]:
    """The cheapest active plan that can hold ``headcount``, or None.

    Used to name the specific plan an administrator needs instead of telling them to
    "choose a larger plan" and leaving them to work out which. Enterprise is excluded:
    it is quote-only and cannot be self-served, so offering it as the answer to "I need
    one more seat" would be a dead end.
    """
    candidates = (
        db.query(models.BillingPlan)
          .filter(models.BillingPlan.active_status == True,          # noqa: E712
                  models.BillingPlan.is_enterprise == False)         # noqa: E712
          .order_by(models.BillingPlan.tier_order)
          .all()
    )
    for candidate in candidates:
        if above_tier is not None and (candidate.tier_order or 0) <= above_tier:
            continue
        if candidate.user_limit is None or int(candidate.user_limit) >= headcount:
            return candidate
    return None


def assert_plan_capacity(db: Session, org_id: Optional[int],
                         plan: Optional[models.BillingPlan],
                         *, extra_seats: int = 0) -> None:
    """Raise PlanCapacityExceeded when the plan cannot hold this organization."""
    shortfall = plan_capacity_shortfall(db, org_id, plan, extra_seats=extra_seats)
    if shortfall is None:
        return
    billable, limit = shortfall

    # Name the plan that actually solves it. "Choose a larger plan" makes the admin go
    # and work out which one holds 26 people; saying "Governed holds up to 250" is the
    # same sentence with the answer in it.
    nxt = smallest_plan_holding(db, billable, above_tier=plan.tier_order)
    if nxt is not None:
        room = "unlimited users" if nxt.user_limit is None else f"up to {nxt.user_limit} users"
        remedy = f"{nxt.name} covers {room} — upgrade to add this person."
    else:
        remedy = ("No self-serve plan covers this many users — contact sales about an "
                  "Enterprise agreement.")

    raise PlanCapacityExceeded(
        f"{plan.name} allows up to {limit} billable users, and this would make "
        f"{billable}. {remedy}",
        billable=billable, limit=limit,
    )


def plan_limit_options(db: Session, *, requested_seats: int,
                       current_plan: Optional[models.BillingPlan]) -> dict:
    """Everything the UI needs to explain a seat count a plan cannot hold.

    Built from the plan catalog rather than from constants so the limit quoted
    to the customer, the limit enforced at checkout and the limit stored on the
    plan are the same number. `smallest_plan_holding` already answers "which
    plan solves this"; this wraps it with the pricing the upgrade card shows and
    the enterprise fallback for headcounts no self-serve plan covers.

    Enterprise is always offered alongside, because "no self-serve plan fits"
    and "a bigger self-serve plan fits but you may still want to talk to us" are
    both real answers at 300 seats.
    """
    upgrade = smallest_plan_holding(
        db, requested_seats,
        above_tier=current_plan.tier_order if current_plan is not None else None,
    )
    enterprise = (
        db.query(models.BillingPlan)
          .filter(models.BillingPlan.is_enterprise.is_(True),
                  models.BillingPlan.active_status.is_(True))
          .first()
    )

    def _card(plan):
        if plan is None:
            return None
        return {
            "code": plan.code,
            "name": plan.name,
            "badge": plan.badge,
            "short_description": plan.short_description,
            "monthly_price_per_user": plan.monthly_price_per_user,
            "annual_price_per_user": plan.annual_price_per_user,
            "user_limit": plan.user_limit,
            "is_enterprise": bool(plan.is_enterprise),
            "currency": plan.currency or "USD",
        }

    return {
        "requested_seats": requested_seats,
        "current_plan": _card(current_plan),
        "upgrade_plan": _card(upgrade),
        "enterprise_plan": _card(enterprise),
    }


def assert_room_for_new_worker(db: Session, org_id: Optional[int], *,
                               include_reserved: bool = True) -> None:
    """Refuse *before* creating a worker the plan cannot hold.

    Checked at the point an administrator adds or invites someone, because that is the
    person who can resolve it by upgrading. The alternative — discovering the ceiling
    when an invited employee later tries to set their password — fails in front of
    someone with no way to fix it.

    ``include_reserved`` counts pending invitations as well as billable workers. An
    invitation reserves plan capacity even though it is never charged
    (ZT-COM-BILL-001 §H3), so without this an organization could queue unlimited
    invitations and sail past its ceiling as they were accepted.
    """
    if org_id is None:
        return
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        return
    plan = (db.query(models.BillingPlan)
              .filter(models.BillingPlan.id == sub.plan_id).first())
    if plan is None or plan.user_limit is None:
        return

    reserved = count_reserved_seats(db, org_id) if include_reserved else 0
    # +1 for the worker about to be created.
    assert_plan_capacity(db, org_id, plan, extra_seats=reserved + 1)


def require_billable_tenant(db: Session, org_id: Optional[int]) -> int:
    """Gate every Stripe-charging path. Raises InternalTenantBilling if exempt."""
    resolved = require_org_id(org_id)
    if is_internal_tenant(db, resolved):
        raise InternalTenantBilling(INTERNAL_TENANT_DETAIL)
    return resolved


# ── Plan catalog seed (DB-driven, no hardcoded prices in views) ──────────────
SEED_PLANS: List[Dict[str, Any]] = [
    {
        "code": "verified",
        "name": "Verified",
        "badge": "Starter",
        "tier_order": 1,
        "monthly_price_per_user": 8.0,
        # Annual = monthly x 12 x 0.8, so the published "Save 20%" is literally true.
        # temveris.example/pricing advertises $6.40/user/month billed annually, and the
        # website is the price a customer sees before they buy — it is authoritative.
        # This deliberately supersedes the $79.00 in ZT-COM-BILL-001 §13, which migration
        # c7e4b1a93f52 had aligned to: at $79 a 250-seat Governed customer paid $1,250/yr
        # more than the pricing page implied. Confirmed 2026-08-21.
        "annual_price_per_user": 76.80,
        "annual_discount_percentage": 20.0,
        "user_limit": 25,
        "is_most_popular": False,
        "is_enterprise": False,
        "trial_enabled": True,
        "short_description": "Cryptographically verified work records for small teams and contractor-heavy environments.",
        "description": "Entry plan for small teams that need verified work records, tamper-evident logs, and basic productivity evidence.",
        "feature_list_json": [
            {"label": "Automated time tracking"},
            {"label": "Idle detection"},
            {"label": "Activity signals"},
            {"label": "AI confidence scoring"},
            {"label": "Tamper-evident audit logs"},
            {"label": "Up to 25 users", "highlighted": True},
            {"label": "Email support"},
        ],
    },
    {
        "code": "governed",
        "name": "Governed",
        "badge": "Recommended",
        "tier_order": 2,
        "monthly_price_per_user": 15.0,
        "annual_price_per_user": 144.0,       # 15 x 12 x 0.8 — see the note on Verified
        "annual_discount_percentage": 20.0,
        "user_limit": 250,
        "is_most_popular": True,
        "is_enterprise": False,
        "trial_enabled": True,
        "short_description": "Policy-enforced workforce intelligence for growing companies, client-billable work, and audit-ready operations.",
        "description": "Core growth plan and recommended default. Adds policy enforcement, anomaly detection, evidence packaging, API access, and manager review workflows.",
        "feature_list_json": [
            {"label": "Everything in Verified"},
            {"label": "Anomaly detection", "highlighted": True},
            {"label": "Policy enforcement", "highlighted": True},
            {"label": "Evidence export bundles"},
            {"label": "Manager review queues"},
            {"label": "API access"},
            {"label": "Up to 250 users", "highlighted": True},
            {"label": "Priority email support"},
        ],
    },
    {
        "code": "sovereign",
        "name": "Sovereign",
        "badge": "Compliance Grade",
        "tier_order": 3,
        "monthly_price_per_user": 25.0,
        "annual_price_per_user": 240.0,       # 25 x 12 x 0.8 — already exact
        "annual_discount_percentage": 20.0,
        "user_limit": None,  # unlimited
        "is_most_popular": False,
        "is_enterprise": False,
        "trial_enabled": False,
        "short_description": "Multi-jurisdiction compliance infrastructure with custom policies, SSO, legal hold, and unlimited retention.",
        "description": "Premium compliance infrastructure for regulated, multi-jurisdiction, audit-heavy, or litigation-sensitive teams.",
        "feature_list_json": [
            {"label": "Everything in Governed"},
            {"label": "Jurisdiction-aware compliance", "highlighted": True},
            {"label": "Legal templates"},
            {"label": "Custom policy builder"},
            {"label": "SSO / SAML", "highlighted": True},
            {"label": "Legal hold", "highlighted": True},
            {"label": "Unlimited users", "highlighted": True},
            {"label": "Unlimited retention"},
            {"label": "24/5 priority support"},
        ],
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "badge": "Private Architecture",
        "tier_order": 4,
        "monthly_price_per_user": 0.0,
        "annual_price_per_user": 0.0,
        "annual_discount_percentage": 0.0,
        "user_limit": None,
        "is_most_popular": False,
        "is_enterprise": True,
        "trial_enabled": False,
        "short_description": "Dedicated infrastructure, private deployment, custom integrations, SLAs, and white-glove implementation.",
        "description": "Private architecture, contractual SLAs, custom integrations, legal/compliance advisory, and dedicated implementation.",
        "feature_list_json": [
            {"label": "Private cloud / on-premise"},
            {"label": "Custom data residency"},
            {"label": "Dedicated tenant"},
            {"label": "Workday / SAP / Oracle integrations"},
            {"label": "24/7 priority support"},
            {"label": "Dedicated implementation manager"},
        ],
    },
]


# ZT-COM-BILL-001 §J1 makes the price catalog and plan caps canonical in one
# place: code, DB, Stripe, invoices, Order Forms and the website must agree.
# Only these fields are reconciled — copy, badges and ordering stay DB-editable.
_CANONICAL_PLAN_FIELDS = ("monthly_price_per_user", "annual_price_per_user", "user_limit",
                          "feature_list_json")


def ensure_plans(db: Session) -> None:
    """Insert missing seed plans and reconcile the canonical price catalog.

    Existing rows used to be left entirely untouched, which is precisely how
    production drifted to Verified $9 / $86.40 and Governed $144 while the code
    said otherwise. §J1 requires a single canonical catalog, so pricing and cap
    fields are now corrected against SEED_PLANS; descriptive fields are not.
    """
    by_code = {p.code: p for p in db.query(models.BillingPlan).all()}
    dirty = False
    for seed in SEED_PLANS:
        row = by_code.get(seed["code"])
        if row is None:
            db.add(models.BillingPlan(**seed))
            dirty = True
            continue
        for field in _CANONICAL_PLAN_FIELDS:
            want, have = seed[field], getattr(row, field)
            if want != have:
                _log.warning(
                    "billing_plans drift corrected: %s.%s %r -> %r",
                    seed["code"], field, have, want,
                )
                setattr(row, field, want)
                dirty = True
    if dirty:
        db.commit()


def get_plan_by_code(db: Session, code: str) -> Optional[models.BillingPlan]:
    return db.query(models.BillingPlan).filter(models.BillingPlan.code == code).first()


def trial_enabled_for(db: Session, code: Optional[str]) -> bool:
    """Whether ``code`` may be started as a self-serve, no-card trial.

    The catalog has carried ``trial_enabled`` since it was seeded (Verified and
    Governed true, Sovereign and Enterprise false) but nothing consulted it, so
    a Sovereign trial could be self-provisioned for free. This is the single
    place that answers the question; callers pass the result into the routing
    decision rather than re-deriving it.

    Answered from ``SEED_PLANS`` rather than the ``billing_plans`` row, because
    the seed is the only thing that ever writes the column — ``ensure_plans``
    inserts it at creation (see ``db.add(models.BillingPlan(**seed))``) and
    there is no plan-editing endpoint anywhere in the application. Reading the
    row would therefore add nothing, and reading it *safely* is worse than it
    looks: probing for the table with a fresh inspector connection rolls back
    the caller's in-flight transaction under SQLite's StaticPool, and this runs
    inside signup. ``db`` is kept in the signature so the lookup can move to the
    catalog if plans ever become editable.

    An unrecognised plan is never trial-eligible.
    """
    if not code:
        return False
    for seed in SEED_PLANS:
        if seed.get("code") == code:
            return bool(seed.get("trial_enabled"))
    return False


def list_plans(db: Session) -> List[models.BillingPlan]:
    ensure_plans(db)
    return (
        db.query(models.BillingPlan)
          .filter(models.BillingPlan.active_status == True)  # noqa: E712
          .order_by(models.BillingPlan.tier_order.asc())
          .all()
    )


def ensure_subscription(db: Session, org_id: Optional[int]) -> models.BillingSubscription:
    """Ensure THIS organization has a subscription row (14-day Verified trial).

    ``org_id`` is required (§4). Passing ``None`` raises rather than falling back
    to a shared row — that fallback is exactly what caused cross-tenant billing.
    """
    org_id = require_org_id(org_id)
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub:
        return sub
    ensure_plans(db)
    verified = get_plan_by_code(db, "verified")
    now = datetime.now(timezone.utc)
    # Trial length comes from app/billing/trial.py so the stored end date and the ladder
    # that emails about it cannot disagree. Imported here rather than at module scope:
    # trial.py imports this module, and a top-level import would be circular.
    from app.billing.trial import TRIAL_LENGTH_DAYS       # noqa: PLC0415
    trial_ends = now + timedelta(days=TRIAL_LENGTH_DAYS)
    sub = models.BillingSubscription(
        organization_id   = org_id,
        plan_id           = verified.id if verified else None,
        billing_cycle     = "monthly",
        subscription_status = "trial",
        trial_start_date  = now,
        trial_end_date    = trial_ends,
        # Renewal is the trial end for a trialing row: it is what the billing page falls
        # back to, and a +30d value here is what made the countdown read 30 days.
        renewal_date      = trial_ends,
        currency          = "USD",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _org_scoped_users(db: Session, org_id: Optional[int]):
    """Users belonging to exactly one organization.

    billing_subscriptions still keys on a legacy string org ("default"), which
    does not map to organizations.id. So every seat query must be scoped by the
    caller's real org explicitly — otherwise one tenant is counted (and billed)
    for every user on the platform.
    """
    q = db.query(models.User)
    if org_id is None:
        return q.filter(models.User.organization_id.is_(None))
    return q.filter(models.User.organization_id == org_id)


def count_billable_seats(db: Session, org_id: Optional[int]) -> int:
    """Paid seats for ONE organization, from ``users.billing_state`` (§H3/§A4).

    Billable     — ACTIVE_BILLABLE, SUSPENDED_BILLABLE
    Not billable — INVITED_PENDING, DEACTIVATED, TERMINATED, ARCHIVED,
                   MIGRATION_REVIEW, and NULL (not yet backfilled)

    ``is_active``/``is_invited`` are deliberately not consulted: ``is_invited``
    is never cleared on acceptance, and ``is_active=False`` cannot distinguish
    suspended (billable) from deactivated (not billable).
    """
    org_id = require_org_id(org_id)
    return (
        _org_scoped_users(db, org_id)
        .filter(models.User.billing_state.in_(sorted(worker_state.BILLABLE_STATES)))
        .count()
    )


def count_reserved_seats(db: Session, org_id: Optional[int]) -> int:
    """Pending invitations — they hold plan capacity but are never charged (§A5)."""
    org_id = require_org_id(org_id)
    return (
        _org_scoped_users(db, org_id)
        .filter(models.User.billing_state.in_(sorted(worker_state.RESERVING_STATES)))
        .count()
    )


def refresh_seat_metrics(
    db: Session,
    sub: models.BillingSubscription,
    org_id: Optional[int] = None,
) -> None:
    """Recalculates seat / pending / suspended counts for ONE org.

    ``active_users`` is the seat figure the UI renders *and* the number the
    upgrade modal multiplies by (BillingModule.jsx: `subscription.active_users`),
    so it must equal the quantity sent to Stripe — quoting one number and
    charging another is worse than a loose label. Callers must pass the
    requesting user's organization_id.
    """
    org_id = require_org_id(org_id)
    scoped = lambda: _org_scoped_users(db, org_id)  # noqa: E731
    billable = count_billable_seats(db, org_id)
    pending = count_reserved_seats(db, org_id)
    suspended = scoped().filter(
        models.User.billing_state == worker_state.SUSPENDED_BILLABLE
    ).count()

    # Called on every /summary GET. Only write when a count actually moved —
    # otherwise each page load opens a write transaction against the database
    # for no change at all.
    if (
        sub.active_users == billable
        and sub.billable_users == billable
        and sub.pending_invites == pending
        and sub.suspended_users == suspended
    ):
        return

    sub.active_users = billable
    sub.billable_users = billable
    sub.pending_invites = pending
    sub.suspended_users = suspended
    db.commit()


def write_audit(
    db: Session,
    *,
    actor: Optional[models.User],
    action_key: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent_hash: Optional[str] = None,
    org_id: Optional[int] = None,
) -> models.BillingAuditEvent:
    # Audit rows are tenant-owned: an event written to a shared sentinel org is
    # visible to every tenant's audit trail. Derived from the actor when the
    # caller does not pass one explicitly.
    if org_id is None and actor is not None:
        org_id = actor.organization_id
    evt = models.BillingAuditEvent(
        organization_id   = require_org_id(org_id),
        actor_user_id     = actor.id if actor else None,
        actor_name        = actor.name if actor else None,
        actor_email       = actor.email if actor else None,
        action_key        = action_key,
        summary           = summary,
        details_json      = details or {},
        ip_address        = ip_address,
        user_agent_hash   = user_agent_hash,
    )
    db.add(evt)
    db.commit()
    db.refresh(evt)
    return evt


def seed_demo_invoices_if_empty(db: Session, sub: models.BillingSubscription) -> None:
    """RETIRED — always raises.

    This fabricated three invoices per subscription so the billing table would not
    look empty on first paint, pricing them from the local plan and adding a
    hardcoded 8% "tax". The resulting rows were indistinguishable from real ones:
    production still holds three of them claiming $9.72 each, while the actual Stripe
    invoice for that subscription is $615.

    Invoices are now mirrored from Stripe (app/billing/invoices.py), which is the only
    system that knows what a customer was actually charged. Nothing in Temveris may
    invent an invoice.

    Kept as a raising stub rather than deleted so any forgotten caller fails loudly
    instead of silently reintroducing fabricated billing records.
    """
    raise RuntimeError(
        "seed_demo_invoices_if_empty is retired: invoices are mirrored from Stripe "
        "(see app/billing/invoices.sync_org_invoices). Temveris must never fabricate "
        "an invoice."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Plan changes on an EXISTING Stripe subscription (ZT-COM-BILL-001 — Phase 2.1)
# ══════════════════════════════════════════════════════════════════════════════
def change_stripe_plan(
    db: Session,
    sub: models.BillingSubscription,
    *,
    price_id: str,
    seats: int,
    target_plan: models.BillingPlan,
    billing_cycle: str,
    stripe_client=None,
) -> models.BillingSubscription:
    """Move an existing subscription to a new price/quantity, in place.

    The defect this replaces: ``/upgrade`` always opened a Checkout Session in
    ``mode='subscription'``, so every plan change minted a *second* Stripe
    subscription while the first kept billing — and the local row's
    ``stripe_subscription_id`` was overwritten, so the app forgot the original
    existed. One customer ended up with three concurrent subscriptions.

    Guarantees:
      * exactly one Stripe subscription per organization — the existing one is
        modified, never replaced;
      * ``stripe_subscription_id`` and ``stripe_customer_id`` are never
        reassigned here;
      * Stripe is changed first, the database second. If Stripe fails, nothing
        local changes. If the local commit fails afterwards we log the drift and
        raise — a plan change is never reported as successful when it isn't.

    ``stripe_client`` is injectable so tests can exercise the whole path without
    network access.
    """
    if stripe_client is None:                     # pragma: no cover - real runtime path
        import stripe as stripe_client            # noqa: PLC0415

    # Defence in depth. The router refuses internal tenants before reaching here,
    # but this is one of only two functions in the codebase that calls
    # Subscription.modify — so it verifies for itself rather than trusting callers.
    # Checked before the Stripe id test, because an internal tenant that somehow
    # *has* a subscription id is exactly the case that must not proceed.
    if is_internal_tenant(db, sub.organization_id):
        raise StripePlanChangeError(403, INTERNAL_TENANT_DETAIL)

    subscription_id = sub.stripe_subscription_id
    customer_id = sub.stripe_customer_id
    if not subscription_id or not customer_id:
        raise StripePlanChangeError(
            409, "This organization has no Stripe subscription to modify."
        )

    # ── 1. Read the live subscription (we need its item id; not stored locally)
    try:
        remote = stripe_client.Subscription.retrieve(subscription_id)
    except Exception as exc:
        _log.error(
            "stripe retrieve failed for org=%s subscription=%s: %s",
            sub.organization_id, subscription_id, exc,
        )
        raise StripePlanChangeError(
            502, "Could not reach Stripe to read your subscription. No change was made."
        ) from exc

    # ── 2. Tenant safety: the subscription must belong to this org's customer.
    # The ids come from this organization's own row, never from the client, but
    # a mismatch would mean the row points at another tenant's subscription —
    # refuse rather than modify someone else's billing.
    remote_customer = remote["customer"]
    if remote_customer != customer_id:
        _log.error(
            "tenancy anomaly: org=%s row customer=%s but subscription %s belongs to %s",
            sub.organization_id, customer_id, subscription_id, remote_customer,
        )
        raise StripePlanChangeError(
            409, "Your billing records are inconsistent. Please contact support."
        )

    # ── 3. Only modify states Stripe allows to be modified.
    remote_status = remote["status"]
    if remote_status not in MODIFIABLE_STRIPE_STATES:
        raise StripePlanChangeError(
            409,
            f"Your subscription is {remote_status} and cannot be changed. "
            "Please start a new subscription or contact support.",
        )

    items = remote["items"]["data"]
    if len(items) != 1:
        # Multiple line items are not something this flow creates; changing the
        # price of an arbitrary one would be a guess.
        _log.error(
            "subscription %s has %d items; refusing to guess which to change",
            subscription_id, len(items),
        )
        raise StripePlanChangeError(
            409, "Your subscription has an unexpected structure. Please contact support."
        )
    item_id = items[0]["id"]

    # ── 4. Modify in place. proration_behavior credits the unused portion of the
    # old price and charges the new one pro-rata on the next invoice.
    # error_if_incomplete makes a failed payment raise instead of leaving the
    # subscription 'incomplete', so success here really means success.
    try:
        updated = stripe_client.Subscription.modify(
            subscription_id,
            items=[{"id": item_id, "price": price_id, "quantity": seats}],
            proration_behavior="create_prorations",
            payment_behavior="error_if_incomplete",
            metadata={
                "organization_id": str(sub.organization_id),
                "plan_code": target_plan.code,
                "billing_cycle": billing_cycle,
            },
        )
    except Exception as exc:
        _log.error(
            "stripe modify failed for org=%s subscription=%s price=%s seats=%s: %s",
            sub.organization_id, subscription_id, price_id, seats, exc,
        )
        raise StripePlanChangeError(
            402,
            "Stripe could not apply this plan change. Your subscription is unchanged. "
            f"Reason: {exc}",
        ) from exc

    # ── 5. Stripe is authoritative and has already changed. Mirror it locally.
    try:
        sub.plan_id = target_plan.id
        sub.billing_cycle = billing_cycle
        sub.subscription_status = "active"
        sub.billable_users = seats
        sub.active_users = seats
        sub.renewal_date = datetime.now(timezone.utc) + timedelta(
            days=365 if billing_cycle == "annual" else 30
        )

        # Project onto the commercial rows the entitlement resolver reads, inside
        # this transaction and before the commit below, so the plan the customer
        # is billed for and the plan they are entitled to cannot diverge.
        #
        # Placed here rather than at the call sites deliberately: every caller is
        # then covered by construction. Today that is POST /upgrade's in-place
        # modify branch and downgrades.apply_request; anything added later gets it
        # for free, with no second sync call to drift out of step.
        #
        # A raising observer rolls this whole block back into the BILLING DRIFT
        # path below — Stripe moved, we did not, and an operator is told. That is
        # the same trade the webhook layer makes: converging late via reconcile
        # beats committing a plan change the entitlement layer never saw.
        #
        # Imported lazily because app.billing.webhooks imports this module at
        # module level (webhooks.py:47); a top-level import here would close the
        # cycle. Same convention as the deferred imports in that module.
        from app.billing import webhooks as billing_webhooks  # noqa: PLC0415
        billing_webhooks.notify_state_applied(
            db, sub, plan_code=getattr(target_plan, "code", None),
            source="plan_change")

        db.commit()
        db.refresh(sub)
    except Exception as exc:
        db.rollback()
        # Stripe moved, we did not. Log everything an operator needs to
        # reconcile by hand; Phase 3 automates this.
        _log.error(
            "BILLING DRIFT: stripe updated but local commit failed. "
            "org=%s subscription=%s stripe_price=%s stripe_status=%s "
            "intended_plan=%s intended_cycle=%s seats=%s error=%s",
            sub.organization_id, subscription_id, price_id,
            updated["status"], target_plan.code, billing_cycle, seats, exc,
        )
        raise StripePlanChangeError(
            500,
            "Your plan was changed at Stripe but we could not record it. "
            "Support has been alerted — please do not retry.",
        ) from exc

    _log.info(
        "plan changed in place: org=%s subscription=%s -> plan=%s cycle=%s seats=%s "
        "(subscription id preserved)",
        sub.organization_id, subscription_id, target_plan.code, billing_cycle, seats,
    )
    return sub
