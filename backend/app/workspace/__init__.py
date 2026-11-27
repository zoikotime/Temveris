"""Workspace Settings backend (Tab 4-10 of the Workspace Settings spec).

Provides REST endpoints feeding the Roles & Permissions, Policies, Billing &
Subscription summary, Security, Connected Systems, and Audit & Compliance
tabs. Each endpoint returns real DB-derived state — the spec is explicit
that no surface ships static content.
"""
