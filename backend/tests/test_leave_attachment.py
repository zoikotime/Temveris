"""Supporting-document upload + retrieval on leave requests.

Two things are under test here:

1. The upload CONTRACT published by GET /leave/types is the same contract
   /leave/apply enforces. The Apply Leave dropzone renders its accepted formats
   and size cap straight from that payload, so if the two drift the employee is
   shown a limit the server will reject.

2. GET /leave/{id}/attachment releases the file to the employee who filed the
   request and to admin/hr in the SAME organization — and to nobody else. These
   files are medical certificates and birth certificates; before this route the
   only way to read one was an unauthenticated static path, so "another
   tenant's admin cannot read it" is the assertion that matters most.
"""

from __future__ import annotations

import io
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(scope="module", autouse=True)
def storage_root(monkeypatch_module, tmp_path_factory):
    """Local-disk storage, rooted in tmp_path.

    Two separate reasons:
      • `backend_name()` reads STORAGE_BACKEND on every call and a developer
        .env sets it to "supabase" — without the override the upload test would
        write into the real bucket.
      • TRACKER_DATA_DIR is the repo's own tracker-data/, so uploads would leave
        stray files in the working tree. Patching it moves BOTH the write path
        (put_upload_file) and the read path (local_root) at once, which is the
        point of routing them through the same helper.
    """
    monkeypatch_module.setenv("STORAGE_BACKEND", "local")
    root = tmp_path_factory.mktemp("tracker-data")
    from app.storage import service as storage_service
    monkeypatch_module.setattr(storage_service, "TRACKER_DATA_DIR", str(root))
    return root / "leave-attachments"


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _mk_org(s, name):
    org = models.Organization(name=name)
    s.add(org)
    s.commit()
    s.refresh(org)
    s.add(models.OrgSettings(organization_id=org.id, default_timezone="Asia/Kolkata"))
    s.commit()
    return org.id


def _mk_user(s, oid, role):
    """Returns the id, not the instance — the fixture closes its session and a
    detached User cannot be read from."""
    u = models.User(
        name=f"{role}-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex}@x.com",
        password="x", role=role, is_active=True, organization_id=oid,
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    return u.id


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


@pytest.fixture(scope="module")
def world(app_, engine):
    """Two tenants. Org A has an employee with a filed document + an admin.
    Org B has an unrelated admin who must never see org A's document."""
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    oid_a, oid_b = _mk_org(s, "ATT-ORG-A"), _mk_org(s, "ATT-ORG-B")
    emp     = _mk_user(s, oid_a, "employee")
    other   = _mk_user(s, oid_a, "employee")
    admin_a = _mk_user(s, oid_a, "admin")
    admin_b = _mk_user(s, oid_b, "admin")
    out = {
        "emp": emp, "other": other, "admin_a": admin_a, "admin_b": admin_b,
        "oid_a": oid_a, "oid_b": oid_b,
    }
    s.close()
    return out


@pytest.fixture(scope="module")
def filed(app_, engine, world, storage_root):
    """A leave request whose document exists on disk under the storage root."""
    from app.storage import ATTACHMENTS

    stored = f"org_{world['oid_a']}/{uuid.uuid4().hex}.png"
    target = storage_root / stored
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1PX)

    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    from datetime import datetime
    req = models.LeaveRequest(
        user_id=world["emp"],
        organization_id=world["oid_a"],
        leave_type="Sick Leave",
        from_date=datetime(2027, 3, 1),
        to_date=datetime(2027, 3, 2),
        description="flu",
        status="submitted",
        attachment_path=f"{ATTACHMENTS.url_prefix}/{stored}",
        attachment_name="medical-certificate.png",
    )
    s.add(req)
    s.commit()
    s.refresh(req)
    rid = req.id
    s.close()
    return rid


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


# ══════════════════════════════════════════════════════════════════════════
# 1. The published contract IS the enforced contract
# ══════════════════════════════════════════════════════════════════════════

def test_types_publish_the_same_limits_apply_enforces(app_, world):
    from app.leave.router import MAX_FILE_MB, DOC_ACCEPT, DOC_FORMATS
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get("/leave/types", headers=_auth(world["emp"]))
    assert r.status_code == 200
    rows = r.json()
    assert rows, "no leave types returned"
    for row in rows:
        assert row["doc_max_mb"] == MAX_FILE_MB
        assert row["doc_accept"] == DOC_ACCEPT
        assert row["doc_formats"] == list(DOC_FORMATS)


def test_every_advertised_format_has_a_stored_extension():
    """The stored filename's extension is derived from the validated MIME, so
    an advertised format with no MIME_EXT entry would raise a KeyError at
    upload time instead of being rejected cleanly."""
    from app.leave.router import MIME_EXT, DOC_FORMATS, ALLOWED_MIME
    assert ALLOWED_MIME == set(MIME_EXT)
    stored_exts = {e.lower() for e in MIME_EXT.values()}
    for fmt in DOC_FORMATS:
        f = fmt.lower()
        # jpg and jpeg both map to the single "jpg" stored extension.
        assert f in stored_exts or (f == "jpeg" and "jpg" in stored_exts), \
            f"{fmt} is advertised but has no MIME_EXT mapping"


def test_the_filename_never_decides_the_stored_extension(app_, world, storage_root):
    """A request that claims image/png while uploading "payload.exe" must be
    stored as .png. The attachments path serves whatever is written there."""
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.post(
        "/leave/apply",
        headers=_auth(world["emp"]),
        data={
            "leave_type": "Sick Leave",
            "from_date": "2027-06-01",
            "to_date": "2027-06-02",
            "description": "flu",
        },
        files={"attachment": ("payload.exe", io.BytesIO(PNG_1PX), "image/png")},
    )
    assert r.status_code in (200, 422), r.text
    if r.status_code == 422:
        pytest.skip("policy validation blocked this request for an unrelated reason")
    from database import database as _db
    s = _db.SessionLocal()
    req = s.query(models.LeaveRequest).filter(models.LeaveRequest.id == r.json()["id"]).first()
    stored = req.attachment_path
    display = req.attachment_name
    s.close()
    assert stored.endswith(".png"), f"stored as {stored}"
    assert ".exe" not in stored
    # The original name is still shown to the reviewer, just never used as a path.
    assert display == "payload.exe"


def test_an_unaccepted_content_type_is_rejected(app_, world):
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.post(
        "/leave/apply",
        headers=_auth(world["emp"]),
        data={
            "leave_type": "Sick Leave",
            "from_date": "2027-06-03",
            "to_date": "2027-06-04",
            "description": "flu",
        },
        files={"attachment": ("x.exe", io.BytesIO(b"MZ\x90\x00"), "application/x-msdownload")},
    )
    assert r.status_code == 415


# ══════════════════════════════════════════════════════════════════════════
# 2. Who may read the document
# ══════════════════════════════════════════════════════════════════════════

def test_owner_can_read_their_own_document(app_, world, filed):
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment", headers=_auth(world["emp"]))
    assert r.status_code == 200
    assert r.content == PNG_1PX
    # Never cached outside the authenticated response.
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_admin_in_the_same_org_can_read_it(app_, world, filed):
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment", headers=_auth(world["admin_a"]))
    assert r.status_code == 200
    assert r.content == PNG_1PX


def test_a_colleague_cannot_read_it(app_, world, filed):
    """Same organization, but neither the owner nor a reviewer."""
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment", headers=_auth(world["other"]))
    assert r.status_code == 403


def test_another_tenants_admin_cannot_read_it(app_, world, filed):
    """The assertion that matters: an admin is only an admin of their OWN org.
    404, not 403 — a 403 would confirm the request exists."""
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment", headers=_auth(world["admin_b"]))
    assert r.status_code == 404


def test_anonymous_cannot_read_it(app_, filed):
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment")
    assert r.status_code in (401, 403)


def test_the_raw_attachments_path_is_no_longer_public(app_, filed):
    """It used to be a StaticFiles mount that served every stored certificate
    to anyone with the URL."""
    from database import database as _db
    s = _db.SessionLocal()
    path = s.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == filed).first().attachment_path
    s.close()
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(path)
    assert r.status_code in (401, 403), \
        f"{path} answered {r.status_code} without a token"


def test_supabase_mode_streams_the_bytes_and_never_redirects(app_, world, filed, monkeypatch):
    """Production runs STORAGE_BACKEND=supabase. This endpoint must behave there
    exactly as it does locally: same 200, same bytes, no 302 handing the browser
    a signed URL (a shareable, unauthenticated link to a medical record)."""
    from app.storage import service as storage_service
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")

    calls = {}

    class _FakeBucketApi:
        def download(self, filename):
            calls["filename"] = filename
            return PNG_1PX

    class _FakeStorage:
        def from_(self, name):
            calls["bucket"] = name
            return _FakeBucketApi()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr(storage_service, "_get_supabase", lambda: _FakeClient())

    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment",
                   headers=_auth(world["emp"]), follow_redirects=False)
    assert r.status_code == 200, f"{r.status_code} — a 302 means it leaked a signed URL"
    assert r.content == PNG_1PX
    assert r.headers["content-type"].startswith("image/png")
    assert "no-store" in r.headers.get("Cache-Control", "")
    # The bucket-relative key, not the stored "/leave-attachments/..." URL path.
    assert calls["filename"].startswith(f"org_{world['oid_a']}/")


def test_supabase_mode_turns_a_missing_object_into_a_404(app_, world, filed, monkeypatch):
    """storage3 raises for a deleted object (and has a bug where a failed
    request raises UnboundLocalError). That must be a 404, never a 500."""
    from app.storage import service as storage_service
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")

    class _Boom:
        def download(self, filename):
            raise RuntimeError("Object not found")

    class _FakeStorage:
        def from_(self, name):
            return _Boom()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr(storage_service, "_get_supabase", lambda: _FakeClient())

    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{filed}/attachment", headers=_auth(world["emp"]))
    assert r.status_code == 404


def test_the_employee_supplied_filename_cannot_break_the_header(app_, world, engine, storage_root):
    """attachment_name is whatever the employee's file was called. It lands in
    Content-Disposition, so quotes and newlines must not escape the header."""
    from datetime import datetime
    from app.storage import ATTACHMENTS
    stored = f"org_{world['oid_a']}/{uuid.uuid4().hex}.pdf"
    target = storage_root / stored
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4 fake")

    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    req = models.LeaveRequest(
        user_id=world["emp"], organization_id=world["oid_a"],
        leave_type="Sick Leave",
        from_date=datetime(2027, 7, 1), to_date=datetime(2027, 7, 2),
        description="flu", status="submitted",
        attachment_path=f"{ATTACHMENTS.url_prefix}/{stored}",
        attachment_name='eh".pdf\r\nX-Injected: yes',
    )
    s.add(req); s.commit(); s.refresh(req)
    rid = req.id
    s.close()

    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{rid}/attachment", headers=_auth(world["emp"]))
    assert r.status_code == 200
    assert "x-injected" not in {k.lower() for k in r.headers}
    assert "\n" not in r.headers["Content-Disposition"]


@pytest.mark.parametrize("stored", [
    "../../../../etc/passwd",          # walk out with ..
    "/etc/passwd",                     # absolute posix
    "C:/Windows/win.ini",              # absolute windows (different drive → ValueError)
])
def test_a_stored_path_cannot_escape_the_attachments_root(app_, world, engine, stored):
    """A row whose attachment_path points outside the bucket must 404, not
    serve the file and not 500 on the containment check."""
    from datetime import datetime
    from app.storage import ATTACHMENTS
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    req = models.LeaveRequest(
        user_id=world["emp"], organization_id=world["oid_a"],
        leave_type="Sick Leave",
        from_date=datetime(2027, 5, 1), to_date=datetime(2027, 5, 2),
        description="flu", status="submitted",
        attachment_path=f"{ATTACHMENTS.url_prefix}/{stored}",
        attachment_name="x.png",
    )
    s.add(req); s.commit(); s.refresh(req)
    rid = req.id
    s.close()
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{rid}/attachment", headers=_auth(world["emp"]))
    assert r.status_code == 404, r.status_code


def test_a_request_with_no_document_is_a_clean_404(app_, world, engine):
    from datetime import datetime
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    req = models.LeaveRequest(
        user_id=world["emp"], organization_id=world["oid_a"],
        leave_type="Annual Leave",
        from_date=datetime(2027, 4, 1), to_date=datetime(2027, 4, 2),
        description="holiday", status="submitted",
    )
    s.add(req); s.commit(); s.refresh(req)
    rid = req.id
    s.close()
    client = TestClient(app_, raise_server_exceptions=False)
    r = client.get(f"/leave/{rid}/attachment", headers=_auth(world["emp"]))
    assert r.status_code == 404
