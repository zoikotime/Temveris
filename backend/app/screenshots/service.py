from sqlalchemy.orm import Session
from sqlalchemy import desc, asc, func
from database import models
from typing import Optional
from datetime import date
import os


def _file_url(file_path: Optional[str]) -> str:
    """Convert stored file_path to a relative URL that resolves correctly on any domain.
    Handles both:
      - new style: '/uploads/filename.png'  (stored as relative URL)
      - old style: 'C:/Users/.../filename.png'  (stored as OS path)
    Returns a root-relative URL so it works on localhost AND production cloud deployments.
    """
    if not file_path:
        return ""
    fp = file_path.replace("\\", "/")
    if fp.startswith("/uploads/"):
        return fp  # already root-relative: /uploads/filename.png
    # fallback: extract just the filename
    filename = fp.split("/")[-1]
    return f"/uploads/{filename}"


def get_screenshots(
    db: Session,
    organization_id: Optional[int] = None,
    user_id: Optional[int] = None,
    date_filter: Optional[date] = None,
    app_name: Optional[str] = None,
    is_active: Optional[bool] = None,
    sort: str = "newest",
    skip: int = 0,
    limit: int = 100,
):
    """
    Returns list of (Activity, employee_name) tuples with optional filters.
    """
    q = (
        db.query(models.Activity, models.User.name.label("employee_name"))
        .outerjoin(models.User, models.Activity.user_id == models.User.id)
    )

    if organization_id is not None:
        q = q.filter(models.Activity.organization_id == organization_id)

    if user_id is not None:
        q = q.filter(models.Activity.user_id == user_id)

    if date_filter is not None:
        q = q.filter(func.date(models.Activity.timestamp) == date_filter)

    if app_name:
        q = q.filter(models.Activity.app_name.ilike(f"%{app_name}%"))

    if is_active is not None:
        q = q.filter(models.Activity.is_active == is_active)

    order = desc(models.Activity.timestamp) if sort == "newest" else asc(models.Activity.timestamp)
    q = q.order_by(order)

    return q.offset(skip).limit(limit).all()


def _load_image_bytes(act) -> Optional[bytes]:
    """Load raw image bytes from an Activity record, trying all available sources."""
    import base64
    import requests as req

    try:
        from app.storage import backend_name, signed_url, SCREENSHOTS
    except ImportError:
        return None

    # 1. Supabase private bucket → signed URL → download
    if backend_name() == "supabase":
        target = act.file_path or getattr(act, "thumbnail", None)
        if target and not (len(target) > 200 and not target.startswith("/")):
            # Resolve the bucket key EXACTLY like the dashboard image endpoint
            # (app/screenshots/router.py get_screenshot_image): preserve the
            # org-prefix subpath for "/uploads/org_1/<file>.webp" instead of
            # collapsing to the bare basename. The old `.split("/")[-1]` dropped
            # the "org_1/" prefix, so every org-prefixed screenshot resolved to
            # a non-existent key and silently fell out of the timelapse while
            # still rendering fine in the viewer. Keeping the two loaders in
            # lock-step is the whole point of this change.
            fp = target.replace("\\", "/")
            filename = (
                fp[len("/uploads/"):]
                if fp.startswith("/uploads/")
                else fp.split("/")[-1]
            )
            if filename:
                url = signed_url(SCREENSHOTS, filename, expires_in=3600)
                if url:
                    try:
                        r = req.get(url, timeout=15)
                        if r.status_code == 200:
                            return r.content
                    except Exception:
                        pass

    # 2. Legacy base64 blob stored in thumbnail column
    thumb = getattr(act, "thumbnail", None)
    if thumb and len(thumb) > 200 and not thumb.startswith("/"):
        try:
            return base64.b64decode(thumb)
        except Exception:
            pass

    # 3. Local disk (tracker-data/uploads/<org_prefix>/<filename>)
    if act.file_path:
        fp = act.file_path.replace("\\", "/")
        base = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        uploads_root = os.path.normpath(
            os.path.join(base, "..", "tracker-data", "uploads")
        )
        # Use relative path under /uploads/ to support org-prefixed storage
        relative = fp[len("/uploads/"):] if fp.startswith("/uploads/") else os.path.basename(fp)
        disk_path = os.path.normpath(os.path.join(uploads_root, relative))
        # Guard against path traversal
        if not disk_path.startswith(uploads_root + os.sep) and disk_path != uploads_root:
            return None
        if os.path.isfile(disk_path):
            try:
                with open(disk_path, "rb") as f:
                    return f.read()
            except OSError:
                pass

    return None


def _adaptive_fps(total_shots: int) -> float:
    """
    Calculate FPS so the timelapse hits a sensible duration.
      < 15 shots  → 0.5 fps  (1 frame every 2s, avoids sub-second videos)
      15–300 shots → scales so duration ≈ 20–30 s
      > 300 shots  → 15 fps cap  (avoids insanely long encodes)
    """
    if total_shots == 0:
        return 1.0
    if total_shots <= 15:
        return 0.5
    # Target ~25 s; clamp to [1, 15]
    return round(max(1.0, min(15.0, total_shots / 25.0)), 2)


def generate_timelapse(
    db: Session,
    user_id: Optional[int] = None,
    date_filter: Optional[date] = None,
    organization_id: Optional[int] = None,
    out_width: int = 854,
    out_height: int = 480,
) -> tuple:
    """
    Production-grade timelapse generator.
    - Loads images concurrently (8 threads) for speed
    - Auto-calculates FPS so duration is always 15-30 s
    - Encodes H.264/yuv420p — browser-compatible in Chrome/Firefox/Safari
    - Faststart flag lets video play before fully downloaded
    Returns (mp4_bytes, frame_count, fps, skipped_count)
    """
    try:
        import imageio
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "pip install imageio imageio-ffmpeg numpy"
        ) from exc

    from PIL import Image
    import io as _io
    import tempfile
    import concurrent.futures

    rows = get_screenshots(
        db,
        organization_id=organization_id,
        user_id=user_id,
        date_filter=date_filter,
        sort="oldest",
        limit=1000,
    )
    if not rows:
        raise ValueError("No screenshots found for the given filters")

    total = len(rows)
    fps = _adaptive_fps(total)

    # H.264 requires even pixel dimensions
    w = (out_width // 2) * 2
    h = (out_height // 2) * 2

    # ── Concurrent image loading ─────────────────────────────────────────────
    # Load all image files in parallel (8 workers) so disk/network I/O
    # doesn't serialize the entire encode pipeline.
    def _load(pair):
        act, _ = pair
        return _load_image_bytes(act)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        raw_images = list(pool.map(_load, rows))

    # ── Decode + resize ──────────────────────────────────────────────────────
    frames = []
    skipped = 0
    for img_bytes in raw_images:
        if not img_bytes:
            skipped += 1
            continue
        try:
            img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
            img = img.resize((w, h), Image.LANCZOS)
            frames.append(np.array(img, dtype=np.uint8))
        except Exception:
            skipped += 1

    if not frames:
        raise ValueError(
            f"No loadable images found ({skipped}/{total} files missing or corrupt)"
        )

    # ── Encode MP4 ───────────────────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmppath = tmp.name

    try:
        with imageio.get_writer(
            tmppath,
            fps=fps,
            codec="libx264",
            ffmpeg_params=[
                "-pix_fmt", "yuv420p",   # browser-compatible chroma subsampling
                "-preset", "fast",        # good speed/size tradeoff
                "-crf", "26",             # quality (18=lossless, 28=decent, 51=worst)
                "-movflags", "+faststart", # moov atom at front → instant playback
            ],
        ) as writer:
            for frame in frames:
                writer.append_data(frame)

        with open(tmppath, "rb") as fh:
            return fh.read(), len(frames), fps, skipped
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def delete_screenshot(
    db: Session,
    screenshot_id: int,
    organization_id: Optional[int] = None,
) -> bool:
    act = db.query(models.Activity).filter(models.Activity.id == screenshot_id).first()
    if not act:
        return False

    # Org isolation: treat cross-org access as not-found to avoid leaking existence
    if organization_id is not None and act.organization_id != organization_id:
        return False

    # Delete the image from storage (Supabase bucket in prod, disk locally).
    # We try both file_path and thumbnail in case they point at different
    # objects (legacy rows sometimes differ).
    try:
        from app.storage import delete_object, backend_name, SCREENSHOTS

        def _filename_of(path: Optional[str]) -> Optional[str]:
            if not path:
                return None
            # Skip base64 blobs that legacy rows may still have in `thumbnail`
            if not path.startswith("/") and len(path) > 200:
                return None
            # Preserve org-prefixed relative path (e.g. "org_1/80_123.webp")
            # so the storage layer deletes from the correct bucket path.
            relative = path.replace("\\", "/")
            prefix = "/uploads/"
            if relative.startswith(prefix):
                return relative[len(prefix):]
            return relative.split("/")[-1]

        for path in {_filename_of(act.file_path), _filename_of(getattr(act, "thumbnail", None))}:
            if path:
                try:
                    delete_object(SCREENSHOTS, path)
                except Exception:
                    pass  # object may already be gone — non-fatal

        # Legacy local-disk paths that were written before storage abstraction
        if backend_name() == "local" and act.file_path and os.path.exists(act.file_path):
            try:
                os.remove(act.file_path)
            except OSError:
                pass
    except Exception:
        # Deletion of the object must never block deletion of the DB row
        pass

    db.delete(act)
    db.commit()
    return True