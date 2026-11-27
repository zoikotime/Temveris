"""
The country-classification script's decision rules.

Migration ``m7n2p5q8r1t4`` deliberately backfills nothing, so
``scripts/holidays_country_report.py`` is what turns unscoped legacy holidays
into classified ones. Its judgement is what protects the two organizations the
audit found holding the wrong calendar — org 11 (Demo) and org 12 (ZR
Solutions), both ``primary_country = 'US'`` with the full Indian gazetted list.
A blanket backfill would have stamped US onto Diwali and made the error
permanent, so these tests pin the refusal, not just the happy path.

Pure functions only: no database, no writes.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parent.parent
           / "scripts" / "holidays_country_report.py")


@pytest.fixture(scope="module")
def report_module():
    spec = importlib.util.spec_from_file_location("holidays_country_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["holidays_country_report"] = module
    spec.loader.exec_module(module)
    return module


# Snapshots of what the two organizations actually hold, from the audit.
INDIAN_CALENDAR = [
    "New Year's Day", "Republic Day", "Maha Shivratri", "Holi", "Id ul Fitr",
    "Id ul Zuha (Bakrid)", "Independence Day", "Janmashtami",
    "Ganesh Chaturthi", "Mahatma Gandhi Jayanti", "Dussehra", "Diwali",
    "Christmas Day",
]
UAE_CALENDAR = [
    "New Year's Day", "Eid al Fitr", "Arafat Day", "Eid al Adha",
    "Islamic New Year", "Commemoration Day", "National Day",
]


# ── Inference ────────────────────────────────────────────────────────────────
def test_an_indian_calendar_is_recognised_as_indian(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country(INDIAN_CALENDAR, index) == "IN"


def test_a_uae_calendar_is_recognised_as_uae(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country(UAE_CALENDAR, index) == "AE"


def test_universally_shared_names_carry_no_signal(report_module):
    """New Year's Day and Christmas appear in several calendars. A list of only
    those must NOT be attributed to a country."""
    index = report_module._curated_name_index()
    assert report_module._infer_country(["New Year's Day", "Christmas"], index) is None


def test_a_bespoke_company_calendar_is_not_forced_into_a_country(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country(
        ["Founders Day", "Summer Shutdown", "Q4 Offsite"], index) is None


def test_a_mostly_unknown_calendar_falls_below_the_threshold(report_module):
    """One recognisable name among many custom ones is not enough evidence."""
    index = report_module._curated_name_index()
    names = ["Diwali"] + [f"Custom Day {i}" for i in range(10)]
    assert report_module._infer_country(names, index) is None


def test_a_real_calendar_padded_with_custom_days_is_still_recognised(report_module):
    """An organization that added its own days to the gazetted list has not
    stopped being Indian. The threshold is measured against the INFORMATIVE
    names, so padding cannot dilute a genuine calendar below it."""
    index = report_module._curated_name_index()
    names = INDIAN_CALENDAR + ["Founders Day", "Summer Shutdown", "Q4 Offsite"]
    assert report_module._infer_country(names, index) == "IN"


def test_christmas_alone_is_not_indian(report_module):
    """Regression: Christmas appears in exactly one curated calendar (India's),
    so a purely computed 'shared' set scored it as India-distinctive."""
    index = report_module._curated_name_index()
    assert report_module._infer_country(["Christmas", "Christmas Day"], index) is None


def test_a_single_distinctive_match_is_a_coincidence_not_evidence(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country(["Diwali"], index) is None
    assert report_module._infer_country(["Diwali", "Holi"], index) is None


def test_empty_input_infers_nothing(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country([], index) is None


def test_inference_is_case_insensitive(report_module):
    index = report_module._curated_name_index()
    assert report_module._infer_country(
        [n.upper() for n in INDIAN_CALENDAR], index) == "IN"


# ── Verdicts ─────────────────────────────────────────────────────────────────
def test_declared_and_inferred_agreeing_is_safe(report_module):
    assert report_module._classify("IN", "IN", unscoped=13) == report_module.VERDICT_AGREED


def test_declared_with_no_inference_is_safe(report_module):
    """A custom calendar in an organization that declared a country: nothing
    contradicts the declaration, so it may be adopted."""
    assert report_module._classify("US", None, unscoped=5) == report_module.VERDICT_AGREED


def test_the_audits_broken_organizations_are_refused(report_module):
    """org 11 / org 12: declared US, calendar is unmistakably Indian."""
    verdict = report_module._classify("US", "IN", unscoped=13)
    assert verdict == report_module.VERDICT_MISMATCH


def test_an_undeclared_but_unmistakable_calendar_needs_a_decision(report_module):
    assert report_module._classify(None, "IN", unscoped=13) == report_module.VERDICT_MISMATCH


def test_no_country_and_no_signal_is_unknown(report_module):
    assert report_module._classify(None, None, unscoped=7) == report_module.VERDICT_UNKNOWN


def test_already_classified_rows_are_left_alone(report_module):
    assert report_module._classify("IN", "IN", unscoped=0) == report_module.VERDICT_SCOPED


@pytest.mark.parametrize("verdict", ["MISMATCH", "UNKNOWN"])
def test_only_agreed_organizations_are_ever_written(report_module, verdict):
    """The apply path filters on AGREED and nothing else — this is the guard
    that keeps a wrong country off Diwali."""
    import inspect
    source = inspect.getsource(report_module.apply_backfill)
    assert "VERDICT_AGREED" in source
    assert f'VERDICT_{verdict}' not in source


def test_every_verdict_has_an_operator_note(report_module):
    for verdict in (report_module.VERDICT_SCOPED, report_module.VERDICT_AGREED,
                    report_module.VERDICT_MISMATCH, report_module.VERDICT_UNKNOWN):
        assert report_module._VERDICT_NOTE[verdict]


def test_render_is_ascii_safe(report_module):
    """Ops scripts run on Windows consoles using cp1252, where a stray arrow
    glyph crashes the whole report with UnicodeEncodeError."""
    rows = [{
        "organization_id": 11, "organization_name": "Demo",
        "declared_country": "US", "inferred_country": "IN",
        "total_holidays": 13, "unscoped_holidays": 13,
        "existing_country_codes": [], "years": [2026],
        "verdict": report_module.VERDICT_MISMATCH,
        "sample_names": INDIAN_CALENDAR[:6],
    }]
    rendered = report_module.render(rows)
    rendered.encode("cp1252")  # raises if a non-encodable glyph slipped in
    assert "Demo" in rendered
    assert "MISMATCH" in rendered


def test_render_handles_an_empty_database(report_module):
    assert "No organizations" in report_module.render([])
