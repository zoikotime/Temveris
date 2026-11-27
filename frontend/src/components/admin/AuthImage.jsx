import React, { useEffect, useRef, useState } from "react";
import { readToken } from "../../api/authStorage";

// AuthImage — drop-in replacement for <img> that can display the backend's
// auth-protected screenshot route.
//
// Background: GET /api/screenshots/{id}/image requires a Bearer token + org
// check (screenshots/router.py). A plain <img src> cannot send an
// Authorization header, so it returns 401 and the screenshot fails to open.
// For those URLs we fetch the bytes with the token and render them via an
// object URL. Every other URL (base64 data:, Supabase public/signed http URL,
// attachment links) is rendered directly — identical to the previous <img> —
// so this is backward compatible.
//
// Blob cache: fetched images are cached by URL (as Blobs, not object URLs so
// revocation on unmount doesn't poison the cache). The lightbox mounts a new
// AuthImage instance for the same URL the thumbnail already loaded — without
// the cache that causes a full re-fetch and a 1-2s delay every time you click
// "Open". With the cache the lightbox renders instantly.
//
// Note: API_BASE is absolute (https://…), so the protected URL also starts
// with "http". We therefore detect our protected route by its PATH, not by
// the scheme.
const PROTECTED_IMAGE_RE = /\/api\/screenshots\/\d+\/image\b/;

// Module-level Blob cache — survives component unmount/remount within a
// session. Capped at 120 entries (insertion-order eviction) to bound memory.
const _MAX_CACHE = 120;
const _blobCache = new Map(); // url → Blob

function _getCached(url) {
  return _blobCache.get(url) || null;
}

function _setCached(url, blob) {
  if (_blobCache.size >= _MAX_CACHE) {
    _blobCache.delete(_blobCache.keys().next().value);
  }
  _blobCache.set(url, blob);
}

export default function AuthImage({
  url,
  token,
  onError,
  alt = "",
  style,
  eager = false, // lightboxes pass eager; grids stay lazy to avoid 500 fetches
  rootMargin = "300px",
  ...rest
}) {
  const needsAuth = typeof url === "string" && PROTECTED_IMAGE_RE.test(url);
  // Direct URLs resolve immediately; protected URLs wait for the blob fetch.
  const [src, setSrc] = useState(needsAuth ? null : url);
  const [failed, setFailed] = useState(false);
  const [inView, setInView] = useState(!needsAuth || eager);
  const nodeRef = useRef(null);
  const objUrlRef = useRef(null);

  // Lazy gate: only fetch a protected image once it is near the viewport.
  useEffect(() => {
    if (!needsAuth || inView) return;
    const el = nodeRef.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          io.disconnect();
        }
      },
      { rootMargin },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [needsAuth, inView, rootMargin]);

  // Resolve the source: pass through for direct URLs, blob-fetch for protected.
  useEffect(() => {
    setFailed(false);
    if (!needsAuth) {
      setSrc(url);
      return;
    }
    if (!inView) return;

    // Cache hit — create a fresh object URL from the cached Blob instantly.
    const cached = _getCached(url);
    if (cached) {
      const obj = URL.createObjectURL(cached);
      objUrlRef.current = obj;
      setSrc(obj);
      return () => {
        URL.revokeObjectURL(obj);
        objUrlRef.current = null;
      };
    }

    setSrc(null);
    let alive = true;
    const bearer = token || readToken();
    fetch(url, { headers: bearer ? { Authorization: `Bearer ${bearer}` } : {} })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (!alive) return;
        _setCached(url, blob); // cache before creating the object URL
        const obj = URL.createObjectURL(blob);
        objUrlRef.current = obj;
        setSrc(obj);
      })
      .catch(() => {
        if (!alive) return;
        setFailed(true);
        if (onError) onError();
      });
    return () => {
      alive = false;
      if (objUrlRef.current) {
        URL.revokeObjectURL(objUrlRef.current);
        objUrlRef.current = null;
      }
    };
    // onError intentionally excluded — callers pass inline arrows; including it
    // would refetch on every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, token, needsAuth, inView]);

  if (failed) return null; // matches the previous "hide on error" behaviour

  return (
    <img
      ref={nodeRef}
      src={src || undefined}
      alt={alt}
      style={{
        ...style,
        // Hide broken-image icon while fetching. Element stays in the DOM so
        // IntersectionObserver can still fire the lazy-load trigger.
        visibility: (needsAuth && !src) ? "hidden" : undefined,
      }}
      onError={() => {
        setFailed(true);
        if (onError) onError();
      }}
      {...rest}
    />
  );
}
