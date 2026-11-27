"""Billing ledger (§12) — every billing mutation creates one row.

Rules:
  - Billing provider is NOT the only source of truth.
  - Internal state reconciles against provider state.
  - Every billing mutation creates a ledger entry.
  - Refunds link to original payments via parent_entry_id.
  - Credits ledgered separately from refunds.
  - Tax calculated before invoice finalization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.commercial import constants, ids
from app.commercial.models import BillingLedgerEntry


def append(
    db: Session,
    *,
    organization_id: str,
    entry_type: str,
    amount_cents: int,
    currency: str = "USD",
    description: Optional[str] = None,
    subscription_id: Optional[str] = None,
    invoice_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    parent_entry_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> BillingLedgerEntry:
    """Write one ledger row."""
    if entry_type not in constants.LEDGER_ENTRY_TYPES:
        raise ValueError(f"Unknown ledger entry type: {entry_type}")

    row = BillingLedgerEntry(
        entry_id=ids.ledger_entry_id(),
        organization_id=organization_id,
        subscription_id=subscription_id,
        invoice_id=invoice_id,
        payment_id=payment_id,
        parent_entry_id=parent_entry_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        currency=currency,
        description=description,
        metadata_json=metadata or {},
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def balance_cents(db: Session, organization_id: str, currency: str = "USD") -> int:
    """Returns the tenant's net ledger balance in cents (charges − credits/refunds).

    Positive = customer owes us; negative = we owe customer credit.
    """
    rows = (
        db.query(BillingLedgerEntry)
          .filter(BillingLedgerEntry.organization_id == organization_id)
          .filter(BillingLedgerEntry.currency == currency)
          .all()
    )
    debit_types = {
        "subscription_charge", "seat_proration_charge", "plan_upgrade_charge",
        "tax_charge", "manual_adjustment", "payment_failed",
    }
    credit_types = {
        "seat_proration_credit", "plan_downgrade_credit", "payment_received",
        "refund_issued", "credit_applied", "write_off",
    }
    total = 0
    for r in rows:
        if r.entry_type in debit_types:
            total += int(r.amount_cents or 0)
        elif r.entry_type in credit_types:
            total -= int(r.amount_cents or 0)
    return total
