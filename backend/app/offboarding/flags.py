"""Feature flags / kill-switches (§8 — DevOps & deployment).

Env-driven toggles, read fresh on every call so an operator can flip them via
the platform's environment config and a rolling restart — no code deploy. They
exist for incident response, not normal operation: every flag DEFAULTS ON, so
forgetting to set them keeps the system fully hardened.

  OFFBOARDING_TELEMETRY_GATE_ENABLED  — the server-side post-exit telemetry gate.
        Kill ONLY if the gate is wrongly rejecting active staff during an
        incident; flipping it off re-opens the "zero post-exit tracking"
        invariant, so it must be a deliberate, audited, time-boxed decision.
  OFFBOARDING_SCHEDULER_ENABLED       — the background job loop. Off = no
        scheduled-closure / retry / reconciliation / conflict sweeps run
        in-process (use when an external scheduler owns them, or to quiesce a
        node during a migration).
  OFFBOARDING_ENFORCEMENT_ENABLED     — master switch; off implies both above
        off. Last-resort break-glass.
"""
from __future__ import annotations

import os


def _truthy(raw: str | None, default: bool = True) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def enforcement_enabled() -> bool:
    return _truthy(os.getenv("OFFBOARDING_ENFORCEMENT_ENABLED"), default=True)


def telemetry_gate_enabled() -> bool:
    return enforcement_enabled() and _truthy(
        os.getenv("OFFBOARDING_TELEMETRY_GATE_ENABLED"), default=True)


def scheduler_enabled() -> bool:
    return enforcement_enabled() and _truthy(
        os.getenv("OFFBOARDING_SCHEDULER_ENABLED"), default=True)


def snapshot() -> dict:
    """Current flag state — surfaced on the ops/metrics endpoint so the live
    configuration is observable (a silently-disabled gate is itself an incident)."""
    return {
        "enforcement_enabled": enforcement_enabled(),
        "telemetry_gate_enabled": telemetry_gate_enabled(),
        "scheduler_enabled": scheduler_enabled(),
    }
