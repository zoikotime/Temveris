"""
Temveris — unified storage service.

Supports two backends, switched via the STORAGE_BACKEND env var:
  - "local"    (default): writes to tracker-data/ on local disk.
  - "supabase": uploads to Supabase Storage using the service-role key.

All upload sites in the codebase (screenshots, leave attachments, org logos)
use put_bytes(...) / put_upload_file(...) so switching backends is a single
env-var flip. The database continues to store the same URL paths it always
did (/uploads/<file>, /leave-attachments/<file>, /static/org/<file>); the
frontend does not change. Serving is handled by main.py — StaticFiles in
local mode, signed-URL redirects in supabase mode.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

# ─── Bucket catalogue ──────────────────────────────────────────────────────
# Each bucket entry maps a logical name to the URL prefix the DB stores and
# the local directory we fall back to when STORAGE_BACKEND=local.
# Visibility drives whether we return public URLs or short-lived signed URLs.
@dataclass(frozen=True)
class Bucket:
    key: str               # logical name
    env_name: str          # env var overriding the Supabase bucket name
    url_prefix: str        # public URL prefix the rest of the app stores
    local_subdir: str      # folder under tracker-data/
    public: bool           # True → public bucket, False → signed URLs


SCREENSHOTS = Bucket(
    key="screenshots",
    env_name="SUPABASE_SCREENSHOTS_BUCKET",
    url_prefix="/uploads",
    local_subdir="uploads",
    public=True,
)
ATTACHMENTS = Bucket(
    key="leave-attachments",
    env_name="SUPABASE_ATTACHMENTS_BUCKET",
    url_prefix="/leave-attachments",
    local_subdir="leave-attachments",
    public=False,
)
ORG_LOGOS = Bucket(
    key="org-logos",
    env_name="SUPABASE_ORG_BUCKET",
    url_prefix="/static/org",
    local_subdir="org",
    public=True,
)

_BUCKETS_BY_PREFIX = {
    SCREENSHOTS.url_prefix: SCREENSHOTS,
    ATTACHMENTS.url_prefix: ATTACHMENTS,
    ORG_LOGOS.url_prefix:   ORG_LOGOS,
}


# ─── Root paths ────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRACKER_DATA_DIR = os.path.join(_BASE_DIR, "..", "tracker-data")


def backend_name() -> str:
    return (os.getenv("STORAGE_BACKEND") or "local").strip().lower()


def _bucket_name(b: Bucket) -> str:
    return os.getenv(b.env_name) or b.key


def local_root(b: Bucket) -> str:
    """Directory a bucket's files live in under the local backend.

    Readers must resolve through this rather than recomputing the path from
    their own __file__ — the leave router and main.py each had their own
    "../../tracker-data/leave-attachments", which agreed only by coincidence
    and would have silently 404'd every document the moment one moved.
    """
    return os.path.join(TRACKER_DATA_DIR, b.local_subdir)


def _local_path(b: Bucket, filename: str) -> str:
    path = os.path.join(local_root(b), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def bucket_for_prefix(prefix: str) -> Optional[Bucket]:
    """Look up a bucket by the /<prefix> a URL starts with."""
    # Normalize: /uploads/foo.png → /uploads
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    for p, b in _BUCKETS_BY_PREFIX.items():
        if prefix == p or prefix.startswith(p + "/"):
            return b
    return None


# ─── Supabase client (lazy, singleton) ─────────────────────────────────────
_supabase_client = None


def _get_supabase():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    try:
        from supabase import create_client
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "supabase-py not installed. Add `supabase` to requirements.txt."
        ) from e
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "STORAGE_BACKEND=supabase but SUPABASE_URL / "
            "SUPABASE_SERVICE_ROLE_KEY are not set in the environment."
        )
    _supabase_client = create_client(url, key)
    return _supabase_client


# ─── Public API ────────────────────────────────────────────────────────────
def _run_with_timeout(fn, timeout_secs: int):
    """Run fn() in a thread; raise TimeoutError if it takes too long."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        return future.result(timeout=timeout_secs)


def put_bytes(bucket: Bucket, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """
    Store raw bytes. Returns the relative URL path (same shape the DB has
    always used, e.g. "/uploads/123.png"), so callers do not need to branch
    on the backend.
    """
    if backend_name() == "supabase":
        sb   = _get_supabase()
        name = _bucket_name(bucket)
        def _do_upload():
            return sb.storage.from_(name).upload(
                path=filename,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        try:
            res = _run_with_timeout(_do_upload, timeout_secs=20)
        except Exception as e:
            print(f"[storage] UPLOAD FAILED bucket={name} file={filename} err={e!r}", flush=True)
            raise
        err = getattr(res, "error", None) or (isinstance(res, dict) and res.get("error"))
        if err:
            print(f"[storage] UPLOAD ERROR RESPONSE bucket={name} file={filename} err={err!r}", flush=True)
            raise RuntimeError(f"Supabase upload failed: {err}")
        print(f"[storage] UPLOAD OK bucket={name} file={filename} bytes={len(data)}", flush=True)
    else:
        with open(_local_path(bucket, filename), "wb") as f:
            f.write(data)
    return f"{bucket.url_prefix}/{filename}"


def get_bytes(bucket: Bucket, filename: str) -> Optional[bytes]:
    """Read an object back. Returns None when it does not exist.

    The mirror of put_bytes, and the only way to read a PRIVATE bucket without
    handing the caller a signed URL. A signed URL is a bearer credential: once
    it reaches a browser it is a shareable, unauthenticated link to the object
    for its whole lifetime, which defeats the point of authorizing the read in
    the first place. Callers that must not leak the object stream these bytes
    themselves.

    Path containment for the local backend is the CALLER's job — this resolves
    `filename` relative to the bucket root without validating it.
    """
    if backend_name() == "supabase":
        sb   = _get_supabase()
        name = _bucket_name(bucket)
        try:
            return _run_with_timeout(
                lambda: sb.storage.from_(name).download(filename), timeout_secs=20)
        except Exception as e:
            # Same rationale as signed_url: storage3 raises for a missing object
            # (and has a bug where a failed request raises UnboundLocalError),
            # so a deleted file must become a clean 404, never a 500.
            print(f"[storage] download failed bucket={name} file={filename} err={e!r}", flush=True)
            return None
    path = os.path.join(local_root(bucket), filename)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def put_upload_file(bucket: Bucket, filename: str, upload_file, content_type: Optional[str] = None) -> str:
    """Stream a FastAPI UploadFile to storage."""
    ctype = content_type or getattr(upload_file, "content_type", None) or "application/octet-stream"
    if backend_name() == "supabase":
        upload_file.file.seek(0)
        data = upload_file.file.read()
        return put_bytes(bucket, filename, data, ctype)
    # Local: stream to disk without buffering the whole file in memory.
    path = _local_path(bucket, filename)
    upload_file.file.seek(0)
    with open(path, "wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    return f"{bucket.url_prefix}/{filename}"


def signed_url(bucket: Bucket, filename: str, expires_in: int = 3600) -> Optional[str]:
    """
    Resolve a stored filename to a fetchable URL.
      - Public bucket (org logos): returns the permanent public URL.
      - Private bucket: returns a short-lived signed URL (default 1 hour).
    In local mode, returns None so callers know to serve via StaticFiles.
    """
    if backend_name() != "supabase":
        return None
    sb   = _get_supabase()
    name = _bucket_name(bucket)
    if bucket.public:
        try:
            res = sb.storage.from_(name).get_public_url(filename)
        except Exception as e:
            print(f"[storage] public_url failed bucket={name} file={filename} err={e!r}", flush=True)
            return None
        # supabase-py versions return either str or {"publicUrl": str}
        if isinstance(res, dict):
            return res.get("publicUrl") or res.get("publicURL")
        return res
    # storage3 has a bug where a failed HTTP request raises UnboundLocalError
    # ("cannot access local variable 'response'") instead of a clean error.
    # Wrap it so a missing/renamed object returns None (→ 404) not 500.
    try:
        res = sb.storage.from_(name).create_signed_url(filename, expires_in)
    except Exception as e:
        print(f"[storage] signed_url failed bucket={name} file={filename} err={e!r}", flush=True)
        return None
    if isinstance(res, dict):
        return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")
    return res


def delete_object(bucket: Bucket, filename: str) -> None:
    if backend_name() == "supabase":
        try:
            _get_supabase().storage.from_(_bucket_name(bucket)).remove([filename])
        except Exception:
            pass
        return
    try:
        os.remove(_local_path(bucket, filename))
    except FileNotFoundError:
        pass
