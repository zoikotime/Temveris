# Temveris — placeholders and open items

The rebrand is done in code, but several values are **placeholders**
because no real Temveris domain, mailbox or artwork exists yet. Replace each one
before any public deployment, then delete its row.

## 1. Placeholders to replace

`temveris.example` is a reserved, non-resolving domain. Search the repo for
`temveris.example` to find every use.

| Placeholder | Where it is used | Replace with |
|---|---|---|
| `https://app.temveris.example` (web app origin) | `VITE_PUBLIC_ORIGIN` default (`frontend/src/config.js`, `frontend/src/lib/supabase.js`, `frontend/.env.example`); desktop `WEBSITE_URL` default (`desktop-tracker/main.js`, override with env `ZOIKO_URL`); `UNSUBSCRIBE_URL_BASE` and `EMAIL_LOGO_URL` defaults (`backend/app/notifications/suppression.py`, `backend/app/email_service.py`); desktop `homepage`; canonical/og/twitter URLs in `frontend/index.html`; Kairos `customer_application` / `campaign_redirect_domain` | Real app URL |
| `https://temveris.example` (+ `/start`, `/contact`, `/pricing`) | Kairos knowledge base (`marketing_site`, signup and contact links), comments | Real marketing site |
| `https://api.temveris.example` | Kairos `api_surface`; browser-extension popup placeholder; comment in `backend/main.py` | Real API URL |
| `https://admin.temveris.example`, `https://status.temveris.example` | Kairos knowledge base | Real URLs, or remove |
| `https://docs.temveris.example/security/recovery` | "View recovery policy" button, `frontend/src/components/workspace/SecurityCenter.jsx` | Real docs page |
| `https://updates.temveris.example/${os}/` | Electron auto-update feed, `desktop-tracker/package.json` → `build.publish.url` | Real update host (bucket/CDN) |
| `https://updates.temveris.example/{win,mac,linux}/…` | Installer downloads: `DESKTOP_DOWNLOADS` in `frontend/src/config.js`; `APP_DOWNLOAD_URL` default in `backend/app/email_service.py` | Real installer URLs |
| Workspace suffix `temveris.example` | `VITE_WORKSPACE_DOMAIN_SUFFIX` default (`frontend/src/config.js`) | Real workspace domain |
| `support@`, `info@temveris.example` | Sender/reply defaults (`SMTP_USER`, `FROM_EMAIL`, `ADMIN_EMAIL` in `backend/app/email_service.py`), mail footers, frontend support links (VerifyOtp, WorkspaceActivation, OrganizationSetup, Unsubscribe, install guides, Kairos), desktop `author`/`maintainer`, Kairos contacts | Real mailboxes |
| Sender identity domain `temveris.example` | `EMAIL_SENDER_DOMAIN` default (`backend/app/notifications/classification.py`) → approvals@, billing@, security@ … ; email `Message-ID` domain | Real sending domain (with SPF/DKIM) |
| `admin@temveris.example`, `admin3@temveris.example` | Admin dashboard initial state; `backend/create_admin.py` `TENANT_EMAIL`; `SUPER_EMAIL` is `info@temveris.example` | Real admin addresses |
| `@temveris` | Twitter/X handle in `frontend/index.html` | Real handle |
| Company / legal name `Temveris` | Desktop `author`, `copyright`, `maintainer`; `<meta name="author">`; Kairos `_meta.owner`; "Temveris Leave Policy" (`LeavePage.jsx`, `backend/app/leave/router.py`) | Legal entity name |
| Placeholder logo set | `frontend/src/assets/Temveris_*`, `frontend/public/favicon.{svg,jpg}` (also og:image), `backend/app/email_assets/temveris_logo.png`, `desktop-tracker/assets/icon.{png,ico,icns}` + `icons/NxN.png`, `browser-extension/icons/*` | Drop real masters into `./brand/` and run `backend/venv/Scripts/python brand/build_assets.py` (see `brand/README.md`) |

## 2. Not changed on purpose (needs a decision or a later phase)

- **Brand colours** are still the old palette (`frontend/tailwind.config.js`, `BRAND` in `backend/app/email_service.py`). Supply `./brand/colors.txt` to change them.
- **`frontend/src/assets/login-hero.jpg`** still shows the previous product's wordmark inside the picture. It needs new artwork.
- **Internal keys kept for a later phase:** `zt_*` localStorage keys, `zt_refresh` cookie, `ZT_*`/`ZOIKO_*` env names (`ZOIKO_URL`, `ZOIKO_AI_MODEL`), `zoiko.*`/`zoikotime.*` logger names, CSS classes/keyframes (`zoiko-widget`, `zoiko-scrollbar`, `zoiko-dots`, `zoiko-up`, `zoiko-drift`), DOM event `zoiko:billing-blocked`, `window.zoikoDesktop`, enum `idp_active_zoiko_revoked`, Kairos intent id `what_is_zoikotime`.
- **Deploy workflows** (`.github/workflows/*.yml`) are templates: each starts with a `# TEMPLATE` comment, and the old GCP project ids, Cloud Run services, image paths, run.app URLs, Supabase project ref and domain are `YOUR_*` placeholders. Fill them in and set the repo secrets before enabling Actions. `desktop-tracker/RELEASE.md` uses the same placeholders.
- **Kairos facts not rewritten** (pricing, 30-day trial, office addresses, response-time targets): review `backend/app/kairos/data/knowledge.json`. The billing code uses a 14-day trial.
- **Docs:** `docs/` and `runbooks/` now use the Temveris name in prose, but their content (audits, plans, playbooks) still describes the inherited codebase; review before sharing. The phase reports and `memory/` notes were deleted.
- **Alembic migrations** are scrubbed of real personal data. Migration `b6a3d9e52f14` marks organization id 1 as `billing_type='internal'` (free); on a fresh, empty database this matches nothing, but on a copy of the old database org 1 would stay unbilled.
- **A brand-new database cannot be built by `alembic upgrade heads` alone.** `env.py` runs all 94 migrations in one transaction, and some migrations alter tables that only the app creates at startup (`f2a1b3c4d5e6` needs `security_trusted_domains`; `a1b2c3d4e5f6` needs five `audit_*` tables), so the whole upgrade rolls back on an empty Postgres. Two model columns also have no migration at all: `live_statuses.app_version` and `billing_subscriptions.stripe_customer_id`. Before the first real deployment, add a baseline migration that creates these tables and columns, so a fresh database builds in one pass.
