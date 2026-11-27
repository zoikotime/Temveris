# Temveris Desktop — Release Process

Auto-update channel: **Supabase Storage public bucket** (`YOUR_UPDATES_BUCKET`), consumed by `electron-updater` with `provider: generic`. The repo stays private; only the signed installer binaries are public.

---

## One-time setup

### 1. Create the public Supabase bucket

Supabase dashboard → **Storage** → **New bucket**

| Field            | Value             |
|------------------|-------------------|
| Name             | `YOUR_UPDATES_BUCKET` (scripts default to `desktop-updates`; set `UPDATES_BUCKET` and the backend's `DESKTOP_UPDATES_BUCKET` if you pick another name) |
| Public bucket    | **ON**            |
| File size limit  | 200 MB            |
| Allowed MIME     | leave empty       |

Set `build.publish.url` in `desktop-tracker/package.json` (currently the placeholder `https://updates.temveris.example/${os}/`) to `https://YOUR_SUPABASE_PROJECT_REF.supabase.co/storage/v1/object/public/YOUR_UPDATES_BUCKET/${os}/` so installed apps read this bucket.

### 2. Provide release-time credentials

The uploader needs two env vars at release time. Set them on the release machine only:

```powershell
$env:SUPABASE_URL              = "https://YOUR_SUPABASE_PROJECT_REF.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "YOUR_SUPABASE_SERVICE_ROLE_KEY"   # never commit
```

These are *only* needed on the release machine / CI. The desktop client never sees them.

---

## Releasing a new version

From `desktop-tracker/`:

1. Bump `package.json` → `version` (e.g. `1.0.11` → `1.0.12`).
2. Commit + tag (`git tag v1.0.12`).
3. Run:
   ```powershell
   npm run release:win
   ```
   This:
   - runs `electron-builder --win` → writes `dist/Temveris-Setup-1.0.12.exe`, `.exe.blockmap`, `latest.yml`
   - runs `node scripts/publish-update.js win` → uploads all three to `YOUR_UPDATES_BUCKET/win/` and verifies the public URL responds with the new manifest
4. Push the tag: `git push origin v1.0.12`.

The script uploads installer + blockmap **first**, then `latest.yml` **last** — so any client checking mid-upload either sees the old release or the complete new one, never a manifest pointing at a missing binary.

---

## What clients do

| Client version | Path |
|----------------|------|
| **v1.0.7 and older** | Their installed `electron-updater` is hard-pointed at the private GitHub repo and 404s on every check. The React `/electron` page detects the old user-agent and shows a **rescue banner** ("Update available — Download v1.0.12"). Clicking it opens the public installer URL in the user's real browser via `shell.openExternal`. Once they install v1.0.12, they join the auto-update channel. |
| **v1.0.11+** | `electron-updater` polls `https://YOUR_SUPABASE_PROJECT_REF.supabase.co/storage/v1/object/public/YOUR_UPDATES_BUCKET/win/latest.yml` every 4 h. On a new release: downloads in the background, shows a tray notification, and the **"Restart & install update"** tray item runs `quitAndInstall()` for a silent NSIS install (`nsis.oneClick: true`). |

---

## Rescue email template (one-time, for the 1.0.7 fleet)

Send when the new channel goes live, so users who rarely open the tracker popup don't wait for the in-app banner:

> **Subject:** Temveris desktop — please update to v1.0.11
>
> Hi team,
>
> A new version of Temveris is available with several reliability fixes. Please install it at your earliest convenience — it will take about 30 seconds.
>
> **Download:** https://YOUR_SUPABASE_PROJECT_REF.supabase.co/storage/v1/object/public/YOUR_UPDATES_BUCKET/win/Temveris-Setup-1.0.11.exe
>
> (Or: open Temveris, click the green "Update available" banner at the top.)
>
> After this update, future releases will install automatically — no manual download required.
>
> Thanks,
> IT

Replace the version + filename for the actual release you ship.

---

## Verifying after release

```powershell
# manifest reachable, public, and matches the new version
curl https://YOUR_SUPABASE_PROJECT_REF.supabase.co/storage/v1/object/public/YOUR_UPDATES_BUCKET/win/latest.yml

# installer reachable
curl -I https://YOUR_SUPABASE_PROJECT_REF.supabase.co/storage/v1/object/public/YOUR_UPDATES_BUCKET/win/Temveris-Setup-1.0.12.exe
```

You can also hit the backend convenience endpoint:

```
GET https://YOUR_APP_DOMAIN/desktop/latest?platform=win
→ { "version": "1.0.12", "download_url": "...", "platform": "win" }
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Banner doesn't appear in /electron | Running in a browser tab (no `temveris-desktop/x.y.z` in UA), or already on latest, or user dismissed this version | Expected; nothing to do |
| Banner appears but Download does nothing | Electron `setWindowOpenHandler` not wired (very old build) | Send the URL via email instead |
| `publish-update.js` reports HTTP 401 on upload | `SUPABASE_SERVICE_ROLE_KEY` missing or wrong | Re-export from `backend/.env` |
| `publish-update.js` reports "Public verification failed" | `YOUR_UPDATES_BUCKET` bucket exists but is private | Toggle the bucket's "Public" switch in Supabase |
| `/desktop/latest` returns 503 | Backend can't reach `latest.yml` (no release uploaded yet, or bucket not public) | Run a release; verify the bucket is public |
| Auto-update on v1.0.11 client never fires | Check `[updater]` lines in the client log — if `Error: HTTP 404`, the manifest path is wrong in `package.json` | Verify `publish.url` in `desktop-tracker/package.json` matches the bucket layout |

---

## Why we're not on the GitHub provider

Pre-2026-06 releases used `provider: github` against a **private** repo. `electron-updater` fetches `latest.yml` *unauthenticated* and got 404 on every client. Embedding a GitHub token in the installer to bypass this would publish the token to anyone who decompiles the app, which effectively makes the private repo readable. The generic provider against a public Supabase bucket is the cleaner separation: source code stays private, binaries are public (and they're signed installers anyway — public exposure of the binary itself is expected).
