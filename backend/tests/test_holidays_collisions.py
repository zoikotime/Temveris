"""
Catalog adoption collision detection.

The production bug these pin: ``find_duplicate`` matches ``country_code``
exactly, so adopting "IN" never matched the pre-migration rows (all
``country_code IS NULL``). Against organization 1's real 13-row calendar the UI
offered "13 available to add" and the server inserted **20**, leaving 33 rows
with 10 duplicated dates — including a second Bakrid on an adjacent day, which
is an extra non-working day for every employee.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import date

import pytest

from database import models
from database.database import Base
from app.holidays import collisions

_org_seq = itertools.count(660000)

# Organization 1's real calendar, verbatim from the production read-only query.
ORG1_LEGACY = [
    ("New Year's Day", "2026-01-01"), ("Republic Day", "2026-01-26"),
    ("Maha Shivratri", "2026-02-15"), ("Holi", "2026-03-04"),
    ("Id ul Fitr", "2026-03-21"), ("Id ul Zuha (Bakrid)", "2026-05-28"),
    ("Independence Day", "2026-08-15"), ("Janmashtami", "2026-09-04"),
    ("Ganesh Chaturthi", "2026-09-14"), ("Mahatma Gandhi Jayanti", "2026-10-02"),
    ("Dussehra", "2026-10-20"), ("Diwali", "2026-11-08"),
    ("Christmas Day", "2026-12-25"),
]


@pytest.fixture(scope="module", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db(engine):
    from sqlalchemy.orm import sessionmaker
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _org(db, country="IN"):
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Collide-{oid}"))
    db.commit()
    db.add(models.OrgSettings(organization_id=oid, primary_country=country))
    db.commit()
    return oid


def _add(db, oid, name, iso, country=None):
    db.add(models.Holiday(name=name, date=date.fromisoformat(iso),
                          holiday_type="Public", organization_id=oid,
                          country_code=country))
    db.commit()


def _entry(name, iso, holiday_type="Public", **kw):
    payload = {"name": name, "date": iso, "holiday_type": holiday_type,
               "description": "", "estimated": False, "regional": False, "regions": []}
    payload.update(kw)
    return payload


def _classify(db, oid, entries, country="IN"):
    return collisions.classify_entries(db, oid, country, entries)


# ── Name matching ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("Id ul Fitr", "Eid-ul-Fitr"),
    ("Mahatma Gandhi Jayanti", "Gandhi Jayanti"),
    ("Christmas Day", "Christmas"),
    ("Id ul Zuha (Bakrid)", "Eid-ul-Adha (Bakrid)"),
    ("Independence Day", "independence day"),
    ("Republic  Day ", "Republic Day"),
])
def test_recognises_the_same_observance(a, b):
    assert collisions.same_observance(a, b)


@pytest.mark.parametrize("a,b", [
    ("Republic Day", "Independence Day"),
    ("Gandhi Jayanti", "Dussehra"),
    # Two different Eids — a shared short fragment must NOT merge them.
    ("Eid-ul-Fitr", "Eid-ul-Adha"),
    ("Holi", "Diwali"),
    ("Good Friday", "Ram Navami"),
])
def test_keeps_different_observances_apart(a, b):
    assert not collisions.same_observance(a, b)


def test_empty_names_never_match():
    assert not collisions.same_observance("", "Diwali")
    assert not collisions.same_observance("Diwali", "")


# ── The four verdicts ────────────────────────────────────────────────────────
def test_no_existing_holiday_is_new(db):
    oid = _org(db)
    [r] = _classify(db, oid, [_entry("Diwali", "2026-11-08")])
    assert r["status"] == collisions.STATUS_NEW
    assert r["existing"] is None


def test_exact_duplicate(db):
    oid = _org(db)
    _add(db, oid, "Diwali", "2026-11-08", country="IN")
    [r] = _classify(db, oid, [_entry("Diwali", "2026-11-08")])
    assert r["status"] == collisions.STATUS_EXACT_DUPLICATE
    assert r["existing"]["name"] == "Diwali"


def test_unscoped_legacy_row_is_still_a_duplicate(db):
    """THE BUG. A NULL country_code row is invisible to find_duplicate but
    absolutely applies to the employee, so it must count as a collision."""
    oid = _org(db)
    _add(db, oid, "Diwali", "2026-11-08", country=None)
    [r] = _classify(db, oid, [_entry("Diwali", "2026-11-08")])
    assert r["status"] == collisions.STATUS_EXACT_DUPLICATE

    # And prove the old check really does miss it.
    from app.holidays import org_calendar
    assert org_calendar.find_duplicate(
        db, oid, date(2026, 11, 8), "Diwali", "IN") is None


def test_same_date_similar_name_is_already_covered(db):
    oid = _org(db)
    _add(db, oid, "Id ul Fitr", "2026-03-21", country=None)
    [r] = _classify(db, oid, [_entry("Eid-ul-Fitr", "2026-03-21")])
    assert r["status"] == collisions.STATUS_ALREADY_COVERED
    assert "Id ul Fitr" in r["reason"]
    assert r["existing"]["name"] == "Id ul Fitr"


def test_same_date_unrelated_name_is_a_conflict(db):
    """Gandhi Jayanti and Dussehra genuinely shared 2025-10-02. Two real
    holidays on one date is not 'already covered' — the admin decides."""
    oid = _org(db)
    _add(db, oid, "Gandhi Jayanti", "2025-10-02", country="IN")
    [r] = _classify(db, oid, [_entry("Dussehra", "2025-10-02")])
    assert r["status"] == collisions.STATUS_DATE_CONFLICT
    assert r["existing"]["name"] == "Gandhi Jayanti"


# ── Bakrid: the lunar-drift case (PHASE 3) ───────────────────────────────────
def test_bakrid_adjacent_date_is_a_conflict_not_new(db):
    """Existing 2026-05-28, catalog 2026-05-27. Silently adding the catalog date
    would give the organization TWO Bakrid non-working days."""
    oid = _org(db)
    _add(db, oid, "Id ul Zuha (Bakrid)", "2026-05-28", country=None)
    [r] = _classify(db, oid, [
        _entry("Eid-ul-Adha (Bakrid)", "2026-05-27", estimated=True)])
    assert r["status"] == collisions.STATUS_DATE_CONFLICT
    assert r["existing"]["date"] == "2026-05-28"
    assert "extra non-working day" in r["reason"]


@pytest.mark.parametrize("offset", [-3, -2, -1, 1, 2, 3])
def test_same_observance_within_the_drift_window_is_a_conflict(db, offset):
    oid = _org(db)
    _add(db, oid, "Id ul Zuha (Bakrid)", "2026-05-28", country=None)
    from datetime import timedelta
    target = (date(2026, 5, 28) + timedelta(days=offset)).isoformat()
    [r] = _classify(db, oid, [_entry("Eid-ul-Adha (Bakrid)", target)])
    assert r["status"] == collisions.STATUS_DATE_CONFLICT


def test_far_apart_same_name_is_new_not_a_conflict(db):
    """A month away is a different occurrence, not lunar drift."""
    oid = _org(db)
    _add(db, oid, "Id ul Zuha (Bakrid)", "2026-05-28", country=None)
    [r] = _classify(db, oid, [_entry("Eid-ul-Adha (Bakrid)", "2026-07-15")])
    assert r["status"] == collisions.STATUS_NEW


# ── Country and year scoping (PHASE 8) ───────────────────────────────────────
def test_a_us_holiday_does_not_collide_with_an_indian_one(db):
    oid = _org(db)
    _add(db, oid, "Independence Day", "2026-07-04", country="US")
    [r] = _classify(db, oid, [_entry("Some Indian Holiday", "2026-07-04")], country="IN")
    assert r["status"] == collisions.STATUS_NEW, "US calendar must not block IN"


def test_the_same_country_does_collide(db):
    oid = _org(db)
    _add(db, oid, "Independence Day", "2026-07-04", country="US")
    [r] = _classify(db, oid, [_entry("Independence Day", "2026-07-04")], country="US")
    assert r["status"] == collisions.STATUS_EXACT_DUPLICATE


def test_an_unscoped_row_collides_with_every_country(db):
    """NULL means 'applies to the whole organization', so it must block both."""
    oid = _org(db)
    _add(db, oid, "Company Shutdown", "2026-07-04", country=None)
    for cc in ("US", "IN"):
        [r] = _classify(db, oid, [_entry("Company Shutdown", "2026-07-04")], country=cc)
        assert r["status"] == collisions.STATUS_EXACT_DUPLICATE, cc


def test_a_different_year_never_collides(db):
    oid = _org(db)
    _add(db, oid, "Diwali", "2025-10-20", country="IN")
    [r] = _classify(db, oid, [_entry("Diwali", "2026-11-08")])
    assert r["status"] == collisions.STATUS_NEW


def test_another_tenants_calendar_is_invisible(db):
    a, b = _org(db), _org(db)
    _add(db, a, "Diwali", "2026-11-08", country="IN")
    [r] = _classify(db, b, [_entry("Diwali", "2026-11-08")])
    assert r["status"] == collisions.STATUS_NEW


# ── The whole org-1 calendar ─────────────────────────────────────────────────
def test_org1_real_calendar_classifies_correctly(db):
    """End-to-end against the exact production data that exposed the bug."""
    from app.holidays import catalog
    oid = _org(db)
    for name, iso in ORG1_LEGACY:
        _add(db, oid, name, iso, country=None)

    entries = catalog.curated_calendar("IN", 2026)["holidays"]
    classified = _classify(db, oid, entries)
    counts = collisions.summarize(classified)

    assert counts["total"] == len(entries)
    # Everything the organization already observes is caught, one way or another.
    assert counts[collisions.STATUS_NEW] == 9
    assert counts[collisions.STATUS_DATE_CONFLICT] == 1
    settled = (counts[collisions.STATUS_EXACT_DUPLICATE]
               + counts[collisions.STATUS_ALREADY_COVERED])
    assert settled == 12

    conflict = next(c for c in classified
                    if c["status"] == collisions.STATUS_DATE_CONFLICT)
    assert "Bakrid" in conflict["name"]

    # No date the organization already has may be classified NEW — that is
    # precisely what produced the duplicate rows.
    have = {iso for _, iso in ORG1_LEGACY}
    for item in classified:
        if item["status"] == collisions.STATUS_NEW:
            assert item["date"] not in have, item


def test_summarize_totals_add_up(db):
    from app.holidays import catalog
    oid = _org(db)
    for name, iso in ORG1_LEGACY:
        _add(db, oid, name, iso, country=None)
    classified = _classify(db, oid, catalog.curated_calendar("IN", 2026)["holidays"])
    counts = collisions.summarize(classified)
    assert (counts[collisions.STATUS_NEW]
            + counts[collisions.STATUS_ALREADY_COVERED]
            + counts[collisions.STATUS_EXACT_DUPLICATE]
            + counts[collisions.STATUS_DATE_CONFLICT]) == counts["total"]


# ── Keys ─────────────────────────────────────────────────────────────────────
def test_entry_key_is_stable_and_name_normalised():
    a = collisions.entry_key({"date": "2026-11-08", "name": "Diwali"})
    b = collisions.entry_key({"date": "2026-11-08", "name": " diwali "})
    assert a == b
    assert a != collisions.entry_key({"date": "2026-11-09", "name": "Diwali"})


def test_malformed_dates_are_dropped_not_crashed(db):
    oid = _org(db)
    out = _classify(db, oid, [_entry("Bad", "not-a-date"), _entry("Good", "2026-01-05")])
    assert [o["name"] for o in out] == ["Good"]


def test_empty_input(db):
    assert _classify(db, _org(db), []) == []
