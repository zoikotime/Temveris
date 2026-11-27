from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import date, datetime


# ==============================
# 🏢 DEPARTMENT SCHEMAS
# ==============================
class DepartmentCreate(BaseModel):
    name: str
    description: Optional[str] = None

class DepartmentResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ==============================
# 👥 TEAM SCHEMAS
# ==============================
class TeamCreate(BaseModel):
    name: str
    department_id: Optional[int] = None

class TeamResponse(BaseModel):
    id: int
    name: str
    department_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ==============================
# 🧑‍💼 EMPLOYEE SCHEMAS
# ==============================
class EmployeeCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: Optional[str] = "employee"

    phone: Optional[str] = None
    address: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    employee_code: Optional[str] = None
    designation: Optional[str] = None
    date_of_joining: Optional[date] = None
    employment_type: Optional[str] = "full_time"


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    designation: Optional[str] = None
    employment_type: Optional[str] = None
    is_active: Optional[bool] = None
    # Allow admins to backfill / correct the Official Start Date after invite.
    # Drives the leave-eligibility countdown.
    date_of_joining: Optional[date] = None