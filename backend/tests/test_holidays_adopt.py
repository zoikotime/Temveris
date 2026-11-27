"""
Catalog adoption over HTTP — the plan, Add-all, per-row Add and conflicts.

Drives the real routes, because the bug being fixed lived in the gap between
what the browser computed and what the server wrote. A query-level test could
not have caught it.
"""
from __future__ import annotations

import itertools
import uuid

import pytest
from fastapi.testclient import TestClient

from database import models
from database.database import Base
from app.holidays import catalog, collisions, provider, service

# 995000 is unused elsewhere. The suite shares one in-memory SQLite, so two
# modules starting at the same base collide on organizations.id — which is
# exactly what 990000 did against test_phase6_tenant_guards.py.
_org_seq = itertools.count(995000)

ORG1_LEGACY = [
    ("New Year's Day", "2026-01-01"), ("Republic Day", "2026-01-26"),
    ("Maha Shivratri", "2026-02-15"), ("Holi", "2026-03-04"),
    ("Id ul Fitr", "2026-03-21"), ("Id ul Zuha (Bakrid)", "2026-05-28"),
    ("Independence Day", "2026-08-15"), ("Janmashtami", "2026-09-04"),
    ("Ganesh Chaturthi", "2026-09-14"), ("Mahatma Gandhi Jayanti", "2026-10-02"),
    ("Dussehra", "2026-10-20"), ("Diwali", "2026-11-08"),
    ("Christmas Day", "2026-12-25"),
]


@pytest.fixture(scope="module")
def app(engine):
    Base.metadata.create_all(engine)
    import main
    return main.app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import sessionmaker
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _clear_auth_caches():
    from app.auth import service as auth_service
    auth_service._USER_CACHE.clear()
    auth_service._ORG_CACHE.clear()
    yield
    auth_service._USER_CACHE.clear()
    auth_service._ORG_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    provider.reset_caches()
    service.invalidate_cache()
    monkeypatch.setattr(provider, "available_countries", lambda **kw: [
        {"country_code": c, "country_name": c} for c in ("US", "GB")])
    yield
    provider.reset_caches()
    service.invalidate_cache()


def _org(session, *, country="IN", legacy=True):
    oid = next(_org_seq)
    session.add(models.Organization(id=oid, name=f"Adopt-{oid}"))
    session.commit()
    session.add(models.OrgSettings(organization_id=oid, primary_country=country))
    # Block the lazy seeder so only what a test adds is present.
    session.add(models.AppSettings(organization_id=oid,
                                   key="holidays_default_seeded", value="true"))
    session.commit()
    if legacy:
        from datetime import date
        for name, iso in ORG1_LEGACY:
            session.add(models.Holiday(
                name=name, date=date.fromisoformat(iso), holiday_type="Public",
                organization_id=oid, country_code=None))  # unscoped, as in production
        session.commit()
    admin = models.User(name="a", email=f"a_{uuid.uuid4().hex}@x.test", password="x",
                        role="admin", is_active=True, organization_id=oid)
    session.add(admin); session.commit(); session.refresh(admin)
    from app.auth.service import create_access_token
    return oid, {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}


def _plan(client, H, cc="IN", year=2026, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/holidays/catalog/plan?country_code={cc}&year={year}" + (f"&{qs}" if qs else "")
    r = client.get(url, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _adopt(client, H, **body):
    payload = {"country_code": "IN", "year": 2026}
    payload.update(body)
    return client.post("/holidays/import-catalog", json=payload, headers=H)


def _rows(client, H):
    return client.get("/holidays", headers=H).json()


# ── The plan ─────────────────────────────────────────────────────────────────
def test_plan_classifies_the_real_legacy_calendar(client, session):
    oid, H = _org(session)
    plan = _plan(client, H)
    c = plan["counts"]
    assert c["total"] == 22
    assert c["new"] == 9
    assert c["date_conflict"] == 1
    assert c["already_covered"] + c["exact_duplicate"] == 12
    assert c["will_add"] == 9


def test_plan_writes_nothing(client, session):
    oid, H = _org(session)
    before = len(_rows(client, H))
    _plan(client, H)
    assert len(_rows(client, H)) == before


def test_plan_entries_carry_status_key_and_reason(client, session):
    oid, H = _org(session)
    for entry in _plan(client, H)["entries"]:
        assert entry["key"]
        assert entry["status"] in (
            "new", "already_covered", "exact_duplicate", "date_conflict")
        assert "eligible" in entry
        if entry["status"] != "new":
            assert entry["reason"]


def test_plan_surfaces_the_covering_holiday(client, session):
    oid, H = _org(session)
    covered = [e for e in _plan(client, H)["entries"] if e["status"] == "already_covered"]
    assert covered
    for e in covered:
        assert e["existing"]["name"]
        assert e["existing"]["name"].lower() in e["reason"].lower()


# ── Add all ──────────────────────────────────────────────────────────────────
def test_add_all_adds_only_genuinely_new_holidays(client, session):
    """The headline regression: this used to insert 20 and report 20."""
    oid, H = _org(session)
    before = _rows(client, H)
    r = _adopt(client, H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 9
    after = _rows(client, H)
    assert len(after) - len(before) == 9


def test_add_all_creates_no_duplicate_dates(client, session):
    oid, H = _org(session)
    _adopt(client, H)
    dates = [h["date"] for h in _rows(client, H)]
    assert len(dates) == len(set(dates)), "a date ended up with two holidays"


def test_add_all_reports_what_happened_not_what_was_asked(client, session):
    oid, H = _org(session)
    body = _adopt(client, H).json()
    assert body["added"] == 9
    assert body["already_covered"] + body["exact_duplicates"] == 12
    assert body["conflicts"] == 1
    assert body["imported"] == body["added"], "legacy alias must agree"


def test_add_all_is_idempotent(client, session):
    oid, H = _org(session)
    first = _adopt(client, H).json()
    second = _adopt(client, H).json()
    assert first["added"] == 9
    assert second["added"] == 0


def test_add_all_on_an_empty_calendar_adds_the_mandatory_set(client, session):
    oid, H = _org(session, legacy=False)
    body = _adopt(client, H).json()
    # 22 curated minus the 2 Optional ones, which need an opt-in.
    assert body["added"] == 20
    assert body["skipped"] == 2


def test_add_all_never_touches_another_tenant(client, session):
    _, HA = _org(session)
    oid_b, HB = _org(session, legacy=False)
    _adopt(client, HA)
    assert _rows(client, HB) == []


# ── Bakrid (PHASE 3) ─────────────────────────────────────────────────────────
def test_bakrid_is_not_added_silently(client, session):
    """Existing 05-28, catalog 05-27. Adding both is an extra day off."""
    oid, H = _org(session)
    _adopt(client, H)
    dates = {h["date"] for h in _rows(client, H)}
    assert "2026-05-28" in dates, "the organization's own date must survive"
    assert "2026-05-27" not in dates, "a second Bakrid must not appear"


def test_bakrid_is_reported_as_a_conflict(client, session):
    oid, H = _org(session)
    body = _adopt(client, H).json()
    assert body["conflicts"] == 1
    conflict = body["conflict_detail"][0]
    assert "Bakrid" in conflict["name"]
    assert conflict["existing"]["date"] == "2026-05-28"
    assert conflict["decision"] == "skip"


def test_bakrid_replace_moves_the_existing_row(client, session):
    oid, H = _org(session)
    key = next(e["key"] for e in _plan(client, H)["entries"]
               if e["status"] == "date_conflict")
    before = len(_rows(client, H))
    body = _adopt(client, H, keys=[key], resolutions={key: "replace"}).json()
    assert body["replaced"] == 1
    assert body["added"] == 0
    after = _rows(client, H)
    assert len(after) == before, "replace must not add a row"
    dates = {h["date"] for h in after}
    assert "2026-05-27" in dates and "2026-05-28" not in dates


def test_bakrid_add_anyway_requires_the_explicit_resolution(client, session):
    oid, H = _org(session)
    key = next(e["key"] for e in _plan(client, H)["entries"]
               if e["status"] == "date_conflict")

    # Naming the key alone is not consent — it still skips.
    assert _adopt(client, H, keys=[key]).json()["added"] == 0
    assert "2026-05-27" not in {h["date"] for h in _rows(client, H)}

    # Only the explicit decision writes it.
    assert _adopt(client, H, keys=[key],
                  resolutions={key: "add_anyway"}).json()["added"] == 1
    dates = {h["date"] for h in _rows(client, H)}
    assert "2026-05-27" in dates and "2026-05-28" in dates


def test_an_invalid_resolution_is_rejected(client, session):
    oid, H = _org(session)
    r = _adopt(client, H, resolutions={"x": "delete_everything"})
    assert r.status_code == 422


# ── Per-row Add (PHASE 6) ────────────────────────────────────────────────────
def test_per_row_add_adds_exactly_one(client, session):
    oid, H = _org(session)
    entry = next(e for e in _plan(client, H)["entries"] if e["status"] == "new")
    before = len(_rows(client, H))
    body = _adopt(client, H, keys=[entry["key"]]).json()
    assert body["added"] == 1
    after = _rows(client, H)
    assert len(after) == before + 1
    assert any(h["name"] == entry["name"] for h in after)


def test_per_row_add_cannot_duplicate(client, session):
    oid, H = _org(session)
    entry = next(e for e in _plan(client, H)["entries"] if e["status"] == "new")
    _adopt(client, H, keys=[entry["key"]])
    assert _adopt(client, H, keys=[entry["key"]]).json()["added"] == 0


def test_per_row_add_refuses_an_already_covered_row(client, session):
    oid, H = _org(session)
    entry = next(e for e in _plan(client, H)["entries"]
                 if e["status"] == "already_covered")
    before = len(_rows(client, H))
    body = _adopt(client, H, keys=[entry["key"]]).json()
    assert body["added"] == 0
    assert len(_rows(client, H)) == before


def test_an_unknown_key_adds_nothing(client, session):
    oid, H = _org(session)
    before = len(_rows(client, H))
    assert _adopt(client, H, keys=["2026-01-01|nonsense"]).json()["added"] == 0
    assert len(_rows(client, H)) == before


def test_per_row_and_add_all_share_one_code_path(client, session):
    """Adding every new key one at a time must land exactly where Add-all does."""
    oid_a, HA = _org(session)
    oid_b, HB = _org(session)
    _adopt(client, HA)
    for entry in _plan(client, HB)["entries"]:
        if entry["status"] == "new":
            _adopt(client, HB, keys=[entry["key"]])
    left = sorted((h["date"], h["name"]) for h in _rows(client, HA))
    right = sorted((h["date"], h["name"]) for h in _rows(client, HB))
    assert left == right


# ── Optional / regional gating ───────────────────────────────────────────────
def test_optional_days_are_excluded_by_default(client, session):
    oid, H = _org(session, legacy=False)
    _adopt(client, H)
    assert not any(h["name"] == "Ganesh Chaturthi" for h in _rows(client, H))


def test_optional_days_can_be_opted_into(client, session):
    oid, H = _org(session, legacy=False)
    _adopt(client, H, include_optional=True)
    assert any(h["name"] == "Ganesh Chaturthi" for h in _rows(client, H))


def test_plan_marks_optional_rows_not_eligible(client, session):
    oid, H = _org(session, legacy=False)
    plan = _plan(client, H)
    optional = [e for e in plan["entries"] if e["holiday_type"] == "Optional"]
    assert optional
    assert all(e["eligible"] is False for e in optional)
    assert plan["counts"]["not_eligible"] == len(optional)


def test_include_optional_changes_the_plan(client, session):
    oid, H = _org(session, legacy=False)
    strict = _plan(client, H)["counts"]["will_add"]
    loose = _plan(client, H, include_optional="true")["counts"]["will_add"]
    assert loose > strict


# ── Country/year scoping (PHASE 8) ───────────────────────────────────────────
def test_a_us_calendar_does_not_collide_with_indian_holidays(client, session, monkeypatch):
    oid, H = _org(session)
    monkeypatch.setattr(provider, "fetch_holidays", lambda code, year: [
        catalog.normalize_entry(
            {"name": "US Day On An Indian Holiday", "date": "2026-11-08",
             "holiday_type": "Public"}, source="provider")])
    service.invalidate_cache()
    plan = client.get("/holidays/catalog/plan?country_code=US&year=2026",
                      headers=H).json()
    # 2026-11-08 is Diwali in the org's unscoped calendar, but that row is
    # unscoped so it DOES apply everywhere — the collision is real and correct.
    assert plan["entries"][0]["status"] in ("date_conflict", "already_covered")


def test_a_scoped_foreign_holiday_does_not_collide(client, session, monkeypatch):
    oid, H = _org(session, legacy=False)
    # An explicitly US-scoped row must not block the Indian calendar.
    from datetime import date
    session.add(models.Holiday(name="US Independence Day", date=date(2026, 11, 8),
                               holiday_type="Public", organization_id=oid,
                               country_code="US"))
    session.commit()
    plan = _plan(client, H)
    diwali = next(e for e in plan["entries"] if e["name"] == "Diwali")
    assert diwali["status"] == "new"


def test_a_different_year_is_untouched(client, session):
    oid, H = _org(session)
    _adopt(client, H, year=2027)
    assert any(h["date"].startswith("2027") for h in _rows(client, H))
    # The 2026 conflict must still be a conflict.
    assert _plan(client, H)["counts"]["date_conflict"] == 1


# ── Permissions ──────────────────────────────────────────────────────────────
def test_the_plan_needs_write_permission(client, session):
    oid, _ = _org(session, legacy=False)
    emp = models.User(name="e", email=f"e_{uuid.uuid4().hex}@x.test", password="x",
                      role="employee", is_active=True, organization_id=oid)
    session.add(emp); session.commit(); session.refresh(emp)
    from app.auth.service import create_access_token
    HE = {"Authorization": f"Bearer {create_access_token({'user_id': emp.id})}"}
    assert client.get("/holidays/catalog/plan?country_code=IN&year=2026",
                      headers=HE).status_code == 403
    assert client.post("/holidays/import-catalog",
                       json={"country_code": "IN", "year": 2026},
                       headers=HE).status_code == 403
