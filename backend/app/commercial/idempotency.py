"""Idempotency for mutating commercial endpoints (Build Contracts §7.1).

A client sends an ``Idempotency-Key`` header on a mutating request. The first
request runs the operation and stores its response keyed by (scope, key). A
retry / double-submit with the same key replays the stored response instead of
running the operation again — so a network timeout or double-click can never
create a second user / org / workspace / checkout.

Design:
  * ``begin()`` inserts an ``in_progress`` row (unique on scope+key, so a
    concurrent duplicate loses the race and is told the request is in flight).
  * the handler runs, then ``complete()`` records the final status + body.
  * a replay with a matching request hash returns the stored response; a
    replay with a *different* body under the same key is rejected (422).

Absent an ``Idempotency-Key`` header the endpoint behaves exactly as before —
this is opt-in and fully backward compatible.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.commercial.models import CommercialIdempotencyKey, _utcnow
from app.commercial.routers._helpers import error_payload

_log = logging.getLogger("zoiko.commercial.idempotency")

TTL_HOURS = 24
HEADER = "Idempotency-Key"


def request_hash(scope: str, body: dict) -> str:
    """Stable sha256 of the canonical request body, scoped."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{scope}:{canonical}".encode("utf-8")).hexdigest()


@dataclass
class IdemOutcome:
    replay: bool = False
    conflict: Optional[str] = None            # "in_progress" | "payload_mismatch"
    status_code: Optional[int] = None
    response: Optional[dict] = None
    row: Optional[CommercialIdempotencyKey] = None


def begin(db: Session, *, scope: str, key: str, req_hash: str) -> IdemOutcome:
    """Look up or claim an idempotency key. Call BEFORE any other mutation so a
    race-loss rollback undoes only this claim."""
    now = _utcnow()
    existing = (
        db.query(CommercialIdempotencyKey)
        .filter_by(scope=scope, idempotency_key=key)
        .one_or_none()
    )
    if existing is not None:
        # SQLite returns naive datetimes even from timezone=True columns;
        # normalize to aware UTC before comparing (Postgres already returns aware).
        exp = existing.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and exp < now:
            db.delete(existing)
            db.flush()
            existing = None
        elif existing.status == "completed":
            if existing.request_hash != req_hash:
                return IdemOutcome(conflict="payload_mismatch")
            return IdemOutcome(
                replay=True, status_code=existing.http_status,
                response=existing.response_json, row=existing,
            )
        else:
            return IdemOutcome(conflict="in_progress", row=existing)

    row = CommercialIdempotencyKey(
        scope=scope, idempotency_key=key, request_hash=req_hash,
        status="in_progress", expires_at=now + timedelta(hours=TTL_HOURS),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request claimed the same key first.
        db.rollback()
        return IdemOutcome(conflict="in_progress")
    return IdemOutcome(replay=False, row=row)


def complete(db: Session, row: CommercialIdempotencyKey, *, status_code: int, response: dict) -> None:
    row.status = "completed"
    row.http_status = status_code
    row.response_json = response
    db.add(row)
    db.flush()


def _normalize(resp: Any) -> Tuple[int, dict]:
    """Reduce a handler return value to (status_code, json-body-dict) for storage."""
    if isinstance(resp, JSONResponse):
        try:
            body = json.loads(bytes(resp.body).decode("utf-8"))
        except Exception:
            body = {}
        return resp.status_code, body
    if isinstance(resp, dict):
        return 200, resp
    # pydantic model or other — best effort
    if hasattr(resp, "model_dump"):
        return 200, resp.model_dump()
    return 200, {"result": str(resp)}


def run(db: Session, *, scope: str, request: Optional[Request], payload, fn: Callable):
    """Functional idempotency wrapper. Call AFTER any authorization so a replay
    can't bypass auth. No ``Idempotency-Key`` header → runs ``fn`` unchanged."""
    key = request.headers.get(HEADER) if request is not None else None
    if not key or db is None:
        return fn()

    body = payload.model_dump() if hasattr(payload, "model_dump") else (payload or {})
    outcome = begin(db, scope=scope, key=key, req_hash=request_hash(scope, body))

    if outcome.replay:
        return JSONResponse(status_code=outcome.status_code or 200, content=outcome.response)
    if outcome.conflict == "payload_mismatch":
        return JSONResponse(status_code=422, content=error_payload(
            code="IDEMPOTENCY_KEY_REUSED",
            message="This idempotency key was already used with a different request.",
            recoverable=False,
        ))
    if outcome.conflict == "in_progress":
        return JSONResponse(status_code=409, content=error_payload(
            code="REQUEST_IN_PROGRESS",
            message="A request with this idempotency key is still being processed.",
            recoverable=True, recovery_action="wait_and_retry",
        ))

    resp = fn()
    status_code, content = _normalize(resp)
    complete(db, outcome.row, status_code=status_code, response=content)
    db.commit()
    return resp


def idempotent(scope: str) -> Callable:
    """Decorator for a FastAPI route ``(payload, request, db, ...)``.

    No ``Idempotency-Key`` header → passes straight through (unchanged behavior).
    With a header → claim/replay/reject as above and persist the response.
    Note: runs before the wrapped fn, so use ``run()`` directly when the handler
    must authorize before the idempotency claim.
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)  # preserves signature so FastAPI DI still works
        def wrapper(*args, **kwargs):
            request: Optional[Request] = kwargs.get("request")
            db: Optional[Session] = kwargs.get("db")
            payload = kwargs.get("payload")
            return run(db, scope=scope, request=request, payload=payload,
                       fn=lambda: fn(*args, **kwargs))
        return wrapper
    return deco
