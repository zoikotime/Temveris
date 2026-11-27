"""
Temveris – Payroll Schemas
"""

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ── Payroll Config (per-employee) ─────────────────────────────────────────────
class PayrollConfigCreate(BaseModel):
    user_id:        int
    pay_type:       str = "salary"
    hourly_rate:    float = 0.0
    basic_salary:   float = 0.0
    hra:            float = 0.0
    da:             float = 0.0
    ta:             float = 0.0
    pf:             float = 0.0
    tax:            float = 0.0
    bonus:          float = 0.0
    currency:       str = "USD"
    pay_frequency:  str = "monthly"
    bank_account:   Optional[str] = None
    notes:          Optional[str] = None


class PayrollConfigUpdate(BaseModel):
    pay_type:       Optional[str] = None
    hourly_rate:    Optional[float] = None
    basic_salary:   Optional[float] = None
    hra:            Optional[float] = None
    da:             Optional[float] = None
    ta:             Optional[float] = None
    pf:             Optional[float] = None
    tax:            Optional[float] = None
    bonus:          Optional[float] = None
    currency:       Optional[str] = None
    pay_frequency:  Optional[str] = None
    bank_account:   Optional[str] = None
    notes:          Optional[str] = None


class PayrollConfigOut(BaseModel):
    id:             int
    user_id:        int
    pay_type:       str
    hourly_rate:    float
    basic_salary:   float
    hra:            float
    da:             float
    ta:             float
    pf:             float
    tax:            float
    bonus:          float
    currency:       str
    pay_frequency:  str
    bank_account:   Optional[str] = None
    notes:          Optional[str] = None
    is_active:      bool
    created_at:     Optional[datetime] = None
    updated_at:     Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Payroll Run ───────────────────────────────────────────────────────────────
class PayrollRunCreate(BaseModel):
    period_start:   str   # ISO date string
    period_end:     str
    notes:          Optional[str] = None


class PayrollRunOut(BaseModel):
    id:             int
    period_start:   Optional[datetime] = None
    period_end:     Optional[datetime] = None
    status:         str
    total_amount:   float
    currency:       str
    notes:          Optional[str] = None
    created_by:     Optional[int] = None
    created_at:     Optional[datetime] = None
    item_count:     int = 0

    class Config:
        from_attributes = True
