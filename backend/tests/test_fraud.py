"""Fraud & Abuse service tests (§17)."""

from app.commercial import fraud, constants


def test_disposable_email_blocked(db):
    decision = fraud.evaluate(db, email="alex@mailinator.com", country="US",
                              ip_address="1.2.3.4")
    assert decision.action == fraud.ACTION_BLOCK
    assert any(s.type == "disposable_email" for s in decision.signals)


def test_sanctioned_country_blocked(db):
    decision = fraud.evaluate(db, email="alex@acme.com", country="IR")
    assert decision.action == fraud.ACTION_BLOCK


def test_clean_signup_allowed(db):
    decision = fraud.evaluate(db, email="alex@acme.com", country="US",
                              ip_address="9.9.9.9", device_fingerprint="dev123")
    assert decision.action == fraud.ACTION_ALLOW


def test_velocity_triggers_review(db):
    # Bump the same IP past the challenge threshold then evaluate.
    ip = "5.5.5.5"
    for _ in range(constants.FRAUD_SAME_IP_HOURLY_CHALLENGE + 1):
        fraud.record_attempt(db, email="alex@acme.com", ip_address=ip)
    decision = fraud.evaluate(db, email="alex@acme.com", country="US", ip_address=ip)
    # IP velocity contributes 40 points → action becomes challenge at minimum.
    assert decision.action in {fraud.ACTION_CHALLENGE, fraud.ACTION_REVIEW}
    assert any(s.type == "high_velocity_ip" for s in decision.signals)


def test_persist_signals_writes_rows(db):
    decision = fraud.evaluate(db, email="alex@mailinator.com", country="US",
                              ip_address="1.2.3.4")
    fraud.persist_signals(db, signup_id="sgn_test", organization_id=None,
                          decision=decision)
    db.flush()
    from app.commercial.models import FraudSignal
    rows = db.query(FraudSignal).filter_by(signup_id="sgn_test").all()
    assert len(rows) >= 1
