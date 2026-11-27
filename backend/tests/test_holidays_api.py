"""
Holiday HTTP API — CRUD, country scoping, permissions and tenant isolation.

Drives the real FastAPI app so the ROUTER's behaviour is covered, not just the
query helpers: the audit's delete bug (200 "Holiday deleted" when nothing was
deleted) was invisible to every query-level test.

The external provider is stubbed in every test that could reach it, so the
suite runs offline and does not depend on Nager.Date's uptime.
"""
from __future__ import annotations

import io
import itertools
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from database import models
from database.database import Base
from app.holidays import catalog, provider, service

_org_seq = itertools.count(770000)


@pytest.fixture(scope="module")
def app(engine):
    Base.metadata.create_all(engine)
    import main
    return main.app


@pytest.fixture(scope="module")
def client(app):
    """No `with` on purpose — that would run the app's lifespan.

    main.py's startup does a dozen `create_table(checkfirst=True)` calls and
    schedules background tasks; running it inside the shared test process is
    both unnecessary for route testing and intermittently fails, which surfaces
    as an ERROR at the setup of whichever test happens to be first in this
    module. Matches tests/test_phase3_cross_tenant_http.py and
    tests/test_billing_endpoint_tenant_isolation.py.
    """
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


def _make_org(session, *, country="IN", seeded=True):
    """A tenant with users at every role we care about.

    `seeded` writes the seed marker so the lazy default-calendar seeding cannot
    fire and add rows a test did not ask for.
    """
    oid = next(_org_seq)
    session.add(models.Organization(id=oid, name=f"HolidayOrg-{oid}"))
    session.add(models.OrgSettings(organization_id=oid, primary_country=country))
    if seeded:
        session.add(models.AppSettings(
            organization_id=oid, key="holidays_default_seeded", value="true"))
    session.commit()

    users = {}
    for role in ("admin", "employee", "hr", "owner", "super_admin", "billing_admin"):
        u = models.User(
            name=f"{role}-{oid}", email=f"{role}_{uuid.uuid4().hex}@x.test",
            password="x", role=role, is_active=True, organization_id=oid)
        session.add(u)
        users[role] = u
    session.commit()

    from app.auth.service import create_access_token
    return {
        "id": oid,
        "users": users,
        "H": {
            role: {"Authorization": f"Bearer {create_access_token({'user_id': u.id})}"}
            for role, u in users.items()
        },
    }


@pytest.fixture
def org(session):
    return _make_org(session)


@pytest.fixture(autouse=True)
def _clear_auth_caches():
    """Drop the process-global auth caches around every test.

    ``app.auth.service`` memoizes users and organizations by id for 5 minutes.
    The suite shares one in-memory SQLite, so user ids are auto-incremented
    ACROSS modules: another module's user can already be cached under the id
    this module's token names, and the request then resolves to a foreign user
    with a different role — a 403 that only appears in a full-suite run.
    Same treatment as tests/test_organization_lifecycle.py.
    """
    from app.auth import service as auth_service
    auth_service._USER_CACHE.clear()
    auth_service._ORG_CACHE.clear()
    yield
    auth_service._USER_CACHE.clear()
    auth_service._ORG_CACHE.clear()


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    """No network in tests. US/GB/AU/CA behave like real provider countries."""
    provider.reset_caches()
    service.invalidate_cache()
    supported = ["US", "GB", "AU", "CA", "DE"]
    monkeypatch.setattr(
        provider, "available_countries",
        lambda **kw: [{"country_code": c, "country_name": f"Country {c}"} for c in supported],
    )

    def _fetch(code, year):
        if code not in supported:
            raise provider.ProviderError(provider.KIND_UNSUPPORTED, "no data")
        return [catalog.normalize_entry(
            {"name": f"{code} National Day", "date": f"{year}-07-04",
             "holiday_type": "Public"}, source="provider")]
    monkeypatch.setattr(provider, "fetch_holidays", _fetch)
    yield
    provider.reset_caches()
    service.invalidate_cache()


def _add(client, org, **kw):
    body = {"name": "Test Holiday", "date": "2026-05-05",
            "holiday_type": "Public", "description": ""}
    body.update(kw)
    return client.post("/holidays", json=body, headers=org["H"]["admin"])


# ── Country registry ──────────────────────────────────────────────────────────
def test_countries_endpoint_merges_curated_and_provider(client, org):
    r = client.get("/holidays/countries", headers=org["H"]["admin"])
    assert r.status_code == 200
    payload = r.json()
    codes = {c["country_code"] for c in payload["countries"]}
    assert {"IN", "AE", "US", "GB", "AU", "CA"} <= codes
    support = {c["country_code"]: c["support"] for c in payload["countries"]}
    assert support["IN"] == "curated" and support["AE"] == "curated"
    assert support["US"] == "provider"


def test_employee_may_read_the_country_list(client, org):
    assert client.get("/holidays/countries", headers=org["H"]["employee"]).status_code == 200


def test_countries_requires_authentication(client):
    assert client.get("/holidays/countries").status_code in (401, 403)


# ── Catalog: the acceptance-criteria countries ────────────────────────────────
@pytest.mark.parametrize("code,expected_source", [
    ("IN", "curated"), ("AE", "curated"),
    ("US", "provider"), ("GB", "provider"), ("AU", "provider"), ("CA", "provider"),
])
def test_catalog_loads_for_every_required_country(client, org, code, expected_source):
    r = client.get(f"/holidays/catalog?country_code={code}&year=2026",
                   headers=org["H"]["admin"])
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["source"] == expected_source
    assert payload["year"] == 2026
    assert payload["holidays"], f"{code} returned an empty calendar"


def test_india_2026_is_the_reconciled_calendar_over_http(client, org):
    """The exact scenario from the bug report: India + 2026 on Manage Holidays."""
    r = client.get("/holidays/catalog?country_code=IN&year=2026", headers=org["H"]["admin"])
    assert r.status_code == 200
    payload = r.json()
    names = {h["name"] for h in payload["holidays"]}
    assert "Diwali" in names and "Republic Day" in names
    dates = {h["name"]: h["date"] for h in payload["holidays"]}
    assert dates["Holi"] == "2026-03-04"


def test_catalog_rejects_a_malformed_country_code(client, org):
    r = client.get("/holidays/catalog?country_code=XYZ&year=2026", headers=org["H"]["admin"])
    assert r.status_code == 422  # query max_length


def test_catalog_reports_an_unsupported_country_as_404_with_a_kind(client, org):
    r = client.get("/holidays/catalog?country_code=ZZ&year=2026", headers=org["H"]["admin"])
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "unsupported_country"


def test_catalog_reports_a_provider_outage_distinctly(client, org, monkeypatch):
    def _down(code, year):
        raise provider.ProviderError(provider.KIND_TIMEOUT, "slow")
    monkeypatch.setattr(provider, "fetch_holidays", _down)
    service.invalidate_cache()
    r = client.get("/holidays/catalog?country_code=US&year=2026", headers=org["H"]["admin"])
    assert r.status_code == 504
    assert r.json()["detail"]["kind"] == "provider_unavailable"


def test_catalog_rejects_an_out_of_range_year(client, org):
    r = client.get("/holidays/catalog?country_code=US&year=12026", headers=org["H"]["admin"])
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "invalid_year"


# ── CRUD ──────────────────────────────────────────────────────────────────────
def test_create_persists_the_country_code(client, org):
    r = _add(client, org, name="Diwali", date="2026-11-08", country_code="IN")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["holiday"]["country_code"] == "IN"
    assert body["holiday"]["day"] == "Sunday"


def test_create_without_country_is_unscoped(client, org):
    r = _add(client, org, name="Company Retreat", date="2026-06-01")
    assert r.json()["holiday"]["country_code"] is None


def test_create_rejects_a_bad_country_code(client, org):
    r = _add(client, org, name="X", date="2026-06-02", country_code="XX")
    # "XX" is ISO-2 shaped, so it is accepted as a label; a 3-letter code is not.
    assert r.status_code == 200
    r = client.post("/holidays",
                    json={"name": "X", "date": "2026-06-03", "country_code": "IND"},
                    headers=org["H"]["admin"])
    assert r.status_code == 422


def test_create_rejects_a_bad_date(client, org):
    r = _add(client, org, date="08-15-2026")
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]


def test_create_rejects_an_empty_name(client, org):
    assert _add(client, org, name="   ").status_code == 400


def test_duplicate_is_same_country_date_and_name(client, org):
    assert _add(client, org, name="Diwali", date="2026-11-08", country_code="IN").status_code == 200
    dup = _add(client, org, name="Diwali", date="2026-11-08", country_code="IN")
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_same_date_different_country_is_allowed(client, org):
    """The core Phase-5 requirement: one organization, two country calendars."""
    assert _add(client, org, name="Independence Day", date="2026-07-04",
                country_code="US").status_code == 200
    assert _add(client, org, name="Some Indian Holiday", date="2026-07-04",
                country_code="IN").status_code == 200


def test_same_date_different_name_is_allowed(client, org):
    assert _add(client, org, name="Gandhi Jayanti", date="2025-10-02",
                country_code="IN").status_code == 200
    assert _add(client, org, name="Dussehra", date="2025-10-02",
                country_code="IN").status_code == 200


def test_update_changes_country_and_returns_the_row(client, org):
    hid = _add(client, org, name="Movable", date="2026-04-01", country_code="IN").json()["id"]
    r = client.put(f"/holidays/{hid}",
                   json={"name": "Movable", "date": "2026-04-02",
                         "holiday_type": "Company", "country_code": "US"},
                   headers=org["H"]["admin"])
    assert r.status_code == 200
    assert r.json()["holiday"]["country_code"] == "US"
    assert r.json()["holiday"]["date"] == "2026-04-02"
    assert r.json()["holiday"]["holiday_type"] == "Company"


def test_update_of_a_missing_holiday_is_404(client, org):
    r = client.put("/holidays/99999999", json={"name": "X", "date": "2026-01-01"},
                   headers=org["H"]["admin"])
    assert r.status_code == 404


def test_update_into_a_duplicate_is_409(client, org):
    _add(client, org, name="A", date="2026-09-01", country_code="IN")
    hid = _add(client, org, name="B", date="2026-09-02", country_code="IN").json()["id"]
    r = client.put(f"/holidays/{hid}",
                   json={"name": "A", "date": "2026-09-01", "country_code": "IN"},
                   headers=org["H"]["admin"])
    assert r.status_code == 409


# ── Delete: the false-success bug ─────────────────────────────────────────────
def test_delete_removes_the_holiday(client, org):
    hid = _add(client, org, name="Temporary", date="2026-03-17").json()["id"]
    r = client.delete(f"/holidays/{hid}", headers=org["H"]["admin"])
    assert r.status_code == 200
    assert client.delete(f"/holidays/{hid}", headers=org["H"]["admin"]).status_code == 404


def test_delete_of_a_nonexistent_id_is_404_not_a_fake_success(client, org):
    """Previously returned 200 'Holiday deleted' and the UI showed a green
    toast for a no-op."""
    r = client.delete("/holidays/98765432", headers=org["H"]["admin"])
    assert r.status_code == 404
    assert r.json()["detail"] == "Holiday not found"


def test_delete_of_another_tenants_holiday_is_404_and_does_not_delete(client, session):
    a, b = _make_org(session), _make_org(session)
    hid = _add(client, a, name="A-only", date="2026-02-02").json()["id"]

    r = client.delete(f"/holidays/{hid}", headers=b["H"]["admin"])
    assert r.status_code == 404

    still_there = client.get("/holidays", headers=a["H"]["admin"]).json()
    assert any(h["id"] == hid for h in still_there), "cross-tenant delete removed the row"


# ── Listing and filters ───────────────────────────────────────────────────────
def test_list_with_no_filters_returns_everything(client, org):
    _add(client, org, name="Y2025", date="2025-05-05", country_code="IN")
    _add(client, org, name="Y2026", date="2026-05-06", country_code="US")
    names = {h["name"] for h in client.get("/holidays", headers=org["H"]["admin"]).json()}
    assert {"Y2025", "Y2026"} <= names


def test_year_filter(client, org):
    _add(client, org, name="OnlyIn2027", date="2027-05-05")
    rows = client.get("/holidays?year=2027", headers=org["H"]["admin"]).json()
    assert rows and all(h["date"].startswith("2027") for h in rows)
    assert any(h["name"] == "OnlyIn2027" for h in rows)


def test_country_filter_includes_unscoped_rows(client, org):
    """Rows that predate country codes must not vanish from the admin's view."""
    _add(client, org, name="ScopedUS", date="2026-07-04", country_code="US")
    _add(client, org, name="ScopedIN", date="2026-11-08", country_code="IN")
    _add(client, org, name="Unscoped", date="2026-06-15")

    names = {h["name"] for h in
             client.get("/holidays?country_code=US", headers=org["H"]["admin"]).json()}
    assert "ScopedUS" in names
    assert "Unscoped" in names, "unscoped holidays must survive a country filter"
    assert "ScopedIN" not in names


def test_country_and_year_filter_together(client, org):
    _add(client, org, name="US2026", date="2026-07-04", country_code="US")
    _add(client, org, name="US2027", date="2027-07-04", country_code="US")
    rows = client.get("/holidays?country_code=US&year=2027", headers=org["H"]["admin"]).json()
    names = {h["name"] for h in rows}
    assert "US2027" in names and "US2026" not in names


def test_list_rejects_an_absurd_year(client, org):
    assert client.get("/holidays?year=99999", headers=org["H"]["admin"]).status_code == 400


def test_summary_reports_the_calendars_the_org_maintains(client, org):
    _add(client, org, name="A", date="2026-07-04", country_code="US")
    _add(client, org, name="B", date="2026-11-08", country_code="IN")
    payload = client.get("/holidays/summary", headers=org["H"]["admin"]).json()
    assert set(payload["country_codes"]) >= {"US", "IN"}
    assert 2026 in payload["years"]
    assert payload["primary_country"] == "IN"


def test_employee_can_read_the_organization_calendar(client, org):
    _add(client, org, name="Visible", date="2026-08-15", country_code="IN")
    rows = client.get("/holidays", headers=org["H"]["employee"]).json()
    assert any(h["name"] == "Visible" for h in rows)


def test_list_is_tenant_scoped(client, session):
    a, b = _make_org(session), _make_org(session)
    _add(client, a, name="SECRET-A", date="2026-04-04")
    rows = client.get("/holidays", headers=b["H"]["admin"]).json()
    assert not any(h["name"] == "SECRET-A" for h in rows)


# ── Permissions ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ["admin", "hr", "owner", "super_admin"])
def test_write_roles_may_create(client, org, role):
    r = client.post("/holidays",
                    json={"name": f"By-{role}", "date": f"2026-01-{10 + len(role):02d}"},
                    headers=org["H"][role])
    assert r.status_code == 200, f"{role} was denied: {r.text}"


@pytest.mark.parametrize("role", ["employee", "billing_admin"])
def test_read_only_roles_cannot_create(client, org, role):
    r = client.post("/holidays", json={"name": "Nope", "date": "2026-01-05"},
                    headers=org["H"][role])
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["employee", "billing_admin"])
def test_read_only_roles_cannot_delete(client, org, role):
    hid = _add(client, org, name="Protected", date="2026-01-06").json()["id"]
    assert client.delete(f"/holidays/{hid}", headers=org["H"][role]).status_code == 403
    assert any(h["id"] == hid for h in
               client.get("/holidays", headers=org["H"]["admin"]).json())


@pytest.mark.parametrize("role", ["employee", "billing_admin"])
def test_read_only_roles_cannot_import(client, org, role):
    r = client.post("/holidays/import-csv",
                    files={"file": ("h.csv", io.BytesIO(b"name,date\nX,2026-01-01"), "text/csv")},
                    headers=org["H"][role])
    assert r.status_code == 403


def test_unauthenticated_write_is_rejected(client):
    assert client.post("/holidays", json={"name": "X", "date": "2026-01-01"}).status_code in (401, 403)


def test_permission_matrix_matches_the_documented_policy():
    from app.holidays import permissions
    assert set(permissions.HOLIDAY_WRITE_ROLES) == {"admin", "hr", "owner", "super_admin"}
    assert "employee" not in permissions.HOLIDAY_WRITE_ROLES
    assert "billing_admin" not in permissions.HOLIDAY_WRITE_ROLES


# ── Adopting a catalog calendar ───────────────────────────────────────────────
def test_import_catalog_writes_real_rows(client, org):
    """The step the old UI could not perform: turn a preview into holidays that
    actually affect employees."""
    r = client.post("/holidays/import-catalog",
                    json={"country_code": "IN", "year": 2026},
                    headers=org["H"]["admin"])
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["imported"] > 0
    assert payload["source"] == "curated"

    rows = client.get("/holidays?country_code=IN&year=2026", headers=org["H"]["admin"]).json()
    names = {h["name"] for h in rows}
    assert "Diwali" in names and "Republic Day" in names
    assert all(h["country_code"] == "IN" for h in rows if h["name"] == "Diwali")


def test_import_catalog_skips_optional_days_by_default(client, org):
    r = client.post("/holidays/import-catalog",
                    json={"country_code": "IN", "year": 2026},
                    headers=org["H"]["admin"]).json()
    assert r["skipped"] > 0
    rows = client.get("/holidays?country_code=IN", headers=org["H"]["admin"]).json()
    assert not any(h["name"] == "Ganesh Chaturthi" for h in rows)


def test_import_catalog_can_include_optional_days(client, org):
    client.post("/holidays/import-catalog",
                json={"country_code": "IN", "year": 2026, "include_optional": True},
                headers=org["H"]["admin"])
    rows = client.get("/holidays?country_code=IN", headers=org["H"]["admin"]).json()
    assert any(h["name"] == "Ganesh Chaturthi" for h in rows)


def test_import_catalog_is_idempotent(client, org):
    first = client.post("/holidays/import-catalog", json={"country_code": "IN", "year": 2026},
                        headers=org["H"]["admin"]).json()
    second = client.post("/holidays/import-catalog", json={"country_code": "IN", "year": 2026},
                         headers=org["H"]["admin"]).json()
    assert first["imported"] > 0
    assert second["imported"] == 0
    assert second["skipped"] >= first["imported"]


def test_import_catalog_rejects_an_unsupported_country(client, org):
    r = client.post("/holidays/import-catalog", json={"country_code": "ZZ", "year": 2026},
                    headers=org["H"]["admin"])
    assert r.status_code == 404


def test_two_country_calendars_coexist_in_one_org(client, org):
    """End-to-end proof of the Phase-5 acceptance example."""
    client.post("/holidays/import-catalog", json={"country_code": "IN", "year": 2026},
                headers=org["H"]["admin"])
    client.post("/holidays/import-catalog", json={"country_code": "US", "year": 2026},
                headers=org["H"]["admin"])
    summary = client.get("/holidays/summary", headers=org["H"]["admin"]).json()
    assert {"IN", "US"} <= set(summary["country_codes"])


# ── CSV import over HTTP ──────────────────────────────────────────────────────
def _upload(client, org, text, **data):
    return client.post(
        "/holidays/import-csv",
        files={"file": ("holidays.csv", io.BytesIO(text.strip().encode()), "text/csv")},
        data=data or None,
        headers=org["H"]["admin"],
    )


def test_csv_import_reports_structured_results(client, org):
    r = _upload(client, org, """
name,date,type
Good One,2026-02-10,Public
,2026-02-11,Public
Bad Date,rubbish,Public
Good One,2026-02-10,Public
""")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1
    assert body["invalid"] == 2
    assert body["duplicates"] == 1
    assert body["total_rows"] == 4
    assert body["invalid_detail"][0]["line"] == 3
    assert body["added"] == 1, "legacy alias must survive"


def test_csv_import_applies_the_form_country(client, org):
    r = _upload(client, org, "name,date\nFormScoped,2026-02-12", country_code="us")
    assert r.status_code == 200
    assert r.json()["country_code"] == "US"
    rows = client.get("/holidays?country_code=US", headers=org["H"]["admin"]).json()
    assert any(h["name"] == "FormScoped" and h["country_code"] == "US" for h in rows)


def test_csv_row_country_beats_the_form_country(client, org):
    _upload(client, org, "name,date,country_code\nRowScoped,2026-02-13,IN", country_code="US")
    rows = client.get("/holidays?country_code=IN", headers=org["H"]["admin"]).json()
    assert any(h["name"] == "RowScoped" and h["country_code"] == "IN" for h in rows)


def test_csv_import_detects_duplicates_against_the_database(client, org):
    _add(client, org, name="Already Here", date="2026-02-14", country_code="IN")
    body = _upload(client, org, "name,date,country_code\nAlready Here,2026-02-14,IN").json()
    assert body["imported"] == 0
    assert body["duplicates"] == 1
    assert "already in this calendar" in body["duplicate_detail"][0]["error"]


def test_csv_import_rejects_ambiguous_dates_without_importing_them(client, org):
    body = _upload(client, org, "name,date\nAmbiguous,03/04/2026").json()
    assert body["imported"] == 0
    assert body["invalid"] == 1
    assert "ambiguous" in body["invalid_detail"][0]["error"].lower()


def test_csv_import_honours_an_explicit_date_format(client, org):
    _upload(client, org, "name,date\nUSFormat,03/04/2026", date_format="mdy")
    rows = client.get("/holidays", headers=org["H"]["admin"]).json()
    match = next(h for h in rows if h["name"] == "USFormat")
    assert match["date"] == "2026-03-04"


def test_csv_import_rejects_a_file_without_required_columns(client, org):
    r = _upload(client, org, "foo,bar\n1,2")
    assert r.status_code == 400
    assert "name" in r.json()["detail"]


def test_csv_import_rejects_an_oversized_file(client, org):
    payload = "name,date\n" + "X,2026-01-01\n" * 400_000
    r = _upload(client, org, payload)
    assert r.status_code == 413


def test_csv_options_endpoint_describes_the_contract(client, org):
    body = client.get("/holidays/import-csv/options", headers=org["H"]["admin"]).json()
    assert body["required_columns"] == ["name", "date"]
    assert "country_code" in body["optional_columns"]
    assert {f["value"] for f in body["date_formats"]} == {"auto", "iso", "dmy", "mdy"}


def test_csv_import_is_tenant_scoped(client, session):
    a, b = _make_org(session), _make_org(session)
    _upload(client, a, "name,date\nA-Import,2026-02-20")
    rows = client.get("/holidays", headers=b["H"]["admin"]).json()
    assert not any(h["name"] == "A-Import" for h in rows)


# ── /holidays/dates helper ────────────────────────────────────────────────────
def test_dates_helper_returns_applicable_dates(client, org):
    _add(client, org, name="Scoped", date="2026-11-08", country_code="IN")
    _add(client, org, name="Unscoped", date="2026-06-15")
    dates = client.get("/holidays/dates?year=2026", headers=org["H"]["admin"]).json()["dates"]
    assert "2026-11-08" in dates and "2026-06-15" in dates


def test_dates_helper_excludes_another_countrys_calendar(client, org):
    """org fixture is primary_country=IN, so a US-scoped holiday must not count
    as a non-working day for its staff."""
    _add(client, org, name="US Only", date="2026-07-04", country_code="US")
    _add(client, org, name="IN Only", date="2026-11-08", country_code="IN")
    dates = client.get("/holidays/dates?year=2026", headers=org["H"]["admin"]).json()["dates"]
    assert "2026-11-08" in dates
    assert "2026-07-04" not in dates


def test_dates_helper_is_tenant_scoped(client, session):
    a, b = _make_org(session), _make_org(session)
    _add(client, a, name="A", date="2026-12-30", country_code="IN")
    dates = client.get("/holidays/dates", headers=b["H"]["admin"]).json()["dates"]
    assert "2026-12-30" not in dates


# ── Lazy seeding ──────────────────────────────────────────────────────────────
def test_a_new_indian_org_is_seeded_from_the_shared_catalog(client, session):
    fresh = _make_org(session, country="IN", seeded=False)
    rows = client.get("/holidays", headers=fresh["H"]["admin"]).json()
    names = {h["name"] for h in rows}
    assert "Republic Day" in names
    assert all(h["country_code"] == "IN" for h in rows)


def test_seeding_never_uses_another_countrys_calendar(client, session):
    """The audit found two US organizations holding Diwali and Republic Day.
    A US org must never be seeded from the Indian calendar."""
    fresh = _make_org(session, country="US", seeded=False)
    rows = client.get("/holidays", headers=fresh["H"]["admin"]).json()
    names = {h["name"] for h in rows}
    assert "Diwali" not in names and "Republic Day" not in names


def test_an_org_with_no_country_is_seeded_with_nothing(client, session):
    fresh = _make_org(session, country=None, seeded=False)
    assert client.get("/holidays", headers=fresh["H"]["admin"]).json() == []


def test_seeding_skips_optional_days(client, session):
    fresh = _make_org(session, country="IN", seeded=False)
    rows = client.get("/holidays", headers=fresh["H"]["admin"]).json()
    assert not any(h["name"] == "Ganesh Chaturthi" for h in rows)


def test_seeding_runs_at_most_once(client, session):
    fresh = _make_org(session, country="IN", seeded=False)
    first = client.get("/holidays", headers=fresh["H"]["admin"]).json()
    assert first
    for h in first:
        client.delete(f"/holidays/{h['id']}", headers=fresh["H"]["admin"])
    assert client.get("/holidays", headers=fresh["H"]["admin"]).json() == [], \
        "an emptied calendar must not be re-seeded"
