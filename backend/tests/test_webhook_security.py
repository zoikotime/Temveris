"""Webhook signature verification must fail closed (Build Contracts §7.3).

Regression guard for the "accept-all default" hole: an unconfigured /
secret-less provider must reject webhooks unless unverified acceptance is
explicitly opted in for local dev.
"""
import os

from app.commercial.billing.providers import (
    BillingProvider, StubProvider, _allow_unverified_webhooks,
)


def _clear_optin(monkeypatch):
    monkeypatch.delenv("ZT_ALLOW_UNVERIFIED_WEBHOOKS", raising=False)


def test_base_provider_fails_closed():
    assert BillingProvider().verify_webhook(payload=b"{}", signature=None) is False


def test_stub_rejects_by_default(monkeypatch):
    _clear_optin(monkeypatch)
    assert StubProvider().verify_webhook(payload=b"{}", signature=None) is False


def test_stub_accepts_only_with_explicit_optin(monkeypatch):
    monkeypatch.setenv("ZT_ALLOW_UNVERIFIED_WEBHOOKS", "1")
    assert StubProvider().verify_webhook(payload=b"{}", signature="x") is True


def test_optin_flag_parsing(monkeypatch):
    for val, expected in [("1", True), ("true", True), ("YES", True),
                          ("0", False), ("", False), ("no", False)]:
        monkeypatch.setenv("ZT_ALLOW_UNVERIFIED_WEBHOOKS", val)
        assert _allow_unverified_webhooks() is expected
    _clear_optin(monkeypatch)
    assert _allow_unverified_webhooks() is False
