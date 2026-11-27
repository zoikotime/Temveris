"""
Temveris - Confidence Scoring Engine
=====================================
Computes a 0-100 confidence score for a WorkRecord driven primarily by
real keyboard/mouse activity so that each record produces a realistic,
variable score instead of everyone getting the same number.

Scoring pipeline (applied in order):
  1. Start at 100
  2. Activity-based deductions (keyboard + mouse thresholds)
  3. Combined idle penalty (both keyboard + mouse very low)
  4. Short-duration penalty
  5. Missing-artifact penalty
  6. Small natural variation (±5, deterministic per record)
  7. Classify: >=75 validated, 50-74 warning, <50 flagged
  8. Clamp to 0..100

Note: Device, location, and replay checks continue to run inside
validation_service.py as fraud-event producers. This scoring engine
focuses purely on how confident we are that real work was performed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List

from sqlalchemy.orm import Session

from database import models


@dataclass
class ScoreResult:
    """Mutable result accumulator for the scoring pipeline."""
    score: int = 100
    status: str = "validated"          # validated | warning | flagged
    deductions: List[dict] = field(default_factory=list)

    def deduct(self, points: int, reason: str, category: str) -> None:
        # Do not clamp mid-pipeline; final clamp happens once at the end
        # so the cumulative deductions math stays honest.
        self.score -= points
        self.deductions.append({
            "points": points,
            "reason": reason,
            "category": category,
        })

    def adjust(self, delta: int, reason: str, category: str) -> None:
        """Signed adjustment (used for the ±5 natural variation step)."""
        self.score += delta
        self.deductions.append({
            "points": -delta,   # negative delta == positive score boost
            "reason": reason,
            "category": category,
        })

    def classify(self) -> None:
        if self.score >= 75:
            self.status = "validated"
        elif self.score >= 50:
            self.status = "warning"
        else:
            self.status = "flagged"


def compute_confidence(
    db: Session,
    record: models.WorkRecord,
) -> ScoreResult:
    """
    Score a WorkRecord based on real activity, duration, and artifact
    presence. Output is stable per-record (deterministic variation)
    while still producing realistic variance across records.
    """
    result = ScoreResult()

    kb       = int(record.keyboard_activity or 0)
    mouse    = int(record.mouse_activity or 0)
    duration = int(record.duration or 0)

    # ── STEP 2: Activity-based deductions ───────────────────────────────
    if kb < 10:
        result.deduct(30, f"Very low keyboard activity ({kb} keys)", "keyboard_very_low")
    elif kb <= 50:
        result.deduct(15, f"Low keyboard activity ({kb} keys)", "keyboard_low")

    if mouse < 5:
        result.deduct(25, f"Very low mouse activity ({mouse} moves)", "mouse_very_low")
    elif mouse <= 20:
        result.deduct(10, f"Low mouse activity ({mouse} moves)", "mouse_low")

    # ── STEP 3: Combined activity (possible idle/fake) ──────────────────
    if kb < 10 and mouse < 5:
        result.deduct(
            40,
            "Combined keyboard + mouse activity near zero — possible idle/fake work",
            "combined_idle",
        )

    # ── STEP 4: Duration check ──────────────────────────────────────────
    # Only penalize when a duration is actually set and it's suspiciously short.
    if 0 < duration < 20:
        result.deduct(20, f"Very short tracked duration ({duration}s)", "short_duration")

    # ── STEP 5: Artifact check ──────────────────────────────────────────
    if not record.artifact_url:
        result.deduct(30, "No screenshot / artifact attached", "missing_artifact")

    # ── STEP 5b: Tamper / replay penalty (-40) ──────────────────────────
    # Triggered by either "same screenshot reused" or "missing artifact hash".
    # Both signal that the evidence bundle cannot be trusted; a -40 hit drops a
    # clean record from 100 → 60 (warning) and stacks with other deductions to
    # push obviously fabricated records into the flagged zone.
    if record.artifact_hash:
        duplicate = (
            db.query(models.WorkRecord)
            .filter(
                models.WorkRecord.artifact_hash == record.artifact_hash,
                models.WorkRecord.id != record.id,
            )
            .first()
        )
        if duplicate:
            result.deduct(
                40,
                f"Tamper detected: artifact reused (duplicate of {duplicate.record_id})",
                "tampered_data",
            )
    else:
        # No hash at all → tampered or missing metadata.
        result.deduct(
            40,
            "Tamper detected: missing artifact hash (no verifiable evidence)",
            "tampered_data",
        )

    # ── STEP 6: Natural variation ±5 (deterministic per record) ─────────
    # Seeding by record_id means the same record always yields the same
    # score when re-validated, yet two different records with identical
    # activity profiles still differ slightly — which is what the user wants.
    seed_key = f"{record.record_id or ''}:{record.id or 0}"
    jitter = random.Random(seed_key).randint(-5, 5)
    result.adjust(jitter, f"Natural variation ({jitter:+d})", "variation")

    # ── STEP 8: Clamp to 0..100 ─────────────────────────────────────────
    result.score = max(0, min(100, result.score))

    # ── STEP 7: Classify (after clamp so thresholds are exact) ──────────
    result.classify()
    return result
