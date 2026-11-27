"""
Data-retention policy — per organization.
=========================================
How long a tenant's screenshots and activity captures are kept is a compliance
decision that belongs to the tenant, not to the deployment. It used to be a
single ``RETENTION_DAYS`` environment variable read once in ``main.py`` and
applied to every organization's data in one nightly sweep, so a customer with a
30-day statutory limit and a customer with a 12-month audit requirement could
not both be served by the same instance.

``retention.screenshot_days`` is org-scoped through the normal cascade. The
system default (30) is the previous global default, so nothing changes for an
existing tenant until they configure it.

Note the offboarding module already models retention correctly per jurisdiction
(``app/offboarding/constants.py::RETENTION_DAYS_DEFAULTS``); this covers the
operational tracking data that sweep does not own.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from sqlalchemy.orm import Session

SETTING_SCREENSHOT_RETENTION_DAYS = "retention.screenshot_days"

# The old global default. Still honoured as the SYSTEM default so an existing
# deployment that sets RETENTION_DAYS keeps its behaviour until tenants
# configure their own.
_ENV_DEFAULT = os.getenv("RETENTION_DAYS", "30")

SETTING_DEFAULTS: Dict[str, str] = {
    SETTING_SCREENSHOT_RETENTION_DAYS: _ENV_DEFAULT,
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    SETTING_SCREENSHOT_RETENTION_DAYS:
        "Days to keep screenshots and activity captures before automatic "
        "deletion. Set to 0 to keep them indefinitely.",
}

# Guard rails: a typo must not delete everything or keep it forever by accident.
_MIN_DAYS, _MAX_DAYS = 1, 3650


def retention_days(db: Session, org_id) -> Optional[int]:
    """Retention window for this organization, or ``None`` for "keep forever".

    ``None`` is returned for an explicit 0 so the caller can skip the sweep
    entirely rather than computing a cutoff of "now".
    """
    from app.settings import scope as settings_scope
    from database import models
    try:
        merged = settings_scope.resolve_effective(
            db, models.User(organization_id=org_id), SETTING_DEFAULTS)
        raw = merged.get(SETTING_SCREENSHOT_RETENTION_DAYS, _ENV_DEFAULT)
        days = int(str(raw).strip())
    except Exception:
        try:
            days = int(_ENV_DEFAULT)
        except (TypeError, ValueError):
            days = 30
    if days <= 0:
        return None
    return max(_MIN_DAYS, min(_MAX_DAYS, days))
