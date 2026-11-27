"""The 6-digit email-verification code (activation flow, Step 2 — Verify).

A six-digit secret has a million possible values. Everything here exists
because that is a weak credential and the only thing that makes it safe is the
set of controls around it: it is bound to a session, it is attempt-limited, it
is short-lived, and it lives on the commercial verification row rather than in
the password-reset table where it could authorise something else entirely.

The other half of the file guards what must NOT have changed — the 48-hour
emailed link, and password reset — because the whole design of this feature was
"add a branch, break nothing".
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user, hash_password
from app.commercial import constants, identity
from app.commercial.models import EmailVerificationToken
import app.commercial.routers.verification as verification_mod


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def user(Session):
    s = Session()
    u = User(
        name="Ada Lovelace",
        email=f"code-{int(time.time() * 1_000_000) % 10_000_000}@engines.example.com",
        password=hash_password("Passw0rd!23"),
        role="admin",
        is_active=True,
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    s.expunge(u)
    s.close()
    return u


@pytest.fixture
def client(engine, Session, user, monkeypatch):
    monkeypatch.setattr(verification_mod, "send_verification_resend", lambda *a, **k: True)

    app = FastAPI()
    app.state.limiter = verification_mod.limiter
    verification_mod.limiter.enabled = False   # the per-IP ceiling is asserted separately
    app.include_router(verification_mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=True)


def _issue(Session, user_id):
    """Mint a real verification row and hand back the plaintext credentials."""
    s = Session()
    try:
        raw_token, raw_code, row = identity.issue_verification_credentials(
            s, user_id=user_id, organization_id=None,
        )
        s.commit()
        return raw_token, raw_code, row.id
    finally:
        s.close()


def _row(Session, row_id):
    s = Session()
    try:
        return s.query(EmailVerificationToken).filter_by(id=row_id).one()
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Minting
# ══════════════════════════════════════════════════════════════════════════
def test_signup_mints_both_credentials_on_one_row(Session, user):
    raw_token, raw_code, row_id = _issue(Session, user.id)

    assert len(raw_code) == constants.VERIFICATION_CODE_LENGTH
    assert raw_code.isdigit()
    assert len(raw_token) > 30                      # still the long link token

    row = _row(Session, row_id)
    # Neither credential is recoverable from the database.
    assert row.code_hash and row.code_hash != raw_code
    assert row.token_hash and row.token_hash != raw_token
    assert row.code_attempts == 0


def test_codes_are_zero_padded_to_full_length(Session, user):
    # A code rendered as "4821" instead of "004821" would shrink the keyspace
    # and leak that the drawn number was small.
    for _ in range(40):
        _, raw_code, _ = _issue(Session, user.id)
        assert len(raw_code) == constants.VERIFICATION_CODE_LENGTH


def test_code_hash_is_keyed_not_a_bare_digest(Session, user):
    # A plain SHA-256 over a million values is a table an attacker builds in
    # seconds, so a database read would be equivalent to reading the code.
    import hashlib

    _, raw_code, row_id = _issue(Session, user.id)
    row = _row(Session, row_id)
    assert row.code_hash != hashlib.sha256(raw_code.encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# Verifying
# ══════════════════════════════════════════════════════════════════════════
def test_correct_code_verifies(client, Session, user):
    _, raw_code, row_id = _issue(Session, user.id)

    r = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    assert _row(Session, row_id).consumed_at is not None


def test_wrong_code_is_rejected_and_counted(client, Session, user):
    _, raw_code, row_id = _issue(Session, user.id)
    wrong = "".join("1" if c != "1" else "2" for c in raw_code)

    r = client.post("/api/v1/email-verification/verify-code", json={"code": wrong})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == constants.ERR_VERIFICATION_CODE_INVALID
    assert _row(Session, row_id).code_attempts == 1
    assert _row(Session, row_id).consumed_at is None


def test_attempts_lock_the_code(client, Session, user):
    _, raw_code, row_id = _issue(Session, user.id)
    wrong = "".join("1" if c != "1" else "2" for c in raw_code)

    for i in range(constants.VERIFICATION_CODE_MAX_ATTEMPTS - 1):
        assert client.post("/api/v1/email-verification/verify-code",
                           json={"code": wrong}).status_code == 400, f"attempt {i}"

    locked = client.post("/api/v1/email-verification/verify-code", json={"code": wrong})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == constants.ERR_VERIFICATION_CODE_LOCKED

    # And the CORRECT code no longer works — otherwise the lock buys nothing.
    after = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    assert after.status_code == 429
    assert _row(Session, row_id).consumed_at is None


def test_a_locked_code_is_cleared_by_a_resend(client, Session, user):
    _, raw_code, _ = _issue(Session, user.id)
    wrong = "".join("1" if c != "1" else "2" for c in raw_code)
    for _ in range(constants.VERIFICATION_CODE_MAX_ATTEMPTS):
        client.post("/api/v1/email-verification/verify-code", json={"code": wrong})

    # A fresh row means a fresh counter — the way out of a lockout.
    _, new_code, _ = _issue(Session, user.id)
    r = client.post("/api/v1/email-verification/verify-code", json={"code": new_code})
    assert r.status_code == 200, r.text


def test_expired_code_is_rejected(client, Session, user):
    _, raw_code, row_id = _issue(Session, user.id)
    s = Session()
    row = s.query(EmailVerificationToken).filter_by(id=row_id).one()
    row.created_at = (datetime.now(timezone.utc)
                      - timedelta(seconds=constants.VERIFICATION_CODE_TTL_SECONDS + 60))
    s.commit()
    s.close()

    r = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == constants.ERR_VERIFICATION_CODE_EXPIRED


@pytest.mark.parametrize("bad", ["", "1", "12345", "1234567", "abcdef", "12 34 56", None])
def test_malformed_codes_fail_like_wrong_ones(client, Session, user, bad):
    # Same code and same message for every shape of bad input — nothing tells a
    # guesser whether they were close, or even the right length.
    _issue(Session, user.id)
    r = client.post("/api/v1/email-verification/verify-code", json={"code": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == constants.ERR_VERIFICATION_CODE_INVALID


def test_verifying_twice_is_success_not_an_error(client, Session, user):
    # A double-clicked submit, or a refresh after success, must not read as a
    # failure to someone who has already verified.
    _, raw_code, _ = _issue(Session, user.id)
    first = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    second = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["verified"] is True
    assert second.json().get("already_verified") is True


def test_a_code_from_another_users_row_does_not_verify_this_session(client, Session, user):
    """The identity verified is the session's, never the code's owner."""
    s = Session()
    other = User(name="Grace", email="other-code@engines.example.com",
                 password=hash_password("Passw0rd!23"), role="admin", is_active=True)
    s.add(other)
    s.commit()
    other_id = other.id
    s.close()

    _issue(Session, user.id)                       # the session user's own code
    _, other_code, other_row = _issue(Session, other_id)

    r = client.post("/api/v1/email-verification/verify-code", json={"code": other_code})
    assert r.status_code == 400
    # The other account is untouched.
    assert _row(Session, other_row).consumed_at is None


# ══════════════════════════════════════════════════════════════════════════
# Authorization — the property that makes six digits safe
# ══════════════════════════════════════════════════════════════════════════
def test_code_endpoint_requires_a_session(engine, Session, monkeypatch):
    monkeypatch.setattr(verification_mod, "send_verification_resend", lambda *a, **k: True)
    app = FastAPI()
    app.state.limiter = verification_mod.limiter
    app.include_router(verification_mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db      # deliberately NO auth override
    anon = TestClient(app, raise_server_exceptions=True)

    r = anon.post("/api/v1/email-verification/verify-code", json={"code": "123456"})
    assert r.status_code in (401, 403), r.text


def test_status_endpoint_reports_the_session_address(client, Session, user):
    _issue(Session, user.id)
    body = client.get("/api/v1/email-verification/status").json()
    assert body["email"] == user.email          # from the session, not a query param
    assert body["verified"] is False
    assert body["code_length"] == constants.VERIFICATION_CODE_LENGTH
    assert body["resend_cooldown_seconds"] == constants.VERIFICATION_RESEND_COOLDOWN_SECONDS
    # Just minted, so the cooldown is running.
    assert 0 < body["resend_available_in"] <= constants.VERIFICATION_RESEND_COOLDOWN_SECONDS


def test_status_flips_to_verified(client, Session, user):
    _, raw_code, _ = _issue(Session, user.id)
    client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    assert client.get("/api/v1/email-verification/status").json()["verified"] is True


# ══════════════════════════════════════════════════════════════════════════
# Resend
# ══════════════════════════════════════════════════════════════════════════
def test_resend_mints_a_new_code(Session, user):
    # Without this the Verify step would keep checking the PREVIOUS row's code —
    # one the visitor no longer has in front of them.
    _issue(Session, user.id)
    s = Session()
    row = s.query(EmailVerificationToken).filter_by(user_id=user.id).order_by(
        EmailVerificationToken.id.desc()).first()
    row.last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=999)
    s.commit()

    raw_token, raw_code, err = identity.resend_verification_credentials(s, user_id=user.id)
    s.commit()
    s.close()

    assert err is None
    assert raw_token and raw_code and len(raw_code) == constants.VERIFICATION_CODE_LENGTH


def test_resend_respects_the_cooldown(Session, user):
    _issue(Session, user.id)                       # sets last_sent_at to now
    s = Session()
    try:
        raw_token, raw_code, err = identity.resend_verification_credentials(s, user_id=user.id)
        assert err == constants.ERR_RATE_LIMITED
        assert raw_token is None and raw_code is None
    finally:
        s.close()


def test_cooldown_remaining_counts_down_and_reaches_zero(Session, user):
    _issue(Session, user.id)
    s = Session()
    try:
        assert 0 < identity.resend_cooldown_remaining(s, user_id=user.id) \
            <= constants.VERIFICATION_RESEND_COOLDOWN_SECONDS

        row = s.query(EmailVerificationToken).filter_by(user_id=user.id).order_by(
            EmailVerificationToken.id.desc()).first()
        row.last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=999)
        s.commit()
        assert identity.resend_cooldown_remaining(s, user_id=user.id) == 0
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Nothing that already worked may stop working
# ══════════════════════════════════════════════════════════════════════════
def test_the_emailed_link_still_verifies(client, Session, user):
    raw_token, _, row_id = _issue(Session, user.id)

    r = client.post("/api/v1/email-verification/verify", json={"token": raw_token})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    assert _row(Session, row_id).consumed_at is not None


def test_the_link_endpoint_is_still_unauthenticated(engine, Session, user, monkeypatch):
    # Someone clicking a link in their inbox may not be signed in. This endpoint
    # must keep working without a session.
    monkeypatch.setattr(verification_mod, "send_verification_resend", lambda *a, **k: True)
    raw_token, _, _ = _issue(Session, user.id)

    app = FastAPI()
    app.state.limiter = verification_mod.limiter
    app.include_router(verification_mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db      # no auth override
    anon = TestClient(app, raise_server_exceptions=True)

    r = anon.post("/api/v1/email-verification/verify", json={"token": raw_token})
    assert r.status_code == 200, r.text


def test_link_then_code_reports_already_verified(client, Session, user):
    # One row, two credentials: whichever is used first wins and the other is
    # not an error.
    raw_token, raw_code, _ = _issue(Session, user.id)
    assert client.post("/api/v1/email-verification/verify",
                       json={"token": raw_token}).status_code == 200
    later = client.post("/api/v1/email-verification/verify-code", json={"code": raw_code})
    assert later.status_code == 200
    assert later.json()["verified"] is True


def test_code_then_link_reports_already_consumed(client, Session, user):
    raw_token, raw_code, _ = _issue(Session, user.id)
    assert client.post("/api/v1/email-verification/verify-code",
                       json={"code": raw_code}).status_code == 200
    later = client.post("/api/v1/email-verification/verify", json={"token": raw_token})
    assert later.status_code == 400
    assert later.json()["error"]["code"] == "ALREADY_CONSUMED"


def test_a_signup_code_cannot_satisfy_a_password_reset(engine, Session, user):
    """The reason this does not reuse `password_reset_otps`.

    Rows in that table are what ``POST /auth/reset-password`` checks for
    eligibility. A signup verification code stored there would double as a
    password-reset code — so it is stored on the commercial verification row
    instead, and this asserts the two tables stayed separate.
    """
    from database.models import PasswordResetOTP

    # Nothing else in the suite touches the reset table, so conftest does not
    # create it. Created here precisely so the query below can prove it is empty
    # rather than passing because the table is absent.
    PasswordResetOTP.__table__.create(engine, checkfirst=True)

    _issue(Session, user.id)
    s = Session()
    try:
        assert s.query(PasswordResetOTP).filter_by(email=user.email).count() == 0
        assert s.query(PasswordResetOTP).count() == 0
    finally:
        s.close()


def test_password_reset_otp_is_still_four_digits(Session):
    # Extracting the shared OtpInput on the frontend must not have dragged the
    # reset flow onto the signup code's length.
    from app.auth.service import _generate_otp_code, _OTP_MAX_VERIFY_ATTEMPTS

    assert len(_generate_otp_code()) == 4
    assert _OTP_MAX_VERIFY_ATTEMPTS == 5
    assert constants.VERIFICATION_CODE_LENGTH == 6
