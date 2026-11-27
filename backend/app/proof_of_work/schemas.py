"""
Temveris - Proof-of-Work Pydantic Schemas
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any, List

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# REQUEST schemas
# ──────────────────────────────────────────────

class CreateWorkRecordRequest(BaseModel):
    timestamp: datetime
    duration: int = Field(0, ge=0, description="Work duration in seconds")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    device_id: Optional[str] = None
    task_id: Optional[str] = None
    artifact_url: Optional[str] = None
    artifact_hash: Optional[str] = Field(None, max_length=64, description="SHA-256 of the artifact file")
    keyboard_activity: int = Field(0, ge=0)
    mouse_activity: int = Field(0, ge=0)
    policy_context: Optional[Dict[str, Any]] = None
    session_id: Optional[int] = None


# ──────────────────────────────────────────────
# RESPONSE schemas
# ──────────────────────────────────────────────

class WorkRecordResponse(BaseModel):
    record_id: str
    user_id: int
    session_id: Optional[int]
    timestamp: datetime
    duration: int
    latitude: Optional[float]
    longitude: Optional[float]
    device_id: Optional[str]
    task_id: Optional[str]
    artifact_url: Optional[str]
    artifact_hash: Optional[str]
    keyboard_activity: int
    mouse_activity: int
    policy_context: Optional[Dict[str, Any]]
    validation_status: str
    confidence_score: int
    version: int
    previous_hash: Optional[str]
    current_hash: str
    created_at: datetime

    class Config:
        from_attributes = True


class WorkRecordCreatedResponse(BaseModel):
    success: bool = True
    record_id: str
    current_hash: str
    validation_status: str
    message: str = "Record created. Validation running in background."


class ValidationResultResponse(BaseModel):
    record_id: str
    confidence_score: int
    validation_status: str
    deductions: List[dict]
    fraud_events_created: int
    fraud_events: List[dict]


class FraudEventResponse(BaseModel):
    id: int
    wec_id: str
    reason: str
    severity: str
    details: Optional[Dict[str, Any]]
    created_at: datetime

    class Config:
        from_attributes = True
