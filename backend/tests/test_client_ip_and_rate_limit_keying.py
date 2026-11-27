"""Client-IP resolution and rate-limit keying behind a proxy.

Two production-only bugs are pinned here, both invisible to local testing
because a dev machine is a single client talking to an unproxied server.

1. **Shared bucket.** SlowAPI keyed on `request.client.host`, which behind Cloud
   Run is the Google front end — the same value for every visitor. `10/hour` on
   signup became ten signups per hour *platform-wide*.

2. **Spoofable bucket.** The obvious fix (`FORWARDED_ALLOW_IPS=*`, or reading
   `X-Forwarded-For[0]` by hand) trusts the LEFTMOST forwarded entry. Google
   appends the real peer *after* whatever the client sent, so the leftmost value
   is attacker-controlled and a caller could mint a fresh bucket per request.

The rule under test: read the forwarded header right-to-left, skipping
`TRUSTED_PROXY_HOPS` trailing entries.
"""

from __future__ import annotations

import os
import sys
import pathlib

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.client_ip import client_ip, rate_limit_key  # noqa: E402


# ─── Request stubs ───────────────────────────────────────────────────────────

class _Headers(dict):
    """Case-insensitive enough for these tests; keys are supplied lowercase."""
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Peer:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, headers=None, peer=None):
        self.headers = _Headers(headers or {})
        self.client = _Peer(peer) if peer else None


# Address Cloud Run presents as the TCP peer — a stand-in; the point is that it
# is the proxy, not the visitor.
PROXY_PEER = "169.254.1.1"


@pytest.fixture(autouse=True)
def _clean_hops(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)


# ═════════════════════════════════════════════════════════════════════════════
# Spoof resistance — the security property
# ═════════════════════════════════════════════════════════════════════════════

class TestSpoofResistance:

    def test_client_supplied_forwarded_prefix_is_ignored(self):
        """The attack: send your own X-Forwarded-For, get a fresh bucket."""
        req = _Req({"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, peer=PROXY_PEER)
        assert client_ip(req) == "203.0.113.7"

    def test_a_long_forged_chain_still_resolves_to_the_appended_peer(self):
        req = _Req(
            {"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.7"},
            peer=PROXY_PEER,
        )
        assert client_ip(req) == "203.0.113.7"

    def test_varying_the_forged_prefix_does_not_change_the_bucket(self):
        """Directly refutes the bypass: same real client, one key."""
        keys = {
            rate_limit_key(_Req(
                {"x-forwarded-for": f"{i}.{i}.{i}.{i}, 203.0.113.7"}, peer=PROXY_PEER,
            ))
            for i in range(1, 20)
        }
        assert keys == {"203.0.113.7"}

    def test_we_never_return_the_leftmost_entry_of_a_multi_hop_header(self):
        """Guards against a future refactor reintroducing [0]."""
        req = _Req({"x-forwarded-for": "evil, good"}, peer=PROXY_PEER)
        assert client_ip(req) != "evil"


# ═════════════════════════════════════════════════════════════════════════════
# Bucket isolation — the availability property
# ═════════════════════════════════════════════════════════════════════════════

class TestBucketIsolation:

    def test_distinct_clients_get_distinct_keys(self):
        a = _Req({"x-forwarded-for": "203.0.113.1"}, peer=PROXY_PEER)
        b = _Req({"x-forwarded-for": "203.0.113.2"}, peer=PROXY_PEER)
        assert rate_limit_key(a) != rate_limit_key(b)

    def test_same_client_shares_one_key(self):
        a = _Req({"x-forwarded-for": "203.0.113.1"}, peer=PROXY_PEER)
        b = _Req({"x-forwarded-for": "203.0.113.1"}, peer=PROXY_PEER)
        assert rate_limit_key(a) == rate_limit_key(b)

    def test_the_old_behaviour_would_have_collapsed_these(self):
        """Shows the regression this replaces: client.host is identical."""
        a = _Req({"x-forwarded-for": "203.0.113.1"}, peer=PROXY_PEER)
        b = _Req({"x-forwarded-for": "203.0.113.2"}, peer=PROXY_PEER)
        assert a.client.host == b.client.host == PROXY_PEER   # what slowapi used
        assert rate_limit_key(a) != rate_limit_key(b)         # what we use now


# ═════════════════════════════════════════════════════════════════════════════
# Fallbacks — local development must keep working
# ═════════════════════════════════════════════════════════════════════════════

class TestFallbacks:

    def test_no_forwarded_header_uses_the_peer(self):
        assert client_ip(_Req({}, peer="127.0.0.1")) == "127.0.0.1"

    def test_x_real_ip_is_honoured_when_no_forwarded_for(self):
        req = _Req({"x-real-ip": "198.51.100.4"}, peer="10.0.0.1")
        assert client_ip(req) == "198.51.100.4"

    def test_forwarded_for_wins_over_x_real_ip(self):
        req = _Req(
            {"x-forwarded-for": "a, 203.0.113.7", "x-real-ip": "198.51.100.4"},
            peer=PROXY_PEER,
        )
        assert client_ip(req) == "203.0.113.7"

    def test_no_information_at_all_returns_none(self):
        assert client_ip(_Req({})) is None

    def test_none_request_is_tolerated(self):
        assert client_ip(None) is None

    def test_rate_limit_key_never_returns_none(self):
        """SlowAPI requires a string; an unidentifiable caller shares one bucket
        rather than escaping limits entirely."""
        assert rate_limit_key(_Req({})) == "127.0.0.1"
        assert isinstance(rate_limit_key(None), str)

    def test_empty_and_whitespace_forwarded_header_falls_through(self):
        assert client_ip(_Req({"x-forwarded-for": ""}, peer="127.0.0.1")) == "127.0.0.1"
        assert client_ip(_Req({"x-forwarded-for": " , , "}, peer="127.0.0.1")) == "127.0.0.1"


class TestIPv6:
    """An IPv6 address contains colons and may be bracketed, but never a comma,
    so comma-splitting cannot break an address in half."""

    def test_bare_ipv6_survives_intact(self):
        req = _Req({"x-forwarded-for": "2001:db8::1"}, peer=PROXY_PEER)
        assert client_ip(req) == "2001:db8::1"

    def test_bracketed_ipv6_with_port_survives_intact(self):
        req = _Req({"x-forwarded-for": "[2001:db8::1]:443"}, peer=PROXY_PEER)
        assert client_ip(req) == "[2001:db8::1]:443"

    def test_forged_ipv6_prefix_is_still_ignored(self):
        req = _Req({"x-forwarded-for": "::1, 2001:db8::dead"}, peer=PROXY_PEER)
        assert client_ip(req) == "2001:db8::dead"

    def test_mixed_v4_and_v6_chain(self):
        req = _Req({"x-forwarded-for": "9.9.9.9, 2001:db8::beef"}, peer=PROXY_PEER)
        assert client_ip(req) == "2001:db8::beef"

    def test_ipv6_clients_get_distinct_buckets(self):
        a = _Req({"x-forwarded-for": "2001:db8::1"}, peer=PROXY_PEER)
        b = _Req({"x-forwarded-for": "2001:db8::2"}, peer=PROXY_PEER)
        assert rate_limit_key(a) != rate_limit_key(b)


# ═════════════════════════════════════════════════════════════════════════════
# TRUSTED_PROXY_HOPS
# ═════════════════════════════════════════════════════════════════════════════

class TestTrustedProxyHops:

    def test_default_is_rightmost(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        req = _Req({"x-forwarded-for": "a, b, 203.0.113.7"}, peer=PROXY_PEER)
        assert client_ip(req) == "203.0.113.7"

    def test_one_hop_skips_one_trailing_entry(self, monkeypatch):
        """An external load balancer appends its own address after Cloud Run's."""
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        req = _Req(
            {"x-forwarded-for": "forged, 203.0.113.7, 130.211.0.1"}, peer=PROXY_PEER,
        )
        assert client_ip(req) == "203.0.113.7"

    def test_more_hops_than_entries_never_returns_a_forged_entry(self, monkeypatch):
        """A misconfigured hop count must not become spoofable.

        Regression test for a real defect: the offset originally clamped to
        hops[0] when it fell off the left end, which is precisely the
        attacker-supplied value. It now ignores the header and uses the peer —
        a coarser bucket, but not a forgeable one.
        """
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "5")
        req = _Req(
            {"x-forwarded-for": "forged1, forged2, 203.0.113.7"}, peer=PROXY_PEER,
        )
        got = client_ip(req)
        assert got != "forged1", "clamped into the client-controlled prefix"
        assert got != "forged2"
        assert got == PROXY_PEER

    def test_overshooting_hops_degrades_to_a_shared_not_spoofable_bucket(self, monkeypatch):
        """Two different forged prefixes must not yield two different buckets."""
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "9")
        keys = {
            rate_limit_key(_Req({"x-forwarded-for": f"{i}.{i}.{i}.{i}, 203.0.113.7"},
                                peer=PROXY_PEER))
            for i in range(1, 6)
        }
        assert keys == {PROXY_PEER}, f"misconfig became spoofable: {keys}"

    def test_hops_exactly_equal_to_entry_count_is_still_safe(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        req = _Req({"x-forwarded-for": "forged, 203.0.113.7"}, peer=PROXY_PEER)
        assert client_ip(req) == PROXY_PEER

    def test_garbage_and_negative_values_fall_back_to_the_default(self, monkeypatch):
        for bad in ("abc", "", "-3", "1.5"):
            monkeypatch.setenv("TRUSTED_PROXY_HOPS", bad)
            req = _Req({"x-forwarded-for": "a, 203.0.113.7"}, peer=PROXY_PEER)
            assert client_ip(req) == "203.0.113.7", f"failed for {bad!r}"


# ═════════════════════════════════════════════════════════════════════════════
# Wiring — the limiter and the fraud path must both use this
# ═════════════════════════════════════════════════════════════════════════════

class TestWiring:

    def test_slowapi_limiter_uses_our_key_function(self):
        from app.rate_limit import limiter
        assert limiter._key_func is rate_limit_key, (
            "the limiter is not keyed on app.client_ip.rate_limit_key — per-IP "
            "limits would collapse into one global bucket behind Cloud Run"
        )

    def test_slowapi_get_remote_address_is_no_longer_imported(self):
        """Checks the import, not the prose — the module docstring names
        get_remote_address deliberately, to explain why it is avoided."""
        import inspect
        import re
        import app.rate_limit as rl

        src = inspect.getsource(rl)
        assert not re.search(r"^\s*from\s+slowapi\.util\s+import", src, re.M)
        assert not hasattr(rl, "get_remote_address")

    def test_signup_fraud_path_resolves_ip_the_same_way(self):
        """client_meta feeds fraud.record_attempt; it must not be spoofable."""
        from app.commercial.routers._helpers import client_meta
        ip, _ua = client_meta(
            _Req({"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, peer=PROXY_PEER)
        )
        assert ip == "203.0.113.7"

    def test_client_meta_still_returns_a_user_agent_hash(self):
        from app.commercial.routers._helpers import client_meta
        ip, ua = client_meta(
            _Req({"x-forwarded-for": "203.0.113.7", "user-agent": "Mozilla/5.0"},
                 peer=PROXY_PEER)
        )
        assert ip == "203.0.113.7"
        assert ua and len(ua) == 16          # sha256 prefix, unchanged contract

    def test_client_meta_tolerates_no_request(self):
        from app.commercial.routers._helpers import client_meta
        assert client_meta(None) == (None, None)


# ═════════════════════════════════════════════════════════════════════════════
# Rejected alternative — documented so it is not reintroduced
# ═════════════════════════════════════════════════════════════════════════════

class TestForwardedAllowIpsIsNotTheFix:

    def test_uvicorn_always_trust_takes_the_leftmost_entry(self):
        """Why FORWARDED_ALLOW_IPS=* was rejected.

        If a future uvicorn changes this to be right-to-left, the env-var
        approach becomes viable and this test will fail, prompting a re-review.
        """
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        mw = ProxyHeadersMiddleware(app=None, trusted_hosts="*")
        assert mw.always_trust is True
        assert mw.get_trusted_client_host(["9.9.9.9", "203.0.113.7"]) == "9.9.9.9"

    def test_forwarded_allow_ips_is_not_set_in_the_repo(self):
        """Guards against someone adding the unsafe shortcut later."""
        import subprocess
        root = BACKEND_ROOT.parent
        out = subprocess.run(
            ["git", "grep", "-l", "FORWARDED_ALLOW_IPS", "--",
             ".github", "Dockerfile", "Dockerfile.slim", "docker-compose.yml"],
            cwd=root, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "", (
            "FORWARDED_ALLOW_IPS appeared in deployment config; uvicorn's "
            "always-trust path takes the leftmost (spoofable) forwarded entry. "
            f"Found in: {out.stdout.strip()}"
        )
