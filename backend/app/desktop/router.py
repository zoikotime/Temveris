"""
Temveris — Desktop release metadata router.

Exposes:

    GET /desktop/latest?platform=win

Response contract (matches the in-app banner spec):

    {
      "latest_version": "1.0.11",
      "minimum_supported_version": "1.0.7",
      "download_url":   "https://.../Temveris-Setup-1.0.11.exe",
      "release_notes":  ["...", "...", "..."],
      "platform":       "win",
      "released_at":    "2026-06-01T06:15:48Z"
    }

WHY THIS EXISTS
  Old 1.0.7 desktop clients can't reach the new generic-provider auto-update
  channel (their installed electron-updater is hard-pointed at a private
  GitHub repo and silently 404s on the client). This endpoint feeds the
  React /electron page so it can show an in-app "Update Available" banner
  with a one-click download URL. The frontend gates the banner behind
  window.isDesktopApp === true, so browser users never see it.

DATA SOURCES
  1. latest.yml on Supabase Storage (source of truth for VERSION + installer
     filename) — same file electron-updater itself reads on 1.0.11+ clients.
  2. releases.json bundled with the backend (release-notes catalogue,
     keyed by version). Editable without code change — just commit + deploy.
  3. DESKTOP_MIN_VERSION env var — the minimum_supported_version policy
     knob, so ops can bump it later without a code change.

There is NO database I/O in this router. The endpoint is safe to hit on
every dashboard render; the 60s in-process cache absorbs the bulk of the
load.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse


router = APIRouter(prefix="/desktop", tags=["Desktop App"])


# ─── Config ─────────────────────────────────────────────────────────────────
SUPABASE_URL   = (os.getenv("SUPABASE_URL") or "").rstrip("/")
UPDATES_BUCKET = os.getenv("DESKTOP_UPDATES_BUCKET", "desktop-updates")
# Override-able floor for the "minimum_supported_version" policy. The banner
# still allows Later for now (per spec); we expose this so a future release
# can flip the banner to force-update mode by simply bumping the env var.
DEFAULT_MIN_VERSION = "1.0.7"

# Bundled release-notes catalogue. Resolved relative to this file so it
# travels with the backend image regardless of the runtime cwd.
RELEASES_JSON_PATH = Path(__file__).parent / "releases.json"


# ─── In-process caches ──────────────────────────────────────────────────────
# Both caches are intentionally tiny + short-lived. No persistence — a
# backend restart wipes them and the next request rehydrates.
_MANIFEST_CACHE: dict[str, tuple[float, dict]] = {}  # keyed by platform
_MANIFEST_TTL_SECS = 60.0

_RELEASES_CACHE: tuple[float, dict] | None = None
_RELEASES_TTL_SECS = 60.0


def _public_url(platform: str, filename: str) -> str:
    return (
        f"{SUPABASE_URL}/storage/v1/object/public/"
        f"{UPDATES_BUCKET}/{platform}/{filename}"
    )


def _parse_latest_yml(text: str) -> dict:
    """Extract version + path from electron-builder's latest.yml.

    Schema is fixed by electron-builder and ships these two fields on every
    write, so a 2-line regex is more reliable than pulling in PyYAML for
    one file format we already trust.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*version:\s*(\S.*?)\s*$", line)
        if m and "version" not in out:
            out["version"] = m.group(1)
            continue
        m = re.match(r"^\s*path:\s*(\S.*?)\s*$", line)
        if m and "path" not in out:
            out["path"] = m.group(1)
            continue
        m = re.match(r"^\s*releaseDate:\s*['\"]?(\S.*?)['\"]?\s*$", line)
        if m and "releaseDate" not in out:
            out["releaseDate"] = m.group(1).strip("'\"")
    return out


def _load_releases_catalogue() -> dict:
    """Return parsed releases.json, cached briefly.

    A bad or missing file is NOT fatal — we degrade to {} and let callers
    fall back to default notes. That keeps the banner functional during a
    bad deploy.
    """
    global _RELEASES_CACHE
    now = time.time()
    if _RELEASES_CACHE and (now - _RELEASES_CACHE[0]) < _RELEASES_TTL_SECS:
        return _RELEASES_CACHE[1]
    try:
        with RELEASES_JSON_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _RELEASES_CACHE = (now, data)
    return data


def _release_notes_for(version: str) -> list[str]:
    cat = _load_releases_catalogue()
    versions = cat.get("versions") or {}
    entry = versions.get(version) or {}
    notes = entry.get("notes")
    if isinstance(notes, list) and notes:
        return [str(n) for n in notes][:8]  # cap UI length defensively
    fallback = cat.get("default_notes")
    if isinstance(fallback, list) and fallback:
        return [str(n) for n in fallback]
    return ["Stability and performance improvements"]


def _minimum_supported_version() -> str:
    # Env var wins; releases.json provides a back-compat path so ops can also
    # bump the floor by editing the file if they prefer config-as-data.
    env = (os.getenv("DESKTOP_MIN_VERSION") or "").strip()
    if env:
        return env
    cat = _load_releases_catalogue()
    cfg = cat.get("min_supported_version")
    if isinstance(cfg, str) and cfg.strip():
        return cfg.strip()
    return DEFAULT_MIN_VERSION


def _fetch_manifest(platform: str) -> dict:
    """Returns {latest_version, download_url, released_at, manifest_url}.

    Raises HTTPException on hard misses (bucket missing, manifest absent,
    manifest malformed). Network blips inside the TTL are masked by the cache.
    """
    now = time.time()
    cached = _MANIFEST_CACHE.get(platform)
    if cached and (now - cached[0]) < _MANIFEST_TTL_SECS:
        return cached[1]

    if not SUPABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_URL is not configured on the backend",
        )

    url = _public_url(platform, "latest.yml")
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 404 here almost always means: bucket exists but no release uploaded
        # yet, OR bucket exists but is not public. 503 (not 500) lets the
        # frontend's catch path hide the banner cleanly without sentry noise.
        raise HTTPException(
            status_code=503,
            detail=f"latest.yml unavailable at {url} (HTTP {e.code})",
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not fetch latest.yml: {type(e).__name__}: {e}",
        )

    parsed = _parse_latest_yml(body)
    version = parsed.get("version")
    installer = parsed.get("path")
    if not version or not installer:
        raise HTTPException(
            status_code=503,
            detail="latest.yml is missing required fields (version, path)",
        )

    result = {
        "latest_version":  version,
        "download_url":    _public_url(platform, installer),
        "manifest_url":    url,
        "platform":        platform,
        "released_at":     parsed.get("releaseDate"),
    }
    _MANIFEST_CACHE[platform] = (now, result)
    return result


# ─── ROUTES ─────────────────────────────────────────────────────────────────

@router.get("/latest")
def get_latest_release(
    platform: str = Query("win", pattern="^(win|mac|linux)$"),
):
    """Return the latest desktop version + a direct installer download URL.

    Used by:
      - ElectronDashboard.jsx in-app update banner (compares against UA-
        reported client version, prompts download if stale).
      - Admin "About" page (future).
    """
    manifest = _fetch_manifest(platform)
    return {
        "latest_version":            manifest["latest_version"],
        "minimum_supported_version": _minimum_supported_version(),
        "download_url":              manifest["download_url"],
        "release_notes":             _release_notes_for(manifest["latest_version"]),
        "platform":                  manifest["platform"],
        "released_at":               manifest["released_at"],
    }


@router.get("/download/{platform}")
def download_redirect(platform: str):
    """Convenience deep-link so unauthenticated browser links work too.

    `/desktop/download/win` → 302 to the actual public installer URL.
    Useful for the IT email blast and any future "Get the app" buttons.
    """
    if platform not in ("win", "mac", "linux"):
        raise HTTPException(status_code=400, detail="Unknown platform")
    manifest = _fetch_manifest(platform)
    # 302 so the browser hits the public Supabase URL directly — no proxying
    # 80 MB installers through Cloud Run, which would blow request limits.
    return RedirectResponse(url=manifest["download_url"], status_code=302)
