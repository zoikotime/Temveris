"""Guard: the shared "default" billing organization must never come back.

`billing_*.organization_id` was `VARCHAR(80) DEFAULT 'default'` with no foreign
key, so every tenant shared one subscription row: one organization was billed
for every user on the platform and the others could not be billed at all.

These are static checks over the source tree rather than behavioural tests —
the defect was a *literal* that reappeared in four modules outside `app/billing`
long after the billing code itself had moved on. A behavioural test in the
billing suite would not have caught any of them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.billing import service as billing_service

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Billing ORM models whose organization_id is an INTEGER FK to organizations.id.
_BILLING_MODELS = (
    "BillingSubscription", "BillingInvoice", "BillingContact",
    "BillingTaxProfile", "BillingPaymentMethod", "BillingAuditEvent",
    "BillingDowngradeRequest", "BillingEnterpriseQuote",
)

# e.g. BillingSubscription.organization_id == "default"
_STRING_COMPARISON = re.compile(
    r"(?:" + "|".join(_BILLING_MODELS) + r")\.organization_id\s*(?:==|!=|=)\s*[\"']([^\"']*)[\"']"
)


def _python_sources():
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_string_literal_compared_against_a_billing_organization_id():
    """organization_id is an integer FK; comparing it to any string literal is a
    type error in Postgres and was previously a cross-tenant data leak."""
    offenders = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            match = _STRING_COMPARISON.search(line)
            if match:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{lineno} -> {match.group(0)}")

    assert not offenders, (
        "billing organization_id compared against a string literal:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse the authenticated caller's organization id. There is no "
          "default/shared billing organization."
    )


def test_default_org_sentinel_is_not_reintroduced_in_code():
    """`DEFAULT_ORG_ID = "default"` must not return as a live constant."""
    offenders = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or "``" in line:
                continue    # prose / docstring references to the old defect are fine
            if re.search(r"DEFAULT_ORG_ID\s*=\s*[\"']default[\"']", stripped):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{lineno}")

    assert not offenders, f"DEFAULT_ORG_ID sentinel reintroduced at: {offenders}"


def test_billing_service_exposes_no_default_org_constant():
    assert not hasattr(billing_service, "DEFAULT_ORG_ID")


def test_org_scoped_helpers_fail_closed_without_an_organization():
    """No caller may transact billing without a real organization."""
    with pytest.raises(billing_service.MissingOrganization):
        billing_service.require_org_id(None)


def test_billing_models_use_integer_fk_to_organizations():
    """The schema itself must enforce tenancy, not convention."""
    from database import models

    for name in _BILLING_MODELS:
        model = getattr(models, name)
        column = model.__table__.columns["organization_id"]
        assert "INTEGER" in str(column.type).upper(), f"{name}.organization_id is {column.type}"
        targets = {fk.target_fullname for fk in column.foreign_keys}
        assert targets == {"organizations.id"}, f"{name}.organization_id FK -> {targets}"
        assert column.default is None, (
            f"{name}.organization_id still carries a column default "
            f"({column.default.arg!r}); a default tenant must not exist"
        )
