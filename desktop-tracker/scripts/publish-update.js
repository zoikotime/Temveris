#!/usr/bin/env node
/**
 * publish-update.js — uploads the electron-builder output to the public
 * Supabase Storage bucket that electron-updater reads from at runtime.
 *
 * WHY THIS EXISTS:
 *   The Temveris repo is private, so electron-updater's default GitHub
 *   provider cannot fetch latest.yml from clients (GitHub returns 404 on
 *   unauthenticated private-repo asset URLs). We host the manifest +
 *   binaries on Supabase Storage instead, which is public-by-default and
 *   needs no token on the client side.
 *
 * WHY TUS (RESUMABLE) UPLOAD:
 *   Supabase's plain `POST /storage/v1/object/...` endpoint caps a single
 *   request at 50 MB (Free plan). Our NSIS installer is ~75 MB, so the old
 *   single-POST path FAILED SILENTLY on the .exe — the tiny latest.yml would
 *   upload fine while the binary it points at never landed, leaving the
 *   channel advertising a version whose installer 404s (or, as actually
 *   happened, leaving the channel pinned to the previous release because
 *   nobody re-ran the throwaway TUS script). The resumable endpoint at
 *   `/storage/v1/upload/resumable` accepts arbitrarily large files via 6 MB
 *   chunks, so every artifact — installer, blockmap, manifest — goes through
 *   the SAME code path here and the 50 MB cap is irrelevant.
 *
 * USAGE:
 *   SUPABASE_URL=...                  (e.g. https://xxx.supabase.co)
 *   SUPABASE_SERVICE_ROLE_KEY=...     (from backend/.env or CI secret)
 *   node scripts/publish-update.js [win|mac|linux]
 *
 * Defaults: platform = host platform (win on Windows, etc.), bucket
 * = "desktop-updates" (override via UPDATES_BUCKET env var).
 *
 * The bucket layout matches the publish.url in package.json:
 *   desktop-updates/win/latest.yml
 *   desktop-updates/win/Temveris-Setup-X.Y.Z.exe
 *   desktop-updates/win/Temveris-Setup-X.Y.Z.exe.blockmap
 */

"use strict";

const fs   = require("fs");
const path = require("path");

// ── ARG / ENV PARSING ───────────────────────────────────────────────────────
const PLATFORM = (process.argv[2] || guessPlatform()).toLowerCase();
const VALID = new Set(["win", "mac", "linux"]);
if (!VALID.has(PLATFORM)) {
  fail(`Unknown platform "${PLATFORM}" — expected win, mac, or linux.`);
}

const SUPABASE_URL = (process.env.SUPABASE_URL || "").replace(/\/+$/, "");
const SERVICE_KEY  = process.env.SUPABASE_SERVICE_ROLE_KEY || "";
const BUCKET       = process.env.UPDATES_BUCKET || "desktop-updates";
const CHUNK_SIZE   = 6 * 1024 * 1024; // 6 MiB — Supabase's recommended TUS chunk

if (!SUPABASE_URL)  fail("SUPABASE_URL env var is required.");
if (!SERVICE_KEY)   fail("SUPABASE_SERVICE_ROLE_KEY env var is required.");
if (typeof fetch !== "function") {
  fail("global fetch is unavailable — run with Node 18+ (CI uses Node 20).");
}

// ── FILE DISCOVERY ──────────────────────────────────────────────────────────
// electron-builder writes everything to dist/ relative to package.json. We
// pick up the manifest first because it tells us the version, and from that
// derive the installer + blockmap filenames we expect.
const DIST_DIR = path.resolve(__dirname, "..", "dist");
if (!fs.existsSync(DIST_DIR)) {
  fail(`dist/ directory not found at ${DIST_DIR} — run electron-builder first.`);
}

const MANIFEST_NAME = "latest.yml";   // same name for all platforms
const manifestPath  = path.join(DIST_DIR, MANIFEST_NAME);
if (!fs.existsSync(manifestPath)) {
  fail(`${MANIFEST_NAME} not found in dist/ — did electron-builder finish?`);
}

// Tiny YAML scan — we only need the "files:" list, the "path:" entry and the
// "version:" field, no full parser. electron-builder always emits these fields,
// so a regex is safe.
const manifest = fs.readFileSync(manifestPath, "utf-8");
const manifestVersion = (manifest.match(/^\s*version:\s*(.+\S)\s*$/m) || [])[1] || "?";

// Cross-check the manifest against package.json so we never publish a manifest
// whose version disagrees with the build that produced it.
const pkg = require(path.resolve(__dirname, "..", "package.json"));
if (pkg.version && manifestVersion !== "?" && pkg.version !== manifestVersion) {
  fail(
    `Version mismatch: package.json=${pkg.version} but dist/latest.yml=${manifestVersion}. ` +
    `Rebuild before publishing (the manifest must match the installer that's in dist/).`,
  );
}

const fileNames = new Set();
manifest.split(/\r?\n/).forEach((line) => {
  let m = line.match(/^\s*-\s+url:\s*(.+\S)\s*$/);
  if (m) fileNames.add(decodeURIComponent(m[1].trim()));
  m = line.match(/^\s*path:\s*(.+\S)\s*$/);
  if (m) fileNames.add(decodeURIComponent(m[1].trim()));
});

// Each installer in latest.yml typically has a sibling .blockmap that
// electron-updater uses for differential download. Add it explicitly so we
// don't skip it on platforms where latest.yml doesn't reference it.
const toUpload = new Set();
fileNames.forEach((f) => {
  toUpload.add(f);
  if (/\.(exe|dmg|AppImage|zip)$/i.test(f)) {
    const blockmap = f + ".blockmap";
    if (fs.existsSync(path.join(DIST_DIR, blockmap))) toUpload.add(blockmap);
  }
});
toUpload.add(MANIFEST_NAME); // always upload the manifest last; see below

if (toUpload.size === 0) {
  fail("No files discovered in latest.yml — aborting.");
}

// ── UPLOAD ──────────────────────────────────────────────────────────────────
// IMPORTANT: upload the installer + blockmap FIRST, then the manifest LAST.
// electron-updater on a client may race a check-for-updates with our upload;
// if it sees a new manifest before the binary is reachable it will fail the
// download with a 404 and back off. Manifest-last makes the rollout atomic
// from the client's point of view.
const orderedUploads = [...toUpload]
  .filter((f) => f !== MANIFEST_NAME)
  .concat([MANIFEST_NAME]);

(async () => {
  console.log(`[publish] version : ${manifestVersion}`);
  console.log(`[publish] target  : ${SUPABASE_URL}/storage/v1/object/${BUCKET}/${PLATFORM}/`);
  console.log(`[publish] files   : ${orderedUploads.join(", ")}`);
  for (const name of orderedUploads) {
    const localPath = path.join(DIST_DIR, name);
    if (!fs.existsSync(localPath)) {
      // The manifest and installer must exist; a missing blockmap is tolerable
      // (older configs), but a missing installer/manifest is fatal.
      if (name === MANIFEST_NAME || /\.(exe|dmg|AppImage|zip)$/i.test(name)) {
        fail(`Required artifact ${name} missing on disk at ${localPath}.`);
      }
      console.warn(`[publish] SKIP — ${name} missing on disk`);
      continue;
    }
    await uploadViaTus(name, localPath);
  }

  // Final sanity: hit the public URL of latest.yml and verify the content
  // matches what we just uploaded. Catches the embarrassing failure mode
  // where the bucket exists but is not actually public, or a CDN served a
  // stale copy.
  const publicYml = `${SUPABASE_URL}/storage/v1/object/public/${BUCKET}/${PLATFORM}/${MANIFEST_NAME}`;
  console.log(`[publish] verify  : GET ${publicYml}`);
  // Retry a few times — object storage can take a beat to be readable through
  // the public path right after an upsert, and we'd rather wait than fail a
  // release that actually succeeded.
  let fetched = null;
  let lastErr = "";
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(publicYml, { cache: "no-store" });
      if (res.ok) {
        const body = await res.text();
        if (body.trim() === manifest.trim()) { fetched = body; break; }
        lastErr = "content did not match uploaded manifest";
      } else {
        lastErr = `HTTP ${res.status}`;
      }
    } catch (e) {
      lastErr = e && e.message ? e.message : String(e);
    }
    if (attempt < 4) await new Promise((r) => setTimeout(r, 2000));
  }
  if (!fetched) {
    fail(
      `Public verification failed (${lastErr}). Confirm the "${BUCKET}" bucket is ` +
      `PUBLIC and the upload completed; the channel may NOT be live.`,
    );
  }
  const liveVersion = (fetched.match(/^\s*version:\s*(.+\S)\s*$/m) || [])[1] || "?";
  if (liveVersion !== manifestVersion) {
    fail(`Live latest.yml advertises ${liveVersion} but we published ${manifestVersion}.`);
  }
  console.log(`[publish] DONE — auto-update channel is live on v${liveVersion}.`);
})().catch((err) => fail(err && err.message ? err.message : String(err)));

// ── HELPERS ─────────────────────────────────────────────────────────────────

// Resumable (TUS 1.0.0) upload to Supabase Storage. Works for any file size,
// so it sidesteps the 50 MB single-POST cap that silently broke the old
// uploader on the installer. x-upsert overwrites the object in place so each
// release replaces the previous artifact at the same key.
async function uploadViaTus(name, localPath) {
  const objectName  = `${PLATFORM}/${name}`;        // key WITHIN the bucket
  const ctype       = contentTypeFor(name);
  // latest.yml must never be cached by the CDN or a client could keep seeing an
  // old version after a release; binaries are content-addressed by version in
  // their filename, so they're safe to cache for a while.
  const cache       = name === MANIFEST_NAME ? "no-cache" : "max-age=3600";
  const size        = fs.statSync(localPath).size;

  const metadata = [
    ["bucketName",  BUCKET],
    ["objectName",  objectName],
    ["contentType", ctype],
    ["cacheControl", cache],
  ]
    .map(([k, v]) => `${k} ${Buffer.from(String(v), "utf-8").toString("base64")}`)
    .join(",");

  // Step 1 — create the upload session.
  const createRes = await fetch(`${SUPABASE_URL}/storage/v1/upload/resumable`, {
    method: "POST",
    headers: {
      Authorization:    `Bearer ${SERVICE_KEY}`,
      apikey:           SERVICE_KEY,
      "Tus-Resumable":  "1.0.0",
      "Upload-Length":  String(size),
      "Upload-Metadata": metadata,
      "x-upsert":       "true",
      "Content-Length": "0",
    },
  });
  // TUS spec returns 201 Created; accept any 2xx so a minor status variation
  // from the gateway can't fail a working upload. A missing Location IS fatal.
  if (!createRes.ok) {
    const body = await createRes.text().catch(() => "");
    throw new Error(
      `TUS create failed for ${objectName}: HTTP ${createRes.status} — ${body.slice(0, 300)}`,
    );
  }
  let uploadUrl = createRes.headers.get("location") || createRes.headers.get("Location");
  if (!uploadUrl) throw new Error(`TUS create for ${objectName} returned no Location header.`);
  if (uploadUrl.startsWith("/")) {
    const u = new URL(SUPABASE_URL);
    uploadUrl = `${u.protocol}//${u.host}${uploadUrl}`;
  }

  // Step 2 — PATCH chunks until offset === size.
  const fd  = fs.openSync(localPath, "r");
  const buf = Buffer.allocUnsafe(CHUNK_SIZE);
  try {
    let offset = 0;
    while (offset < size) {
      const bytesRead = fs.readSync(fd, buf, 0, CHUNK_SIZE, offset);
      if (bytesRead <= 0) break;
      const chunk = buf.subarray(0, bytesRead);
      const patchRes = await fetch(uploadUrl, {
        method: "PATCH",
        headers: {
          Authorization:    `Bearer ${SERVICE_KEY}`,
          apikey:           SERVICE_KEY,
          "Tus-Resumable":  "1.0.0",
          "Upload-Offset":  String(offset),
          "Content-Type":   "application/offset+octet-stream",
          "Content-Length": String(bytesRead),
        },
        body: chunk,
      });
      // TUS spec returns 204 No Content; accept any 2xx for gateway tolerance.
      if (!patchRes.ok) {
        const body = await patchRes.text().catch(() => "");
        throw new Error(
          `TUS chunk failed at offset ${offset} for ${objectName}: ` +
          `HTTP ${patchRes.status} — ${body.slice(0, 300)}`,
        );
      }
      const next = parseInt(
        patchRes.headers.get("upload-offset") ||
        patchRes.headers.get("Upload-Offset") ||
        String(offset + bytesRead),
        10,
      );
      offset = Number.isFinite(next) ? next : offset + bytesRead;
    }
    const kb = Math.round(size / 1024);
    console.log(`[publish] up      : ${objectName} (${kb} KB) → ${ctype} [${cache}]`);
  } finally {
    fs.closeSync(fd);
  }
}

function contentTypeFor(name) {
  if (name.endsWith(".yml") || name.endsWith(".yaml")) return "application/x-yaml";
  if (name.endsWith(".exe"))                            return "application/vnd.microsoft.portable-executable";
  if (name.endsWith(".dmg"))                            return "application/x-apple-diskimage";
  if (name.endsWith(".zip"))                            return "application/zip";
  if (name.endsWith(".AppImage"))                       return "application/x-executable";
  if (name.endsWith(".blockmap"))                       return "application/octet-stream";
  return "application/octet-stream";
}

function guessPlatform() {
  switch (process.platform) {
    case "win32":  return "win";
    case "darwin": return "mac";
    default:        return "linux";
  }
}

function fail(msg) {
  console.error(`[publish] ERROR: ${msg}`);
  process.exit(1);
}
