"""Idempotency framework tests (Build Contracts §7.1).

Covers the service (begin/complete/replay/mismatch/in-progress/expiry) and the
@idempotent decorator wiring (no-key passthrough, replay, single execution).
"""
from datetime import timedelta

from fastapi.responses import JSONResponse

from app.commercial import idempotency
from app.commercial.models import CommercialIdempotencyKey, _utcnow


# ── service ────────────────────────────────────────────────────────────────
def test_request_hash_stable_and_order_independent():
    a = idempotency.request_hash("signup", {"email": "a@b.com", "plan": "verified"})
    b = idempotency.request_hash("signup", {"plan": "verified", "email": "a@b.com"})
    assert a == b
    assert a != idempotency.request_hash("signup", {"email": "a@b.com", "plan": "governed"})
    assert a != idempotency.request_hash("checkout", {"email": "a@b.com", "plan": "verified"})


def test_begin_claims_then_replays(db):
    h = idempotency.request_hash("signup", {"email": "x@y.com"})
    first = idempotency.begin(db, scope="signup", key="k1", req_hash=h)
    assert first.replay is False and first.row is not None
    idempotency.complete(db, first.row, status_code=201, response={"ok": True, "user_id": 7})
    db.commit()

    again = idempotency.begin(db, scope="signup", key="k1", req_hash=h)
    assert again.replay is True
    assert again.status_code == 201
    assert again.response == {"ok": True, "user_id": 7}


def test_same_key_different_payload_is_rejected(db):
    idempotency.begin(db, scope="signup", key="k2", req_hash="hash-A")
    row = db.query(CommercialIdempotencyKey).filter_by(idempotency_key="k2").one()
    idempotency.complete(db, row, status_code=201, response={"ok": True})
    db.commit()

    out = idempotency.begin(db, scope="signup", key="k2", req_hash="hash-B")
    assert out.replay is False
    assert out.conflict == "payload_mismatch"


def test_in_progress_conflict(db):
    idempotency.begin(db, scope="signup", key="k3", req_hash="h")  # left in_progress
    db.commit()
    out = idempotency.begin(db, scope="signup", key="k3", req_hash="h")
    assert out.conflict == "in_progress"


def test_expired_key_is_reclaimable(db):
    idempotency.begin(db, scope="signup", key="k4", req_hash="h")
    row = db.query(CommercialIdempotencyKey).filter_by(idempotency_key="k4").one()
    idempotency.complete(db, row, status_code=201, response={"ok": True})
    row.expires_at = _utcnow() - timedelta(hours=1)   # force-expire
    db.commit()

    out = idempotency.begin(db, scope="signup", key="k4", req_hash="h")
    assert out.replay is False and out.conflict is None  # reclaimed, not replayed


# ── decorator ────────────────────────────────────────────────────────────────
class _FakeReq:
    def __init__(self, headers): self.headers = headers


class _Payload:
    def __init__(self, **d): self._d = d
    def model_dump(self): return dict(self._d)


def test_decorator_passthrough_without_key(db):
    calls = {"n": 0}

    @idempotency.idempotent("signup")
    def handler(*, payload, request, db):
        calls["n"] += 1
        return {"created": True}

    before = db.query(CommercialIdempotencyKey).count()
    r = handler(payload=_Payload(email="a@b.com"), request=_FakeReq({}), db=db)
    assert r == {"created": True}
    assert calls["n"] == 1
    # no header → nothing new stored (shared StaticPool DB persists across tests)
    assert db.query(CommercialIdempotencyKey).count() == before


def test_decorator_runs_once_then_replays(db):
    calls = {"n": 0}

    @idempotency.idempotent("signup")
    def handler(*, payload, request, db):
        calls["n"] += 1
        return JSONResponse(status_code=201, content={"user_id": 42})

    req = _FakeReq({"Idempotency-Key": "abc"})
    p = _Payload(email="a@b.com", plan="verified")

    r1 = handler(payload=p, request=req, db=db)
    r2 = handler(payload=p, request=req, db=db)

    assert calls["n"] == 1, "handler must run exactly once for a repeated key"
    assert isinstance(r2, JSONResponse) and r2.status_code == 201
    import json
    assert json.loads(bytes(r2.body)) == {"user_id": 42}
