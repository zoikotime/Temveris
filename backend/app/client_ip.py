"""Canonical client-IP resolution for a proxied deployment.

WHY THIS EXISTS
---------------
The app runs on Cloud Run, behind Google's front end, so the TCP peer the
container sees is the proxy — not the visitor. Two different wrong answers were
in use:

1. **SlowAPI** keyed rate limits on ``get_remote_address``, i.e.
   ``request.client.host`` alone. Behind a proxy that is the same value for
   every visitor, so every per-IP limit silently became a single global bucket:
   10 signups per hour *platform-wide*, then 429 for everyone.

2. **Roughly eighteen call sites** parsed ``X-Forwarded-For`` by hand and took
   ``[0]`` — the *leftmost* entry. Google **appends** the real peer to whatever
   the client sent, so the leftmost entry is attacker-controlled. Anything keyed
   on it (notably the fraud velocity buckets in
   ``app.commercial.routers._helpers.client_meta``) can be bypassed by sending a
   different ``X-Forwarded-For`` on each request.

WHY NOT ``FORWARDED_ALLOW_IPS=*``
---------------------------------
That looks like the one-line fix — let uvicorn's ``ProxyHeadersMiddleware``
rewrite ``request.client.host`` and everything downstream becomes correct. It is
not safe here. In uvicorn 0.30.6::

    def get_trusted_client_host(self, x_forwarded_for_hosts):
        if self.always_trust:
            return x_forwarded_for_hosts[0]     # leftmost

With ``*`` the middleware trusts the leftmost entry, which is exactly the
spoofable value described above. It would convert an over-restrictive limit into
no limit at all. (Uvicorn's *non*-always-trust path walks right-to-left and skips
known proxies, which is correct — but it needs the proxy's own address listed,
and Cloud Run does not give us a stable one to pin.)

THE RULE USED HERE
------------------
Read ``X-Forwarded-For`` right-to-left and skip ``TRUSTED_PROXY_HOPS`` trailing
entries. With the default of 0 that is the rightmost entry: the value the
infrastructure appended, which a client cannot forge by sending its own header.
Everything to the left may be attacker-supplied and is ignored.

Set ``TRUSTED_PROXY_HOPS`` above 0 if another appending proxy (e.g. an external
HTTPS load balancer) is added in front of Cloud Run; each such hop adds one
trailing entry.

Misconfiguring it degrades to a coarser bucket, never to a spoofable one:

* Too low — the offset lands on a trailing proxy address, so callers share a
  bucket. Safe, just imprecise.
* Too high — the offset would fall off the left end into the client-controlled
  prefix. That case deliberately ignores the header and uses the peer address,
  rather than clamping to ``hops[0]`` which is the attacker-supplied value.

Falls back to ``request.client.host`` when no forwarded header is present, which
is the correct answer for local development.

IPv6 is handled: entries are split on "," and an IPv6 address never contains a
comma, so bare (``2001:db8::1``) and bracketed (``[2001:db8::1]:443``) forms both
survive intact.
"""

from __future__ import annotations

import os
from typing import Optional

_DEFAULT_TRUSTED_PROXY_HOPS = 0


def _trusted_proxy_hops() -> int:
    """Trailing X-Forwarded-For entries to skip. Never negative."""
    raw = (os.getenv("TRUSTED_PROXY_HOPS") or "").strip()
    if not raw:
        return _DEFAULT_TRUSTED_PROXY_HOPS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_TRUSTED_PROXY_HOPS


def client_ip(request) -> Optional[str]:
    """Best available client address for this request, resistant to spoofing.

    Returns None only when there is neither a forwarded header nor a peer
    address (which in practice means a synthetic/test request).
    """
    if request is None:
        return None

    headers = getattr(request, "headers", None)
    forwarded = ""
    if headers is not None:
        try:
            forwarded = headers.get("x-forwarded-for") or ""
        except Exception:
            forwarded = ""

    if forwarded:
        # Splitting on "," is safe for IPv6: an address may contain colons and
        # brackets but never a comma, so entries never split mid-address.
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            # Walk in from the right by the number of proxies that append after
            # the real client. 0 hops -> rightmost.
            idx = len(hops) - 1 - _trusted_proxy_hops()
            if idx >= 0:
                return hops[idx]
            # More configured hops than entries present, so the header is
            # shorter than the topology claims. Every remaining entry is inside
            # the client-controlled prefix, so NONE of them can be trusted —
            # fall through to the peer address below.
            #
            # Clamping to hops[0] here instead would return the leftmost entry,
            # which is exactly the value an attacker supplies. That would make a
            # misconfigured hop count silently spoofable; falling back to the
            # peer only costs bucket granularity.
            pass

    # Some proxies send X-Real-IP instead. Single-valued, so nothing to choose.
    if headers is not None:
        try:
            real = (headers.get("x-real-ip") or "").strip()
        except Exception:
            real = ""
        if real:
            return real

    peer = getattr(request, "client", None)
    return getattr(peer, "host", None) if peer is not None else None


def rate_limit_key(request) -> str:
    """SlowAPI ``key_func``.

    SlowAPI requires a string key. ``get_remote_address`` falls back to
    "127.0.0.1"; we keep that behaviour so an unidentifiable request shares one
    bucket rather than escaping limits entirely.
    """
    return client_ip(request) or "127.0.0.1"
