"""Temveris Commercial Activation System (Tier-0 Spec v3.0).

This package implements the governed commercial activation system that
converts demand into revenue through one auditable pipeline:

    Website visitor -> Signup intent -> User identity -> Organization
    -> Workspace -> Subscription state -> Billing record -> Entitlement
    profile -> Onboarding -> Team activation -> Trial conversion -> Paid retention

It runs alongside the existing single-tenant time-tracking app. New
multi-tenant tables coexist with legacy tables that use a "default"
organization_id; the commercial layer mints real organization_ids on
self-serve signup and the existing app modules will eventually migrate
to read those instead of the "default" sentinel.
"""

__version__ = "3.0.0"
