# Temveris Tracker — Browser Extension

Reports the active browser tab (URL, title, domain, favicon, interaction
counters) to the Temveris backend so the admin dashboard can show exactly
which website each employee is working on — not just "Chrome".

## Install (developer mode)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Enable **Developer mode** (top-right).
3. Click **Load unpacked** and pick this folder (`browser-extension/`).
4. Click the Temveris icon in the toolbar.
5. Paste:
   - **API base URL** — the same URL the web dashboard uses (e.g. `https://api.yourcompany.com`).
   - **Access token (JWT)** — copy from the web dashboard's localStorage
     (DevTools → Application → Local Storage → key `token`).
6. Click **Save & connect**. Tracking starts on the next tab switch.

## What it sends

- **Heartbeat** (every 15s while a tab is active): `POST /browser/heartbeat`
- **Tab session** (on tab switch, close, or browser blur): `POST /browser/event`

Interaction counters (clicks, keys, scrolls) are collected **as counts only** —
never keystroke contents or click coordinates.

## What it does NOT send

- Form inputs, passwords, or page contents.
- Private/incognito window activity.
- `chrome://`, `about:`, or extension URLs.

## Files

- `manifest.json` — MV3 manifest.
- `background.js` — service worker: tab tracking, heartbeats, session management.
- `content.js` — per-page interaction counter.
- `popup.html` / `popup.js` — config UI for API URL + token.
