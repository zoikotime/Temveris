"""Typed refusals for the break-treatment profile. Mirrors POLICY-VERSION."""

from __future__ import annotations


class BreakTreatmentError(Exception):
    """Base for every refusal this package raises."""


class InvalidBreakTreatmentProfile(BreakTreatmentError):
    """A value outside its governed domain, refused BEFORE anything is added
    to the session so a rejected capture leaves the store byte-identical."""


class BreakTreatmentImmutable(BreakTreatmentError):
    """An attempt to UPDATE or DELETE a captured profile.

    A profile is evidence about which treatment was in force on a date. A
    correction is a NEW version, never a rewrite of the old one — otherwise a
    historical evaluation's stated basis could change under it.
    """


class BreakTreatmentProfileCollision(BreakTreatmentError):
    """Two captures claiming one identity, or one date, for one organization."""


__all__ = [
    "BreakTreatmentError",
    "InvalidBreakTreatmentProfile",
    "BreakTreatmentImmutable",
    "BreakTreatmentProfileCollision",
]
