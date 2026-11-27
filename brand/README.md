# Brand masters

The logo and icon files in the app are **placeholders** (a single-colour
"Temveris" wordmark and a square "T" icon) until real artwork is supplied.

To replace them, drop these files here and rerun the generator:

| File | What it is |
|---|---|
| `icon.png` | Square app icon, at least 1024 px, transparent background |
| `logo.png` | Horizontal wordmark, at least 1600 px wide, transparent background |
| `icon.svg` | Vector icon (becomes the favicon) |
| `logo.svg` | Vector wordmark for light backgrounds |
| `logo-white.svg` | Vector wordmark for the dark sidebar |

Supply each group complete — `icon.png` + `icon.svg`, and `logo.png` +
`logo.svg` + `logo-white.svg`. The script refuses a partial group, so a real
raster never ships next to a placeholder vector.

```
backend/venv/Scripts/python brand/build_assets.py
```

It rewrites, keeping the same file names: the web app logos and favicon
(`frontend/src/assets/Temveris_*`, `frontend/public/favicon.*`, which is also
the og:image), the email header logo (`backend/app/email_assets/temveris_logo.png`),
the desktop icons (`desktop-tracker/assets/icon.{png,ico,icns}`, `icons/NxN.png`)
and the browser-extension icons. Brand colours (`frontend/tailwind.config.js`,
`BRAND` in `backend/app/email_service.py`) are changed separately.
