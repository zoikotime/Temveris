"""Shared API rate limiter (slowapi).

Defined in its own module so routers (auth, employees) can import the limiter
without creating a circular dependency back to main.py.

Keyed by client IP via `app.client_ip.rate_limit_key` — NOT slowapi's
`get_remote_address`. That helper returns `request.client.host` alone, which
behind Cloud Run is the Google front end's address and therefore identical for
every visitor. Every per-IP limit silently collapsed into one global bucket:
`10/hour` on signup meant ten signups per hour *platform-wide*, then 429 for
everyone. Local development never shows this — one client, one address.

`app.client_ip` reads the forwarded header right-to-left so the value cannot be
forged by a client sending its own `X-Forwarded-For`; see that module for why
`FORWARDED_ALLOW_IPS=*` is the wrong fix here.

NOTE on tuning: many employees typically sit behind a single office NAT/public
IP, so per-IP limits must stay generous enough that a normal morning login rush
— and the desktop tracker's screenshot/heartbeat/poll traffic — never trips
them. The global default below is a DoS guard, not a per-user throttle; auth
routes apply their own stricter per-endpoint limits via @limiter.limit(...).

/health is exempt via exempt_when so it never returns 429 regardless of load —
the @limiter.exempt decorator is unreliable with SlowAPIMiddleware because the
middleware intercepts the request before FastAPI resolves the route function.
"""
from slowapi import Limiter

from app.client_ip import rate_limit_key

limiter = Limiter(key_func=rate_limit_key, default_limits=[])
