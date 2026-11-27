"""
The ECO v2 calculation driver (ZT-SPEC-ECO-002 v2.0 §44.2).
===========================================================

The invocation that was missing. Every stage of the ECO v2 chain was built and
covered by executable golden vectors, but nothing in the running application
called any of it — so §44.2's shadow period could produce no data.

    producer -> EVAL-DAY -> (§24.3 alert) -> EVAL-EVENT -> ALERT

This package orchestrates those existing primitives and does nothing else: no
arithmetic, no requirement resolution, no classification, no shortfall, no
weekly total, no threshold comparison. ``service.py`` says at length what it
must never do, and ``tests/test_eco_driver.py`` enforces it with an AST scan
rather than a promise.

It owns no table and no migration. It persists nothing of its own, because
every stage already persists its own authoritative artifact under its own
identity — which is also why no orchestration-level idempotency record exists.
"""

from .outcomes import (
    STAGE_ORDER, CalculationPassResult, DriverStage, WorkerDayResult,
)
from .service import (
    DRIVER_ACTOR, run_eco_calculation_pass, run_worker_day, select_workers,
)

__all__ = [
    "CalculationPassResult",
    "DRIVER_ACTOR",
    "DriverStage",
    "STAGE_ORDER",
    "WorkerDayResult",
    "run_eco_calculation_pass",
    "run_worker_day",
    "select_workers",
]
