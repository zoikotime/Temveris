from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import Response, FileResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import date
import base64
import os
import requests

from database.database import get_db
from database import models
from . import service, schemas
from app.storage import (
    backend_name as _storage_backend,
    signed_url as _signed_url,
    SCREENSHOTS as _SCR,
)
from app.auth.service import get_current_user, get_current_organization
from app.rate_limit import limiter

_TIMELAPSE_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "..",
        "tracker-data",
        "timelapse",
    )
)
_TIMELAPSE_ENABLED = True  # Set to False to disable

router = APIRouter(tags=["Screenshots"])


def _extract_filename(file_path: Optional[str]) -> Optional[str]:
    if not file_path:
        return None
    return file_path.replace("\\", "/").split("/")[-1]


def _looks_like_base64_blob(value: Optional[str]) -> bool:
    if not value:
        return False
    if value.startswith("/") or value.startswith("http"):
        return False
    return len(value) > 200


@router.get("/screenshots", response_model=List[schemas.ScreenshotOut])
def list_screenshots(
    user_id: Optional[int] = Query(None),
    date_filter: Optional[date] = Query(None, alias="date"),
    app_name: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    sort: str = Query("newest"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    rows = service.get_screenshots(
        db,
        organization_id=current_org.id,
        user_id=user_id,
        date_filter=date_filter,
        app_name=app_name,
        is_active=is_active,
        sort=sort,
        skip=skip,
        limit=limit,
    )

    return [
        schemas.ScreenshotOut(
            id=act.id,
            user_id=act.user_id,
            employee_name=emp_name,
            timestamp=act.timestamp,
            app_name=act.app_name,
            window_title=act.window_title,
            # FIXED: The screenshots router is mounted at /api, so the image
            # endpoint lives at /api/screenshots/{id}/image (NOT /screenshots/...).
            # The frontend prepends API_BASE to this value, so we need the
            # full path including the /api prefix.
            file_url=f"/api/screenshots/{act.id}/image",
            thumbnail=None,  # remove base64 usage
            mouse_clicks=act.mouse_clicks or 0,
            key_presses=act.key_presses or 0,
            idle_seconds=act.idle_seconds or 0,
            productivity_score=act.productivity_score or 0.0,
            is_active=bool(act.is_active),
        )
        for act, emp_name in rows
    ]


@router.get("/screenshots/{screenshot_id}/image")
@limiter.exempt
def get_screenshot_image(
    request: Request,
    screenshot_id: int,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    act = db.query(models.Activity).filter(models.Activity.id == screenshot_id).first()
    if not act:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    # Org isolation: return 404 (not 403) to avoid leaking cross-org IDs
    if act.organization_id is not None and act.organization_id != current_org.id:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    # 1. SUPABASE — stream image via signed URL
    if _storage_backend() == "supabase":
        target = act.file_path or act.thumbnail

        if target and not _looks_like_base64_blob(target):
            fp = target.replace("\\", "/")
            # Preserve org-prefix path (e.g. "org_1/80_123.webp")
            relative = fp[len("/uploads/"):] if fp.startswith("/uploads/") else fp.split("/")[-1]

            if relative:
                url = _signed_url(_SCR, relative, expires_in=3600)
                db.close()  # release pool slot before network call
                if url:
                    try:
                        res = requests.get(url, timeout=10)
                        if res.status_code == 200:
                            return Response(
                                content=res.content, media_type="image/jpeg"
                            )
                    except Exception:
                        pass

    # 2. LEGACY BASE64 (old data only)
    thumb = getattr(act, "thumbnail", None)
    if _looks_like_base64_blob(thumb):
        try:
            img_bytes = base64.b64decode(thumb)
            return Response(content=img_bytes, media_type="image/jpeg")
        except Exception:
            pass

    # 3. LOCAL STORAGE — support org-prefixed subdirectory paths
    if act.file_path:
        fp = act.file_path.replace("\\", "/")
        base = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        uploads_root = os.path.normpath(
            os.path.join(base, "..", "tracker-data", "uploads")
        )
        # Use relative path to handle both /uploads/file.webp and
        # /uploads/org_1/file.webp storage layouts
        relative = fp[len("/uploads/"):] if fp.startswith("/uploads/") else os.path.basename(fp)
        disk_path = os.path.normpath(os.path.join(uploads_root, relative))

        # Guard against path traversal
        if disk_path.startswith(uploads_root + os.sep) or disk_path == uploads_root:
            if os.path.isfile(disk_path):
                with open(disk_path, "rb") as f:
                    return Response(content=f.read(), media_type="image/jpeg")

    raise HTTPException(status_code=404, detail="Image not available")


@router.delete("/screenshots/{screenshot_id}")
def delete_screenshot(
    screenshot_id: int,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    ok = service.delete_screenshot(db, screenshot_id, organization_id=current_org.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return {"success": True}


# ── Timelapse endpoints ──────────────────────────────────────────────────────

@router.post("/screenshots/timelapse")
def create_timelapse(
    user_id: int = Query(...),
    date_filter: date = Query(..., alias="date"),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """
    Generate a browser-compatible H.264 MP4 timelapse.
    FPS is auto-calculated so duration is always 15-30 s regardless of screenshot count.
    """
    if not _TIMELAPSE_ENABLED:
        raise HTTPException(status_code=503, detail="Timelapse feature is temporarily disabled")
    # Validate the requested user belongs to the caller's organization
    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user or target_user.organization_id != current_org.id:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        mp4_bytes, frame_count, actual_fps, skipped = service.generate_timelapse(
            db,
            user_id=user_id,
            date_filter=date_filter,
            organization_id=current_org.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    os.makedirs(_TIMELAPSE_DIR, exist_ok=True)
    filename = f"timelapse_{current_org.id}_{user_id}_{date_filter}.mp4"
    filepath = os.path.join(_TIMELAPSE_DIR, filename)
    with open(filepath, "wb") as fh:
        fh.write(mp4_bytes)

    return {
        "video_url": f"/api/screenshots/timelapse/{filename}",
        "filename":  filename,
        "size_bytes": len(mp4_bytes),
        "frame_count": frame_count,
        "fps": actual_fps,
        "duration_seconds": round(frame_count / actual_fps, 1),
        "skipped": skipped,
    }


@router.get("/screenshots/timelapse/{filename}")
def stream_timelapse(
    filename: str,
    _: models.User = Depends(get_current_user),
):
    """Stream a previously generated timelapse MP4."""
    if not _TIMELAPSE_ENABLED:
        raise HTTPException(status_code=503, detail="Timelapse feature is temporarily disabled")
    filename = os.path.basename(filename)  # prevent path traversal
    filepath = os.path.join(_TIMELAPSE_DIR, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Timelapse video not found")
    return FileResponse(filepath, media_type="video/mp4", filename=filename)


@router.delete("/screenshots/timelapse/{filename}")
def delete_timelapse(
    filename: str,
    _: models.User = Depends(get_current_user),
):
    """Delete a generated timelapse MP4."""
    if not _TIMELAPSE_ENABLED:
        raise HTTPException(status_code=503, detail="Timelapse feature is temporarily disabled")
    filename = os.path.basename(filename)  # prevent path traversal
    filepath = os.path.join(_TIMELAPSE_DIR, filename)
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass
    return {"success": True}
