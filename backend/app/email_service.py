"""
Temveris – Email Service
=========================
Sending strategy (tried in order):
  1. Resend API  (HTTP/443 — works on Google Cloud Run, never blocked)
     → Set RESEND_API_KEY in .env / Cloud Run env vars
  2. SMTP fallback  (port 587 STARTTLS or 465 SSL)
     → Works on local dev; may be blocked by some cloud providers

Sign up free at https://resend.com  →  3,000 emails/month, 100/day
"""

import smtplib
import sys
import os
import re
import logging
import time
import uuid
import traceback
from typing import Optional
import requests as _http   # already in requirements.txt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env; override=False preserves any vars already set in the process environment
load_dotenv(override=False)

# ──────────────────────────────────────────────
# CONFIG  (single source of truth — read once)
# ──────────────────────────────────────────────
SMTP_HOST    = os.getenv("SMTP_HOST",    "")          # no provider default — set it with SMTP_PASS
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER",    "support@temveris.example")
SMTP_PASS    = os.getenv("SMTP_PASS",    "")          # ← NEVER hardcode passwords
FROM_EMAIL   = os.getenv("FROM_EMAIL",   "support@temveris.example")
# PLATFORM inboxes — Temveris's own, for Temveris's own mail (support form
# replies, List-Unsubscribe, platform/billing notices).
#
# These are NOT workforce-alert recipients. Every tenant alert (absent, late,
# idle, break overrun, low hours, leave request) resolves its audience through
# app.notifications.recipients.alert_recipients(db, org_id), which reads that
# organization's own configuration and its own admin/HR users. Sending an alert
# about Organization B's employee to the address below would disclose that
# tenant's employee records to Temveris staff — see the module docstring in
# app/notifications/recipients.py.
# PLATFORM address only: the Reply-To on platform mail (support form, billing).
# Never a fallback recipient for tenant workforce alerts.
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL",  "info@temveris.example")
# HR_EMAIL was removed. It was Temveris's own HR inbox used as the fallback
# recipient for EVERY tenant's attendance and risk alerts.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# ── Dev/local email suppression ──────────────────────────────────────────────
# Set DISABLE_EMAILS=true in .env (or the process environment) to prevent any
# real outbound email while running locally. All sends are logged to the console
# instead and the function returns True so callers behave as if delivery worked.
# Production deployments should NOT set this variable (or set it to "false").
DISABLE_EMAILS: bool = os.getenv("DISABLE_EMAILS", "false").strip().lower() in ("1", "true", "yes")

# ── Logo handling ────────────────────────────────────────────────────────────
# Every email header shows the Temveris logo, hot-linked from EMAIL_LOGO_URL.
# The logo is hot-linked (a remote <img src>), NEVER attached as an inline
# CID/MIME part: attaching the PNG was what made Samsung Mail, stock
# Gmail-on-Android, and some BlackBerry Work installs render it a SECOND time
# as a giant block below the action button (the user-reported "logo looks
# really big at the bottom"). With nothing attached there is no asset for an
# extractor to pick up, and the explicit width/height on the <img> keeps the
# header logo correctly sized.
#
# This defaults to the logo served by the backend's public /email-assets mount
# (see main.py) so the image shows in EVERY environment — including Cloud Run,
# where the .env file is absent and only Cloud Run env vars are present. Set
# EMAIL_LOGO_URL to override the host, or to "" to fall back to the text
# wordmark for clients that block remote images.
EMAIL_LOGO_URL = os.getenv(
    "EMAIL_LOGO_URL",
    "https://app.temveris.example/email-assets/temveris_logo.png",
)

# Desktop app download link. Placeholder host until releases are published
# (see TEMVERIS_TODO.md).
# This URL is used as the href of the "Download Temveris App" button in the
# invitation email, so clicking it in any email client starts a direct download
# of the installer without any intermediate landing page.
APP_DOWNLOAD_URL = os.getenv(
    "APP_DOWNLOAD_URL",
    "https://updates.temveris.example/win/Temveris-Setup.exe",
)

# Brand colours
BRAND   = "#009BD6"
DARK    = "#0F172A"
SUCCESS = "#059669"
DANGER  = "#DC2626"
WARN    = "#D97706"


# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
# Windows consoles default to the cp1252 codepage, which cannot encode the
# emoji (📧) and arrows (→) used in our log/print lines. When stdout is on
# cp1252 (the common case on a Windows dev box, or any redirected/piped
# stream) those characters raise UnicodeEncodeError. Because _send() does a
# bare print() on its very first line, that exception was crashing the send
# BEFORE any transport was even tried — emails silently failed on Windows.
# Force UTF-8 on stdout/stderr where the stream supports it so logging can
# never abort a send. errors="replace" is a belt-and-braces fallback.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("email_logs.txt", encoding="utf-8"),
        # Route logs to stdout, not the default stderr. PowerShell paints
        # everything on stderr red and surfaces a `NativeCommandError`,
        # which made every clean boot look like it crashed.
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# STARTUP DIAGNOSTICS  (logs once on import)
# ──────────────────────────────────────────────
def _log_config_status():
    """Log which email transport is configured so problems are visible immediately."""
    if DISABLE_EMAILS:
        logger.warning(
            "📧 [CONFIG] DISABLE_EMAILS=true — all outbound email is suppressed. "
            "Payloads will be logged to console only. "
            "Remove this flag (or set it to false) in production."
        )
        return  # No point checking transports — nothing will be sent

    if RESEND_API_KEY:
        masked = RESEND_API_KEY[:8] + "..." + RESEND_API_KEY[-4:] if len(RESEND_API_KEY) > 12 else "***"
        logger.info(f"📧 [CONFIG] Resend API key detected ({masked}) — will use HTTP transport first")
    else:
        logger.warning("📧 [CONFIG] RESEND_API_KEY not set — Resend transport disabled")

    if SMTP_PASS:
        logger.info(f"📧 [CONFIG] SMTP configured → {SMTP_HOST}:{SMTP_PORT} (user: {SMTP_USER})")
    else:
        logger.warning("📧 [CONFIG] SMTP_PASS not set — SMTP transport disabled")

    if not RESEND_API_KEY and not SMTP_PASS:
        logger.error(
            "❌ [CONFIG] NO EMAIL TRANSPORT CONFIGURED! "
            "Set RESEND_API_KEY or SMTP_PASS in .env / environment variables. "
            "All emails will fail."
        )

_log_config_status()


# ──────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────
def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email))


def _html_to_plain_text(html: str) -> str:
    """Build a clean plain-text alternative from the HTML body.

    Previously this was a one-liner `re.sub("<[^>]+>", "")` which stripped
    HTML *tags* but left the contents of <head>, <style>, and <script>
    blocks in place. That caused GoDaddy / Gmail email previews to show
    raw CSS ("body { margin:0; padding:0; ... }") instead of the actual
    message body — visible in the inbox thumbnail and the message-list
    snippet preview.

    This builder removes those blocks WHOLE first, then strips remaining
    tags, decodes common entities, and collapses whitespace. The output
    is safe to expose to mail clients as the text/plain alternative.
    """
    import html as _html_lib
    s = html or ""
    # Drop the entire <head>...</head> (title, meta, link, style).
    s = re.sub(r"(?is)<head\b[^>]*>.*?</head>", " ", s)
    # Drop any <style>/<script> blocks anywhere else in the document.
    s = re.sub(r"(?is)<style\b[^>]*>.*?</style>",   " ", s)
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", s)
    # Drop HTML comments (incl. the MSO conditional ones we set in the
    # base layout) so their contents don't leak into the preview either.
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    # Convert common block-level closers to line breaks BEFORE stripping
    # so the resulting text has paragraph separation, not one long line.
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|br)\s*/?>", "\n", s)
    # Strip every remaining tag.
    s = re.sub(r"<[^>]+>", " ", s)
    # Decode HTML entities (&nbsp;, &amp;, &#39;, etc.).
    s = _html_lib.unescape(s)
    # Collapse runs of whitespace, preserve paragraph breaks.
    s = re.sub(r"[ \t\xA0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _btn(label: str, url: str, color: str = None) -> str:
    c = color or BRAND
    return (
        f'<a href="{url}" style="display:inline-block;background:{c};color:#ffffff;'
        f'padding:13px 32px;border-radius:8px;text-decoration:none;font-weight:600;'
        f'font-size:14px;letter-spacing:0.3px;">{label}</a>'
    )


def _info_row(label: str, value: str, shaded: bool = False) -> str:
    bg = "#F8FAFC" if shaded else "#ffffff"
    return (
        f'<tr style="background:{bg};">'
        f'<td style="padding:10px 16px;font-weight:600;color:#374151;font-size:13px;width:140px;">{label}</td>'
        f'<td style="padding:10px 16px;color:#1F2937;font-size:13px;">{value}</td>'
        f'</tr>'
    )


def _base_layout(content_html: str, preheader: str = "") -> str:
    year = datetime.now().year

    # Logo rendering — hot-linked from EMAIL_LOGO_URL. We deliberately do NOT
    # attach the PNG as an inline MIME part: several mobile clients render the
    # attached file a second time as a huge block at the bottom of the email
    # (the user-reported "logo looks really big at the bottom" issue). The
    # text wordmark is the fallback when EMAIL_LOGO_URL is intentionally
    # cleared so older clients that block remote images still see the brand.
    text_logo = (
        f'<span style="font-family:\'Segoe UI\',Helvetica,Arial,sans-serif;'
        f'font-size:26px;font-weight:700;letter-spacing:-0.5px;color:{BRAND};">Temveris</span>'
    )
    if EMAIL_LOGO_URL:
        logo_html = (
            f'<img src="{EMAIL_LOGO_URL}" alt="Temveris" '
            f'width="180" height="28" '
            f'style="display:block;border:0;outline:none;text-decoration:none;'
            f'max-width:100%;height:auto;margin:0 auto;" />'
        )
    else:
        logo_html = text_logo
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <title>Temveris</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    body {{ margin:0; padding:0; background:#F1F5F9; }}
    table {{ border-collapse:collapse; mso-table-lspace:0; mso-table-rspace:0; }}
    img {{ border:0; outline:none; text-decoration:none; display:block; }}
    a {{ color:{BRAND}; }}
    @media only screen and (max-width:620px) {{
      .email-wrapper {{ width:100% !important; }}
      .email-body {{ padding:24px 20px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:#F1F5F9;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
  <!-- Preheader (hidden preview text) -->
  <div style="display:none;font-size:1px;color:#F1F5F9;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>

  <!-- Outer wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F5F9;">
    <tr>
      <td align="center" style="padding:40px 16px;">

        <!-- Email card -->
        <table class="email-wrapper" width="600" cellpadding="0" cellspacing="0" border="0"
               style="max-width:600px;width:600px;background:#ffffff;border-radius:16px;overflow:hidden;">

          <!-- Header -->
          <tr>
            <td style="background-color:#ffffff;padding:24px 40px;text-align:center;border-bottom:1px solid #E2E8F0;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center">
                    {logo_html}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td class="email-body" style="padding:40px 40px 32px;">
              {content_html}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#F8FAFC;padding:24px 40px;border-top:1px solid #E2E8F0;text-align:center;">
              <p style="margin:0;color:#94A3B8;font-size:12px;line-height:1.8;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
                &copy; {year} Temveris &mdash; All rights reserved.<br>
                <a href="{FRONTEND_URL}" style="color:{BRAND};text-decoration:none;">Temveris Platform</a>
                &nbsp;&middot;&nbsp;
                <a href="mailto:support@temveris.example" style="color:{BRAND};text-decoration:none;">support@temveris.example</a><br>
                <span style="color:#CBD5E1;">This is an automated message &mdash; please do not reply directly.</span>
              </p>
            </td>
          </tr>

        </table>
        <!-- /Email card -->

      </td>
    </tr>
  </table>
</body>
</html>"""


# ──────────────────────────────────────────────
# INLINE LOGO  (CID attachment for the Resend transport)
# ──────────────────────────────────────────────
# Hot-linked remote images are hidden by privacy-conscious clients ("external
# images not shown") until the recipient enables them — that's why the logo
# showed as a broken/blocked box. Embedding the compressed PNG inline as a CID
# attachment makes the brand logo render on EVERY Resend email without that
# extra click. The bytes are read from disk once and cached for the process.
_LOGO_CID  = "temverislogo"
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "email_assets", "temveris_logo.png")
_logo_b64_cache: Optional[str] = None

def _logo_b64() -> Optional[str]:
    """Base64 of the inline logo, or None if it can't be read. Cached (incl. failure)."""
    global _logo_b64_cache
    if _logo_b64_cache is None:
        import base64
        try:
            with open(_LOGO_PATH, "rb") as fh:
                _logo_b64_cache = base64.b64encode(fh.read()).decode("ascii")
        except Exception as e:
            logger.warning(f"[RESEND] inline logo unavailable ({_LOGO_PATH}): {e}")
            _logo_b64_cache = ""  # cache the miss so we don't retry it on every send
    return _logo_b64_cache or None


# ──────────────────────────────────────────────
# SENDER 1 — Resend API  (HTTP, works on Cloud Run)
# ──────────────────────────────────────────────
def _send_via_resend(to: str, subject: str, html: str, reply_to: Optional[str] = None,
                     from_email: Optional[str] = None) -> dict:
    """
    Send via Resend.com HTTP API.
    Returns {"ok": True/False, "error": str|None, "id": str|None}

    `reply_to` overrides the default ADMIN_EMAIL — used by surfaces like the
    in-app support form so the support inbox can hit Reply and land in the
    user's mailbox instead of bouncing to info@.

    `from_email` overrides the global FROM_EMAIL — supplied by the activation
    seam only when a caller opts into a canonical sender identity. When None
    (every existing caller) the global FROM_EMAIL is used, so the payload is
    byte-identical to before.
    """
    if not RESEND_API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}

    sender = from_email or FROM_EMAIL

    logger.info(f"[RESEND] Attempting to send to {to}")

    # Embed the brand logo inline (CID) so it renders even when the client
    # blocks remote images. Swap the hot-linked <img src> for a cid: reference
    # and attach the bytes — but ONLY when the body actually references the
    # logo, so we never ship an orphan attachment (orphan attachments are what
    # some Android/Samsung clients re-render as a giant block at the bottom,
    # the bug that made the SMTP path drop inline images).
    attachments = []
    _logo_token = f'"{EMAIL_LOGO_URL}"'
    if EMAIL_LOGO_URL and _logo_token in html:
        _b64 = _logo_b64()
        if _b64:
            html = html.replace(_logo_token, f'"cid:{_LOGO_CID}"', 1)
            attachments.append({
                "filename":     "temveris_logo.png",
                "content":      _b64,
                "content_id":   _LOGO_CID,
                "content_type": "image/png",
            })

    # Plain-text alternative — also used as the inbox-preview snippet by
    # most clients. _html_to_plain_text strips <head>/<style>/<script>
    # blocks first so the preview doesn't show raw CSS rules.
    plain_text = _html_to_plain_text(html)

    # One-click unsubscribe URL (§2 Deliverability / §3 global footer). Point the
    # List-Unsubscribe header at the token-signed backend endpoint so mailbox
    # providers can POST to it (RFC 8058) and the opt-out is actually recorded.
    # Falls back to the previous frontend URL on ANY error, so the header is
    # byte-identical to before if the suppression leaf is ever unavailable.
    try:
        from app.notifications import suppression as _suppression
        unsubscribe_url = _suppression.unsubscribe_url(to)
    except Exception:
        unsubscribe_url = f"{FRONTEND_URL.rstrip('/')}/unsubscribe?email={to}"

    # The brand logo is embedded inline above (cid:temverislogo) via the
    # `attachments` entry, so it shows without the recipient enabling remote
    # images. We attach it only when the body references it (see above), which
    # keeps it a true inline part and avoids the orphan-attachment duplicate
    # render that bit us on the SMTP path.
    payload = {
        "from":     f"Temveris <{sender}>",
        "to":       [to],
        "subject":  subject,
        "html":     html,
        "text":     plain_text,
        "reply_to": reply_to or ADMIN_EMAIL,
        # Headers that improve Gmail/Outlook inbox placement:
        # - List-Unsubscribe + List-Unsubscribe-Post: required for one-click
        #   unsubscribe (Gmail bulk-sender rules, Feb 2024)
        # - Auto-Submitted: marks this as system-generated, not a personal
        #   reply expecting one back
        "headers": {
            "List-Unsubscribe":      f"<mailto:{ADMIN_EMAIL}?subject=unsubscribe>, <{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "X-Entity-Ref-ID":       str(uuid.uuid4()),
        },
    }

    # Inline logo attachment (only present when the body references cid:LOGO).
    if attachments:
        payload["attachments"] = attachments

    try:
        resp = _http.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )

        if resp.status_code in (200, 201):
            data = resp.json()
            email_id = data.get("id", "unknown")
            logger.info(f"[RESEND] Sent to {to} | id={email_id}")
            return {"ok": True, "error": None, "id": email_id}

        # --- Handle specific Resend error codes ---
        error_body = ""
        try:
            error_data = resp.json()
            error_body = error_data.get("message", resp.text[:200])
        except Exception:
            error_body = resp.text[:200]

        if resp.status_code == 401:
            error_msg = f"Resend auth failed (401) — check RESEND_API_KEY. Detail: {error_body}"
        elif resp.status_code == 403:
            error_msg = (
                f"Resend forbidden (403) — domain '{FROM_EMAIL.split('@')[-1]}' "
                f"may not be verified in your Resend dashboard. Detail: {error_body}"
            )
        elif resp.status_code == 422:
            error_msg = f"Resend validation error (422): {error_body}"
        elif resp.status_code == 429:
            error_msg = f"Resend rate limit hit (429) — too many emails. Detail: {error_body}"
        else:
            error_msg = f"Resend HTTP {resp.status_code}: {error_body}"

        logger.error(f"❌ [RESEND] {error_msg}")
        return {"ok": False, "error": error_msg}

    except _http.exceptions.ConnectionError as e:
        error_msg = (
            f"Cannot reach api.resend.com — connection refused. "
            f"If deployed on Cloud Run / restricted network, ensure api.resend.com "
            f"is in the allowed egress domains. Detail: {e}"
        )
        logger.error(f"❌ [RESEND] {error_msg}")
        return {"ok": False, "error": error_msg}

    except _http.exceptions.Timeout:
        error_msg = "Resend API request timed out after 15s"
        logger.error(f"❌ [RESEND] {error_msg}")
        return {"ok": False, "error": error_msg}

    except Exception as e:
        error_msg = f"Unexpected Resend error: {type(e).__name__}: {e}"
        logger.error(f"❌ [RESEND] {error_msg}")
        logger.debug(traceback.format_exc())
        return {"ok": False, "error": error_msg}


# ──────────────────────────────────────────────
# SENDER 2 — SMTP fallback  (local dev / non-GCP)
# ──────────────────────────────────────────────
def _send_via_smtp(to: str, subject: str, html: str, retry: int = 3, reply_to: Optional[str] = None,
                   from_email: Optional[str] = None) -> dict:
    """
    Send via SMTP (GoDaddy / any provider).
    Returns {"ok": True/False, "error": str|None}

    `reply_to` overrides the default ADMIN_EMAIL header — used by the in-app
    support form so a Reply from the support inbox goes to the user.

    `from_email` overrides the global FROM_EMAIL — supplied by the activation
    seam only when a caller opts into a canonical sender identity. When None
    (every existing caller) FROM_EMAIL is used, so the message is unchanged.
    """
    if not SMTP_PASS or not SMTP_HOST:
        return {"ok": False, "error": "SMTP_HOST/SMTP_PASS not configured — set them in .env or environment"}

    sender = from_email or FROM_EMAIL

    # MIME shape:
    #   multipart/alternative          ← plain-text + HTML body
    #     ├── text/plain
    #     └── text/html
    #
    # The header logo is hot-linked from EMAIL_LOGO_URL inside the HTML — it is
    # NOT attached. We previously used multipart/related with an inline
    # image/png (cid:temveris-logo), but several mobile clients re-rendered
    # the attached PNG as a giant block at the bottom of the message. With no
    # inline assets, a plain multipart/alternative is the correct, simplest
    # shape and leaves the email with zero attachments.
    msg = MIMEMultipart("alternative")
    msg["Subject"]    = subject
    msg["From"]       = f"Temveris <{sender}>"
    msg["To"]         = to
    msg["Reply-To"]   = reply_to or ADMIN_EMAIL
    msg["Date"]       = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = f"<{uuid.uuid4()}@temveris.example>"
    msg["X-Mailer"]   = "Temveris Mailer 2.0"

    # Plain-text fallback for old email clients AND the inbox preview
    # snippet that GoDaddy / Gmail / Outlook show in the message list.
    # Building this from the real HTML (head / style stripped) means the
    # snippet shows the actual greeting + OTP / link instead of raw CSS.
    plain_text = _html_to_plain_text(html)
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    last_error = ""
    for attempt in range(1, retry + 1):
        try:
            logger.info(
                f"[SMTP] attempt {attempt}/{retry} → {to} "
                f"via {SMTP_HOST}:{SMTP_PORT} (user: {SMTP_USER})"
            )

            if SMTP_PORT == 465:
                # ── Implicit SSL (port 465) ──
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                    server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(sender, to, msg.as_string())
            else:
                # ── STARTTLS (port 587 or others) ──
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(sender, to, msg.as_string())

            logger.info(f"[SMTP] Sent to {to}")
            return {"ok": True, "error": None}

        except smtplib.SMTPAuthenticationError as e:
            error_msg = (
                f"SMTP authentication failed for {SMTP_USER}@{SMTP_HOST}:{SMTP_PORT}. "
                f"Check SMTP_USER and SMTP_PASS in your .env file. "
                f"If you recently changed the mailbox password, update SMTP_PASS. "
                f"SMTP error: {e.smtp_code} {e.smtp_error}"
            )
            logger.error(f"❌ [SMTP] {error_msg}")
            # Auth errors won't resolve on retry — fail immediately
            return {"ok": False, "error": error_msg}

        except smtplib.SMTPConnectError as e:
            last_error = (
                f"Cannot connect to SMTP server {SMTP_HOST}:{SMTP_PORT}. "
                f"Port may be blocked by your hosting provider (common on Cloud Run / GCP). "
                f"Consider using Resend API instead. Detail: {e}"
            )
            logger.warning(f"[SMTP] ConnectError attempt {attempt}: {last_error}")

        except smtplib.SMTPServerDisconnected as e:
            last_error = (
                f"SMTP server {SMTP_HOST}:{SMTP_PORT} disconnected unexpectedly. "
                f"This often means the port is blocked by a firewall or cloud provider. "
                f"Detail: {e}"
            )
            logger.warning(f"[SMTP] Disconnected attempt {attempt}: {last_error}")

        except smtplib.SMTPRecipientsRefused as e:
            last_error = f"Recipient {to} was refused by the SMTP server: {e}"
            logger.error(f"❌ [SMTP] {last_error}")
            # Recipient errors won't resolve on retry
            return {"ok": False, "error": last_error}

        except smtplib.SMTPException as e:
            last_error = f"SMTP error: {type(e).__name__}: {e}"
            logger.warning(f"[SMTP] SMTPException attempt {attempt}: {last_error}")

        except OSError as e:
            # Catches socket.timeout, ConnectionRefusedError, etc.
            last_error = (
                f"Network error connecting to {SMTP_HOST}:{SMTP_PORT}: "
                f"{type(e).__name__}: {e}. "
                f"If on Cloud Run or restricted hosting, SMTP ports are likely blocked."
            )
            logger.warning(f"[SMTP] OSError attempt {attempt}: {last_error}")

        except Exception as e:
            last_error = f"Unexpected error: {type(e).__name__}: {e}"
            logger.error(f"[SMTP] Unexpected error attempt {attempt}: {last_error}")
            logger.debug(traceback.format_exc())

        # Brief, flat backoff before next retry. This runs SYNCHRONOUSLY in the
        # request path of /auth/forgot-password (the send is deliberately not
        # backgrounded — see forgot_password() for the Cloud Run reason), so the
        # old exponential ladder (2s + 4s) added up to 6s of pure sleep on top of
        # the connect timeouts, pushing the worst case toward ~45–60s and pinning
        # a threadpool worker. A connection blip rarely heals between attempts, so
        # a flat 1s is plenty; total SMTP tail is now ~3×10s + 2×1s ≈ 32s worst case.
        if attempt < retry:
            wait = 1
            logger.info(f"[SMTP] Retrying in {wait}s...")
            time.sleep(wait)

    logger.error(f"❌ [SMTP] All {retry} attempts failed for {to}. Last error: {last_error}")
    return {"ok": False, "error": f"All {retry} SMTP attempts failed. {last_error}"}


# ──────────────────────────────────────────────
# PRIMARY ENTRY POINT
# Try Resend first (works on Cloud Run), SMTP as fallback
# ──────────────────────────────────────────────
# Classifications that are NEVER suppressed by preferences / quiet hours /
# unsubscribe (spec §2 "Essential and Critical messages ignore marketing
# preferences"). The default classification is "essential", so existing callers
# skip the governance seam entirely — no import, no DB, no behavior change.
_NON_SUPPRESSIBLE_CLASSES = {"critical", "essential", "commercial_account"}


def _send_all(recipients, subject: str, html: str, **kw) -> bool:
    """Send one message to every address in ``recipients``. True if any landed.

    This is the ONLY way a workforce alert reaches an inbox. ``recipients``
    comes from ``app.notifications.recipients.alert_recipients(db, org_id)``,
    which resolves it from the alert's own organization. An empty list means
    that tenant has no configured audience, and the correct action is to send
    nothing — NOT to fall back to a platform address, which would deliver one
    organization's employee records to another party.
    """
    addrs = [a for a in (recipients or []) if a]
    if not addrs:
        logger.warning(
            "[email] %r not sent: no recipient resolved for this organization. "
            "Configure notifications.alert_recipients or an HR/support email.",
            subject,
        )
        return False
    sent = False
    for addr in addrs:
        if _send(addr, subject, html, **kw):
            sent = True
    return sent


def _send(to: str, subject: str, html: str, retry: int = 3, reply_to: Optional[str] = None,
          from_email: Optional[str] = None, classification: str = "essential",
          family: Optional[str] = None, db=None, recipient_user_id: Optional[int] = None,
          organization_id: Optional[str] = None) -> bool:
    """
    Send an email. Returns True on success, False on failure.
    Tries Resend API first, then SMTP as fallback.
    Logs detailed diagnostics on failure.

    When DISABLE_EMAILS=true the email is NOT sent — the payload is logged to
    the console/log file instead and True is returned so callers are unaffected.

    `reply_to` lets a caller route replies somewhere other than the default
    ADMIN_EMAIL (used by the in-app support form so support@ can hit Reply
    and land in the worker's mailbox).

    Governance seam (all optional; OFF by default — see below). A caller MAY
    opt a send into the notification-governance layer by passing:
      * classification — one of the spec §2 classes. Default "essential" is
        non-suppressible, so existing callers are completely unaffected.
      * family          — a classification.FAMILIES key; only when supplied does
        canonical sender-identity resolution run.
      * db / recipient_user_id / organization_id — recipient context used to
        read preferences + quiet hours + the unsubscribe list.
    With none of these (every current caller) the seam is a no-op: no import,
    no DB read, and the From address stays the global FROM_EMAIL. Any error in
    the seam FAILS OPEN (the email is still sent).
    """
    if DISABLE_EMAILS:
        plain = _html_to_plain_text(html)[:400].replace("\n", " ").strip()
        logger.info(
            f"\n{'='*64}\n"
            f"[DEV EMAIL — NOT SENT]  DISABLE_EMAILS=true\n"
            f"  To:      {to}\n"
            f"  Subject: {subject}\n"
            f"  Body:    {plain}...\n"
            f"{'='*64}"
        )
        print(
            f"\n{'='*64}\n"
            f"[DEV EMAIL — NOT SENT]  DISABLE_EMAILS=true\n"
            f"  To:      {to}\n"
            f"  Subject: {subject}\n"
            f"  Body:    {plain[:200]}...\n"
            f"{'='*64}\n",
            flush=True,
        )
        return True

    print(f"📧 EMAIL → {to} | subject: {subject[:60]}")
    logger.info(f"📧 Email triggered → {to}")

    if not _is_valid_email(to):
        logger.error(f"❌ Invalid email address: {to}")
        return False

    # ── Governance seam (opt-in; default is a no-op) ─────────────────────────
    # Existing callers pass classification="essential" (default) + no context,
    # so `_cls in _NON_SUPPRESSIBLE_CLASSES` is True and this whole block is
    # skipped — no import, no DB, byte-identical behavior. Only a caller that
    # opts into a suppressible class engages preference/quiet-hours/unsubscribe
    # enforcement; only a caller that names a `family` gets sender-identity
    # resolution. Everything here FAILS OPEN.
    resolved_from = from_email
    _cls = (classification or "essential").lower()
    if _cls not in _NON_SUPPRESSIBLE_CLASSES:
        try:
            from app.notifications.router import evaluate_send as _evaluate_send
            from app.notifications import suppression as _sup
            allowed, reason = _evaluate_send(
                to_email=to, classification=_cls, db=db,
                recipient_user_id=recipient_user_id, family=family,
            )
            if allowed and _sup.is_suppressed(db, to, _cls):
                allowed, reason = False, "unsubscribed"
            if not allowed:
                logger.info(f"[EMAIL] suppressed → {to} class={_cls} reason={reason}")
                return True  # intentionally not sent; NOT a delivery failure
        except Exception:
            logger.warning("[EMAIL] governance gate error — failing open (sending)", exc_info=True)

    # Canonical sender identity — resolved ONLY when a caller explicitly names a
    # family, so existing senders (family=None) never change their From, even if
    # ENABLE_SENDER_IDENTITIES is globally on. Returns None (→ FROM_EMAIL) when
    # the flag is off. Fails open to FROM_EMAIL.
    if resolved_from is None and family is not None:
        try:
            from app.notifications import classification as _cls_mod
            resolved_from = _cls_mod.sender_for(family=family)
        except Exception:
            resolved_from = None

    errors = []

    # ── 1. Try Resend (HTTP-based — works everywhere) ──
    if RESEND_API_KEY:
        result = _send_via_resend(to, subject, html, reply_to=reply_to, from_email=resolved_from)
        if result["ok"]:
            return True
        errors.append(f"Resend: {result['error']}")
        logger.warning(f"[EMAIL] Resend failed — trying SMTP fallback. Error: {result['error']}")
    else:
        errors.append("Resend: RESEND_API_KEY not set")

    # ── 2. Fall back to SMTP ──
    if SMTP_PASS:
        result = _send_via_smtp(to, subject, html, retry=retry, reply_to=reply_to, from_email=resolved_from)
        if result["ok"]:
            return True
        errors.append(f"SMTP: {result['error']}")
    else:
        errors.append("SMTP: SMTP_PASS not set")

    # ── Both failed ──
    logger.error(
        f"❌ [EMAIL] ALL TRANSPORTS FAILED for {to}.\n"
        f"   Errors:\n"
        + "\n".join(f"   • {e}" for e in errors)
        + "\n   💡 Fix: Set RESEND_API_KEY (recommended) or SMTP_PASS in .env"
    )
    return False


# ──────────────────────────────────────────────
# HEALTH CHECK ENDPOINT HELPER
# Call this from a /health or /debug-email route
# ──────────────────────────────────────────────
def email_health_check() -> dict:
    """
    Returns a dict describing email transport status.
    Useful for a /health or /admin/debug endpoint.
    """
    status = {
        "resend_configured": bool(RESEND_API_KEY),
        "smtp_configured": bool(SMTP_PASS),
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
        "smtp_user": SMTP_USER,
        "from_email": FROM_EMAIL,
        "any_transport_available": bool(RESEND_API_KEY or SMTP_PASS),
    }

    if RESEND_API_KEY:
        # Quick connectivity check to Resend
        try:
            resp = _http.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=5,
            )
            status["resend_reachable"] = True
            status["resend_auth_valid"] = resp.status_code != 401
            if resp.status_code == 200:
                domains = resp.json().get("data", [])
                status["resend_verified_domains"] = [
                    d.get("name") for d in domains if d.get("status") == "verified"
                ]
        except Exception as e:
            status["resend_reachable"] = False
            status["resend_error"] = str(e)

    if SMTP_PASS:
        # Quick SMTP connectivity check
        try:
            import socket
            sock = socket.create_connection((SMTP_HOST, SMTP_PORT), timeout=5)
            sock.close()
            status["smtp_reachable"] = True
        except Exception as e:
            status["smtp_reachable"] = False
            status["smtp_error"] = str(e)

    return status


def send_test_email(to: str) -> dict:
    """
    Send a test email and return detailed results.
    Useful for debugging from an admin panel or CLI.
    """
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td>
          <h2 style="margin:0 0 12px;color:{DARK};font-size:22px;">Test Email ✅</h2>
          <p style="color:#64748B;font-size:14px;line-height:1.7;margin:0 0 16px;">
            This is a test email from <strong>Temveris</strong>.<br>
            If you received this, your email configuration is working correctly!
          </p>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:16px;">
            {_info_row("Sent To",     to)}
            {_info_row("From",        FROM_EMAIL, True)}
            {_info_row("SMTP Host",   f"{SMTP_HOST}:{SMTP_PORT}")}
            {_info_row("Resend Key",  "Configured" if RESEND_API_KEY else "Not Set", True)}
            {_info_row("Timestamp",   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))}
          </table>
        </td>
      </tr>
    </table>
    """
    html = _base_layout(body, "Temveris test email — checking your configuration")
    success = _send(to, "Test Email — Configuration Check", html)

    return {
        "success": success,
        "to": to,
        "transport_status": email_health_check(),
    }


# ══════════════════════════════════════════════
# 1. INVITE EMAIL  (Two-step onboarding)
# ══════════════════════════════════════════════
def send_invite_email(
    to_email: str,
    employee_name: str,
    invite_link: str,
    inviter_name: str = "Admin",
) -> bool:
    """
    Invitation email sent when admin adds a new employee.
    Layout: Activate Your Account (set password CTA) + expiry notice.
    """
    first_name = employee_name.strip().split()[0] if employee_name.strip() else "there"

    body = f"""
    <!-- ── Greeting ── -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Welcome to Temveris! &#127881;
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            <strong>{inviter_name}</strong> has added you to <strong>Temveris</strong>
            &mdash; the workforce assurance platform. Complete the steps below to
            activate your account and start tracking your work.
          </p>
        </td>
      </tr>
    </table>

    <!-- ── Login email badge ── -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px;">
      <tr>
        <td style="background-color:#F0F9FF;border:1px solid #BAE6FD;border-radius:10px;
                   padding:14px 20px;">
          <p style="margin:0 0 4px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:11px;font-weight:700;color:#0369A1;
                    text-transform:uppercase;letter-spacing:1px;">
            Your Login Email
          </p>
          <p style="margin:0;font-family:Courier,monospace;font-size:15px;
                    font-weight:700;color:{BRAND};">
            {to_email}
          </p>
        </td>
      </tr>
    </table>

    <!-- STEP 1 — Activate Your Account -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:20px;">
      <tr>
        <td style="background-color:#EFF6FF;border:1px solid #BFDBFE;border-radius:12px;
                   padding:24px 28px;">
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:11px;font-weight:700;color:#1D4ED8;
                    text-transform:uppercase;letter-spacing:1.2px;">
            Step 1 &mdash; Activate Your Account
          </p>
          <p style="margin:0 0 18px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#374151;line-height:1.7;">
            Click the button below to set your password. This link is
            <strong>valid for 24 hours</strong> and can only be used once.
          </p>
          <table cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td align="center" style="border-radius:8px;background-color:{BRAND};">
                <a href="{invite_link}"
                   style="display:inline-block;padding:14px 32px;
                          font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:14px;font-weight:700;color:#ffffff;
                          text-decoration:none;border-radius:8px;
                          background-color:{BRAND};
                          mso-padding-alt:14px 32px;">
                  &#128273; Set My Password &amp; Get Started
                </a>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>

    <!-- ── Expiry notice ── -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td style="background-color:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;
                   padding:14px 18px;">
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#92400E;line-height:1.6;">
            <strong>Important:</strong> The password setup link expires in
            <strong>24 hours</strong>. If it expires, ask your admin to resend the invitation.
          </p>
        </td>
      </tr>
    </table>

    <!-- ── Plain-text fallback link ── -->
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:12px;color:#94A3B8;line-height:1.7;">
            If the activation button does not work, copy and paste this URL into your browser:<br>
            <a href="{invite_link}"
               style="color:{BRAND};word-break:break-all;font-size:11px;">
              {invite_link}
            </a>
          </p>
        </td>
      </tr>
    </table>
    """

    html = _base_layout(
        body,
        preheader=f"{inviter_name} invited you to Temveris — set your password and download the app to get started",
    )
    return _send(
        to_email,
        "You have been invited — activate your account",
        html,
        classification="essential",
        family="iam.invite",
    )


# ══════════════════════════════════════════════
# 1b. ACCOUNT ACTIVATED  (ZT-ONB-02)
# Operational - onboarding. Sent once when an invited user completes activation
# (set-password). Always sends (fail-open) — no preference context is passed.
# From welcome@ when sender identities are enabled. Not an employer/disciplinary
# message. reply_to = the inviting admin when available, else the default.
# ══════════════════════════════════════════════
def send_account_activated_email(
    to_email: str,
    name: str,
    role_name: str = "",
    workspace_name: str = "",
    reply_to: Optional[str] = None,
) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    ws = workspace_name or "your workspace"
    role_line = f" as <strong>{role_name}</strong>" if role_name else ""
    login_url = FRONTEND_URL.rstrip("/") + "/"
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Welcome to {ws} on Temveris
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            Your Temveris account for <strong>{ws}</strong> is now active{role_line}.
            Start by reviewing your profile, time zone, work location, schedule, and any
            outstanding actions.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#64748B;line-height:1.7;">
            Questions about workplace policy, attendance expectations, or approval decisions
            should be directed to your organization.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("Open Temveris", login_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"Your Temveris account for {ws} is active — open Temveris to get started.")
    return _send(
        to_email, f"Welcome to {ws} on Temveris", html,
        reply_to=reply_to, classification="operational", family="onboarding.user_activated",
    )


# ══════════════════════════════════════════════
# 2. RESET PASSWORD EMAIL
# ══════════════════════════════════════════════
def send_reset_password_email(to_email: str, name: str, token: str) -> bool:
    reset_link = f"{FRONTEND_URL.rstrip('/')}/reset-password/{token}"
    first_name = name.strip().split()[0] if name.strip() else "there"
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Reset Your Password
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            We received a request to reset your Temveris password.
            Click the button below to choose a new password.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px;">
      <tr>
        <td align="center">
          <table cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td align="center" style="border-radius:8px;background-color:{WARN};">
                <a href="{reset_link}"
                   style="display:inline-block;padding:14px 32px;
                          font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          font-size:15px;font-weight:700;color:#ffffff;
                          text-decoration:none;border-radius:8px;
                          background-color:{WARN};">
                  Reset My Password
                </a>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td style="background-color:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;
                   padding:14px 18px;">
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#92400E;line-height:1.6;">
            <strong>This link expires in 15 minutes.</strong><br>
            If you did not request a password reset, you can safely ignore this email.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:12px;color:#94A3B8;line-height:1.7;">
            If the button does not work, copy and paste this URL into your browser:<br>
            <a href="{reset_link}" style="color:{BRAND};word-break:break-all;font-size:11px;">{reset_link}</a>
          </p>
        </td>
      </tr>
    </table>
    """
    html = _base_layout(body, "Reset your Temveris password — link expires in 15 minutes")
    return _send(to_email, "Password Reset Request", html)


# ══════════════════════════════════════════════
# 2b. PASSWORD RESET OTP EMAIL
# 4-digit code for the OTP-based Forgot Password flow.
# Keep this template visually distinct from the link-based reset email
# above so users who accidentally trigger both flows can tell them apart.
# ══════════════════════════════════════════════
def send_password_reset_otp_email(to_email: str, name: str, otp_code: str) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    # Render the 4-digit OTP as one cell per digit so it stays readable
    # even when an email client strips letter-spacing (Outlook desktop).
    digit_cells = "".join(
        f'<td align="center" style="padding:0 6px;">'
        f'  <div style="display:inline-block;min-width:54px;padding:14px 6px;'
        f'              background:#F1F5F9;border:1px solid #E2E8F0;border-radius:10px;'
        f'              font-family:\'Segoe UI\',Helvetica,Arial,sans-serif;'
        f'              font-size:30px;font-weight:700;color:{DARK};letter-spacing:0;">'
        f'    {ch}'
        f'  </div>'
        f'</td>'
        for ch in otp_code
    )
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Password Recovery Code
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hello <strong style="color:{DARK};">{first_name}</strong>,<br>
            Use the verification code below to reset your Temveris password.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px 0;">
      <tr><td align="center">
        <table cellpadding="0" cellspacing="0" border="0"><tr>{digit_cells}</tr></table>
      </td></tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td style="background-color:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;
                   padding:14px 18px;">
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#92400E;line-height:1.6;">
            <strong>This code expires in 5 minutes.</strong><br>
            If you did not request this reset, you can safely ignore this email —
            your password will not change.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:12px;color:#94A3B8;line-height:1.7;">
            Regards,<br>
            <strong style="color:{DARK};">Temveris Support Team</strong>
          </p>
        </td>
      </tr>
    </table>
    """
    # Preheader must NOT contain the OTP — email clients render this as the
    # inbox-preview snippet and as the body of phone/lock-screen notifications.
    # Putting the code here leaks it to anyone glancing at the recipient's
    # phone or inbox list. Keep this string generic; the digits live only
    # inside the rendered body where the user has to actively open the mail.
    html = _base_layout(body, "Your Temveris password recovery code is inside — expires in 5 minutes.")
    # ZT-IAM-04 sender governance: security@ sender + reply-to when sender
    # identities are enabled; None reply-to preserves today's default when off.
    try:
        from app.notifications import classification as _cls
        reply_to = _cls.sender_for(sender_key="security")
    except Exception:
        reply_to = None
    return _send(to_email, "Temveris Password Recovery Code", html,
                 reply_to=reply_to, classification="essential", family="iam.password_reset_otp")


# ══════════════════════════════════════════════
# 2c. PASSWORD CHANGED  (ZT-IAM-05)
# Essential - security, non-suppressible. Sent after a successful password
# RESET or logged-in CHANGE (never first-time invite activation). Routed through
# the sender-governance seam (family="iam.password_changed" → security@ sender
# + security@ reply-to when identities are enabled; default sender otherwise).
# approximate_location / device_summary are intentionally omitted — they are not
# captured at the call sites and are not invented.
# ══════════════════════════════════════════════
def send_password_changed_email(to_email: str, name: str, event_time: Optional[str] = None) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    when = event_time or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    security_url = f"{FRONTEND_URL.rstrip('/')}/account-settings"
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Your password was changed
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            The password for your Temveris account was changed on <strong>{when}</strong>.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td style="background-color:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;padding:14px 18px;">
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#92400E;line-height:1.6;">
            If you made this change, no action is required. If you did not, secure your account
            immediately and contact Temveris Support.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("Secure my account", security_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, "Your Temveris password was changed — secure your account if this wasn't you.")
    try:
        from app.notifications import classification as _cls
        reply_to = _cls.sender_for(sender_key="security")
    except Exception:
        reply_to = None
    return _send(
        to_email, "Your Temveris password was changed", html,
        reply_to=reply_to, classification="essential", family="iam.password_changed",
    )


# ══════════════════════════════════════════════
# 3. LEAVE REQUEST → ADMIN
# ══════════════════════════════════════════════
def send_leave_request_to_admin(
    *,
    recipients,
    leave_id: int,
    employee_name: str,
    employee_email: str,
    leave_type: str,
    from_date: str,
    to_date: str,
    description: str,
    reason: str = "",
    balance_remaining: float = None,
    leave_days: int = None,
    blackout_dates: list = None,
    team_coverage: str = None,
    supervisor_status: str = None,
    doc_status: str = None,
    balance_unlimited: bool = False,
) -> bool:
    # Route each button into its matching Temveris section. HR must sign in and
    # see the full balance context before approving or declining.
    approve_url   = f"{FRONTEND_URL}/admin?tab=manageleave&leave={leave_id}&action=approve"
    decline_url   = f"{FRONTEND_URL}/admin?tab=manageleave&leave={leave_id}&action=decline"
    dashboard_url = f"{FRONTEND_URL}/admin?tab=overview"
    note_text  = description or reason or "—"

    # ── HR Summary lines ─────────────────────────────────────────────
    # A type with no annual limit (Unpaid, Company, Compensatory) has no
    # meaningful "remaining" — allowance-minus-used printed e.g. "87.0 days
    # remaining for Unpaid Leave", which reads as a real entitlement.
    if balance_unlimited:
        bal_line = f"{leave_type} has <strong>no annual limit</strong> — no leave balance applies."
    elif balance_remaining is None:
        bal_line = "Balance details unavailable — please review in Temveris."
    else:
        bal_line = f"The employee has <strong>{balance_remaining:.1f} day(s)</strong> of leave remaining for {leave_type}."

    if blackout_dates:
        bl_line = "Blackout dates during the requested period: <strong>" + ", ".join(blackout_dates) + "</strong>."
    else:
        bl_line = "There are <strong>no blackout dates</strong> during the requested leave period."

    cov_line = team_coverage or "Team coverage has not been assessed — please review in Temveris."
    sup_line = supervisor_status or "Supervisor pre-approval: pending manager review."
    doc_line = ""
    if doc_status:
        doc_line = f"<li style=\"margin:6px 0;\">{doc_status}</li>"

    body = f"""
    <h2 style="margin:0 0 8px;color:{DARK};font-size:22px;">New Leave Request &#128203;</h2>
    <p style="color:#64748B;font-size:14px;margin:0 0 20px;">
      A new leave request requires your review.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:20px;">
      {_info_row("Employee",    f"{employee_name} &lt;{employee_email}&gt;")}
      {_info_row("Leave Type",  leave_type.replace("_", " ").title(), True)}
      {_info_row("From Date",   from_date)}
      {_info_row("To Date",     to_date, True)}
      {_info_row("Days",        str(leave_days) if leave_days else "—")}
      {_info_row("Description", note_text, True)}
    </table>

    <div style="background:#F0F9FF;border:1px solid #BAE6FD;border-radius:8px;padding:14px 18px;margin-bottom:20px;">
      <p style="margin:0 0 8px;font-size:14px;font-weight:700;color:#075985;">HR Summary</p>
      <ul style="margin:0;padding-left:18px;color:#0C4A6E;font-size:13px;line-height:1.7;">
        <li style="margin:6px 0;">{bal_line}</li>
        <li style="margin:6px 0;">{bl_line}</li>
        <li style="margin:6px 0;">{cov_line}</li>
        <li style="margin:6px 0;">{sup_line}</li>
        {doc_line}
      </ul>
    </div>

    <p style="color:#64748B;font-size:13px;margin:0 0 14px;">
      Please sign in to <strong>Temveris</strong> to review the full balance, attachments and audit trail
      before approving or declining this request.
    </p>
    <table cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding-right:10px;">{_btn("Approve", approve_url, SUCCESS)}</td>
        <td style="padding-right:10px;">{_btn("Decline", decline_url, DANGER)}</td>
        <td>{_btn("View Dashboard", dashboard_url)}</td>
      </tr>
    </table>
    """
    subject = f"Leave Request – {employee_name} ({leave_type})"
    html = _base_layout(body, f"Leave request from {employee_name} – {leave_type}")
    return _send_all(recipients, subject, html,
                     classification="essential", family="leave.request")


# ══════════════════════════════════════════════
# 4. LEAVE STATUS → EMPLOYEE
# ══════════════════════════════════════════════
def send_leave_status_to_employee(
    employee_email: str,
    employee_name: str,
    leave_type: str,
    from_date: str,
    to_date: str,
    status: str,
    comment: str = "",
) -> bool:
    is_approved = status.lower() == "approved"
    color   = SUCCESS if is_approved else DANGER
    label   = "Approved" if is_approved else "Rejected"
    emoji   = "&#9989;" if is_approved else "&#10060;"
    dash_url = f"{FRONTEND_URL}/dashboard"

    body = f"""
    <div style="text-align:center;margin-bottom:28px;">
      <div style="display:inline-block;
                  background:{'#DCFCE7' if is_approved else '#FEE2E2'};
                  border-radius:50%;width:64px;height:64px;line-height:64px;font-size:28px;">
        {emoji}
      </div>
      <h2 style="margin:12px 0 4px;color:{color};font-size:22px;">
        Leave Request {label}
      </h2>
    </div>
    <p style="color:#64748B;font-size:14px;line-height:1.7;margin:0 0 20px;">
      Hi <strong>{employee_name}</strong>,<br><br>
      Your leave request has been
      <strong style="color:{color};">{label.lower()}</strong>.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Leave Type", leave_type.replace("_", " ").title())}
      {_info_row("From Date",  from_date, True)}
      {_info_row("To Date",    to_date)}
      {_info_row("Status", f'<strong style="color:{color};">{label}</strong>', True)}
      {_info_row("Comment", comment) if comment else ""}
    </table>
    <div style="text-align:center;">
      {_btn("View My Leaves", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Your {leave_type} request has been {label.lower()}")
    return _send(
        employee_email,
        f"Leave {label} – {leave_type} ({from_date} to {to_date})",
        html,
        classification="operational",
        family="leave.decision",
    )


# ══════════════════════════════════════════════
# 5. RISK ALERT → HR ONLY
# ══════════════════════════════════════════════
def send_risk_alert_to_hr(
    *,
    recipients,
    employee_name: str,
    employee_email: str,
    alert_date: str,
    worked_hours: str,
    threshold_hours: str,
) -> bool:
    body = f"""
    <div style="background:#FEF2F2;border:2px solid #FCA5A5;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#991B1B;font-size:20px;">&#9888;&#65039; Risk Alert: Insufficient Work Hours</h2>
      <p style="margin:0;color:#7F1D1D;font-size:13px;">
        An employee has not completed the minimum required working hours for the day.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Employee",        f"{employee_name} &lt;{employee_email}&gt;")}
      {_info_row("Date",            alert_date, True)}
      {_info_row("Hours Worked",    worked_hours)}
      {_info_row("Required Hours",  threshold_hours, True)}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Action Required:</strong> Please review this employee's attendance record and
        take appropriate action as per company policy.
      </p>
    </div>
    """
    html = _base_layout(body, f"Risk Alert – {employee_name} worked only {worked_hours} on {alert_date}")
    subject = f"Risk Alert – {employee_name} ({alert_date})"
    return _send_all(recipients, subject, html,
                     classification="operational", family="risk.alert")


# ══════════════════════════════════════════════
# 6. ABSENT ALERT → the organization's own admin/HR audience
# Fires when an employee is marked Absent — i.e. past THAT ORGANIZATION's
# configured absent cutoff (schedule.absent_after_hour, evaluated in the org's
# configured timezone), no approved leave for today, and no clock-in.
# ══════════════════════════════════════════════
def send_idle_alert_to_hr(
    *,
    recipients,
    employee_name: str,
    employee_email: str,
    idle_minutes: int,
    department: str = "",
    designation: str = "",
    detected_at: str = "",
) -> bool:
    """One email per idle streak. The /employees/team handler dedups via
    AuditLog: a single worker who is idle for 45 min triggers exactly one
    email at the 15-min mark, not three. A subsequent idle streak (after
    the worker is active again) does generate a new alert.
    """
    dept_line = f"{department or 'Unassigned'} – {designation}" if designation else (department or "Unassigned")
    dash_url  = f"{FRONTEND_URL}/admin"
    body = f"""
    <div style="background:#FFFBEB;border:2px solid #FCD34D;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#92400E;font-size:20px;">&#9888;&#65039; Idle Alert</h2>
      <p style="margin:0;color:#78350F;font-size:13px;">
        An employee has shown no keyboard or mouse activity for over {idle_minutes} minutes while clocked in.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Employee",   f"{employee_name} &lt;{employee_email}&gt;")}
      {_info_row("Department", dept_line, True)}
      {_info_row("Idle Duration", f'<strong style="color:#92400E;">{idle_minutes} minutes</strong>')}
      {_info_row("Detected At", detected_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), True)}
      {_info_row("Status",     '<strong style="color:#92400E;">Idle &ndash; No Activity</strong>')}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Action Required:</strong> Please verify whether the employee stepped away
        without taking a break, or is in a long meeting / call that the tracker can&rsquo;t see.
        Review their day timeline and follow up as needed.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open Admin Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Idle Alert – {employee_name} ({idle_minutes} min)")
    subject = f"Idle Alert – {employee_name} ({idle_minutes} min)"
    return _send_all(recipients, subject, html,
                     classification="operational", family="attendance.idle")


# ══════════════════════════════════════════════
# BREAK OVERRUN → HR ESCALATION (Workforce Governance)
# Sent when a worker's break exceeds THAT ORGANIZATION's configured allowance
# (break.short_minutes / break.lunch_minutes plus its grace). The first alert
# fires at the effective limit; the break overrun monitor then sends a reminder
# every break.overrun_reminder_minutes while the break stays active. Delivered
# to the worker's own organization's alert audience.
# ══════════════════════════════════════════════
def send_break_overrun_alert_to_hr(
    *,
    recipients,
    worker_name: str,
    worker_id,
    worker_type: str,
    break_type: str,
    allowed_minutes: int,
    current_minutes,
    exceeded_minutes,
    department: str = "",
    manager: str = "",
    start_time: str = "",
    current_time: str = "",
    tenant: str = "default",
    policy_version: str = "",
    is_reminder: bool = False,
) -> bool:
    break_label = "Lunch Break" if (break_type or "").lower() == "lunch" else "Short Break"
    dash_url = f"{FRONTEND_URL}/admin"
    cur = round(float(current_minutes or 0))
    exc = round(float(exceeded_minutes or 0))
    headline = (
        f"Reminder: {worker_name} is still on an over-limit {break_label.lower()}."
        if is_reminder else
        f"{worker_name} exceeded their {break_label} allowance."
    )
    title = "Break Time Exceeded" + (" (Reminder)" if is_reminder else "")
    body = f"""
    <div style="background:#FEF2F2;border:2px solid #FCA5A5;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#991B1B;font-size:20px;">&#9888;&#65039; {title}</h2>
      <p style="margin:0;color:#7F1D1D;font-size:13px;">
        {headline} Immediate review may be required.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Worker Name",      worker_name or "—")}
      {_info_row("Worker ID",        str(worker_id), True)}
      {_info_row("Worker Type",      (worker_type or "—").replace("_", " ").title())}
      {_info_row("Break Type",       break_label, True)}
      {_info_row("Allowed Duration", f"{allowed_minutes} minutes")}
      {_info_row("Current Duration", f'<strong style="color:#991B1B;">{cur} minutes</strong>', True)}
      {_info_row("Exceeded By",      f'<strong style="color:#991B1B;">{exc} minutes</strong>')}
      {_info_row("Department",       department or "Unassigned", True)}
      {_info_row("Manager",          manager or "—")}
      {_info_row("Start Time",       start_time or "—", True)}
      {_info_row("Current Time",     current_time or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}
      {_info_row("Tenant",           tenant or "default", True)}
      {_info_row("Policy Version",   policy_version or "—")}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Action Required:</strong> The worker has not yet resumed work. Please
        review the situation and follow up per your workforce-governance policy.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open Admin Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Break Time Exceeded – {worker_name} ({break_label})")
    subject = "Break Time Exceeded - Workforce Governance Alert"
    return _send_all(recipients, subject, html,
                     classification="operational", family="attendance.break_overrun")


def send_break_overrun_alert_to_employee(
    *,
    employee_email: str,
    employee_name: str,
    break_type: str,
    allowed_minutes: int,
    current_minutes,
    exceeded_minutes,
    start_time: str = "",
    current_time: str = "",
    is_reminder: bool = False,
) -> bool:
    """Notify the WORKER that their own break has run over its allowance, so they
    can resume work without waiting for a manager to chase them. Mirrors the HR
    escalation (same trigger after allowance + grace, then the 30-min reminder
    cadence) but with a self-service tone and a link to the worker dashboard
    rather than /admin. Returns False (no send) when the worker has no email."""
    if not employee_email:
        return False
    break_label = "Lunch Break" if (break_type or "").lower() == "lunch" else "Short Break"
    dash_url = f"{FRONTEND_URL}/dashboard"
    cur = round(float(current_minutes or 0))
    exc = round(float(exceeded_minutes or 0))
    headline = (
        f"You are still on an over-limit {break_label.lower()}. Please resume work now."
        if is_reminder else
        f"Your {break_label} has gone past its {allowed_minutes}-minute allowance."
    )
    title = "Break Time Exceeded" + (" (Reminder)" if is_reminder else "")
    body = f"""
    <div style="background:#FEF2F2;border:2px solid #FCA5A5;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#991B1B;font-size:20px;">&#9888;&#65039; {title}</h2>
      <p style="margin:0;color:#7F1D1D;font-size:13px;">
        Hi {employee_name or 'there'}, {headline}
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Break Type",       break_label)}
      {_info_row("Allowed Duration", f"{allowed_minutes} minutes", True)}
      {_info_row("Current Duration", f'<strong style="color:#991B1B;">{cur} minutes</strong>')}
      {_info_row("Exceeded By",      f'<strong style="color:#991B1B;">{exc} minutes</strong>', True)}
      {_info_row("Start Time",       start_time or "—")}
      {_info_row("Current Time",     current_time or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), True)}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Please resume work.</strong> Your break is being tracked and a
        workforce-governance alert has also been sent to HR. Tap below to clock
        back in from your dashboard.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Resume Work", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Your {break_label} has exceeded its allowance")
    subject = "Break Time Exceeded" + (" (Reminder)" if is_reminder else "")
    return _send(employee_email, subject, html, classification="operational", family="attendance.break_overrun")


def send_absent_alert_to_admin_hr(
    *,
    recipients,
    employee_name: str,
    employee_email: str,
    absent_date: str,
    department: str = "",
    designation: str = "",
    detected_at: str = "",
) -> bool:
    dept_line = f"{department or 'Unassigned'} – {designation}" if designation else (department or "Unassigned")
    dash_url  = f"{FRONTEND_URL}/admin"
    body = f"""
    <div style="background:#FEF2F2;border:2px solid #FCA5A5;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#991B1B;font-size:20px;">&#9888;&#65039; Absent Alert</h2>
      <p style="margin:0;color:#7F1D1D;font-size:13px;">
        An employee has not clocked in and has no approved leave for today — marked as Absent (Unauthorized).
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Employee",   f"{employee_name} &lt;{employee_email}&gt;")}
      {_info_row("Department", dept_line, True)}
      {_info_row("Date",       absent_date)}
      {_info_row("Detected At", detected_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), True)}
      {_info_row("Status",     '<strong style="color:#991B1B;">Absent (Unauthorized)</strong>')}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Action Required:</strong> Please verify with the employee whether this is an
        unplanned absence, and record a leave request or take policy action as appropriate.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open Admin Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Absent Alert – {employee_name} on {absent_date}")
    subject = f"Absent Alert – {employee_name} ({absent_date})"
    return _send_all(recipients, subject, html,
                     classification="operational", family="attendance.absent")


# ══════════════════════════════════════════════
# 6b. LATE-ARRIVAL ALERT → ADMIN + HR
# Fires when an employee with a configured shift has not clocked in and is
# now past their shift start + grace window, but it's still before the noon
# "Absent" threshold. This is the front half of the Overview dashboard's
# "Requires Attention" bucket (the Absent half is handled above). Amber
# styling — distinct from the red Absent alert — because the worker is late,
# not yet declared absent for the day, and may still arrive.
# ══════════════════════════════════════════════
def send_late_arrival_alert_to_admin_hr(
    *,
    recipients,
    employee_name: str,
    employee_email: str,
    late_date: str,
    late_minutes: int,
    shift_start: str = "",
    department: str = "",
    designation: str = "",
    detected_at: str = "",
) -> bool:
    dept_line = f"{department or 'Unassigned'} – {designation}" if designation else (department or "Unassigned")
    dash_url  = f"{FRONTEND_URL}/admin"
    shift_label = shift_start or "their scheduled start"
    body = f"""
    <div style="background:#FFFBEB;border:2px solid #FCD34D;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#92400E;font-size:20px;">&#9888;&#65039; Late Arrival – Requires Attention</h2>
      <p style="margin:0;color:#78350F;font-size:13px;">
        An employee has not clocked in and is now past their shift start plus grace period.
        They have not yet been marked Absent — please follow up.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Employee",   f"{employee_name} &lt;{employee_email}&gt;")}
      {_info_row("Department", dept_line, True)}
      {_info_row("Shift Start", shift_label)}
      {_info_row("Late By",    f'<strong style="color:#92400E;">{late_minutes} minutes</strong>', True)}
      {_info_row("Date",       late_date)}
      {_info_row("Detected At", detected_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), True)}
      {_info_row("Status",     '<strong style="color:#92400E;">Late &ndash; No Clock-In</strong>')}
    </table>
    <div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#92400E;font-size:13px;line-height:1.6;">
        <strong>Action Required:</strong> Please check in with the employee to confirm whether
        they are running late, unwell, or absent. Record a leave request or follow up per policy.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open Admin Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, f"Late Arrival – {employee_name} ({late_minutes} min late)")
    subject = f"Late Arrival – {employee_name} ({late_date})"
    return _send_all(recipients, subject, html,
                     classification="operational", family="attendance.late")


def send_early_clockout_alert(
    *,
    recipients=None,
    employee_name: str,
    employee_email: str = "",
    employee_id: str = "",
    employment_type: str = "",
    department: str = "",
    organization_name: str = "",
    worked_seconds: int = 0,
    required_seconds: int = 0,
    remaining_seconds: int = 0,
    clock_in: str = "",
    clock_out: str = "",
    reason: str = "",
    tz_name: str = "",
) -> bool:
    """Notify the org's HR/notification inbox that an employee clocked out
    before completing their required worked hours (Controlled Clock-Out).

    Presentation only: durations arrive as raw seconds and timestamps as ISO
    (UTC) strings; this builder renders them human-friendly (e.g. "3 minutes",
    "1h 25m", "06 Jul 2026 · 10:17 AM IST"). ``recipients`` are resolved by the
    caller from THIS organization (app/notifications/recipients.py); there is no
    platform fallback address, because sending one tenant's workforce data to
    Temveris's own inbox is a data leak, not a safety net. The free-text
    ``reason`` is HTML-escaped — it is user input.
    """
    import html as _html
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # Render times on the recipient organization's wall clock. `tz_name` is
    # passed by the caller (which knows the org); without it we fall back to the
    # documented system default rather than inventing one here.
    from app import _tz as _tzmod
    _IST = _tzmod._load_zone(tz_name) or _tzmod._load_zone(_tzmod.FALLBACK_TZ_NAME) \
        or _tzmod._IST_FIXED
    _tz_label = (tz_name or _tzmod.FALLBACK_TZ_NAME).split("/")[-1].replace("_", " ")

    def _human_duration(secs) -> str:
        secs = int(secs or 0)
        if secs <= 0:
            return "0 minutes"
        minutes = round(secs / 60)
        if minutes < 60:
            return f"{minutes} minute" + ("" if minutes == 1 else "s")
        hours, rem = divmod(minutes, 60)
        return f"{hours}h" if rem == 0 else f"{hours}h {rem:02d}m"

    def _to_ist(iso_str):
        if not iso_str:
            return None
        try:
            d = _dt.fromisoformat(str(iso_str).strip().replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tz.utc)
            return d.astimezone(_IST)
        except Exception:
            return None

    def _fmt_ist(iso_str) -> str:
        d = _to_ist(iso_str)
        if d is None:
            return "&mdash;"
        return (f"{d.strftime('%d %b %Y')} &middot; "
                f"{d.strftime('%I:%M %p').lstrip('0')} {_tz_label}")

    def _rows(pairs) -> str:
        out = ""
        for i, (label, value) in enumerate(pairs):
            out += _info_row(label, value, i % 2 == 1)
        return out

    emp_type_label = {
        "full_time": "Full-Time",
        "part_time": "Part-Time",
    }.get((employment_type or "").strip().lower(),
          (employment_type or "").replace("_", " ").title() or "&mdash;")

    out_date = _to_ist(clock_out)
    date_label = out_date.strftime("%d %b %Y") if out_date else "&mdash;"

    safe_reason = _html.escape(reason or "").replace("\n", "<br>") or "&mdash;"
    dash_url = f"{FRONTEND_URL}/admin"

    emp_pairs = [
        ("Employee Name", _html.escape(employee_name or "&mdash;")),
        ("Employee ID", _html.escape(str(employee_id)) if employee_id else "&mdash;"),
        ("Organization", _html.escape(organization_name) if organization_name else "&mdash;"),
        ("Employment Type", emp_type_label),
    ]
    if department and str(department).strip():
        emp_pairs.append(("Department", _html.escape(str(department))))
    emp_pairs.append(("Date", date_label))

    work_pairs = [
        ("Worked Time", _human_duration(worked_seconds)),
        ("Required Time", _human_duration(required_seconds)),
        ("Remaining Work Time",
         f'<strong style="color:#DC2626;">{_human_duration(remaining_seconds)}</strong>'),
        ("Clock In", _fmt_ist(clock_in)),
        ("Clock Out", _fmt_ist(clock_out)),
    ]

    _section_label = (
        'margin:0 0 8px;font-size:11px;font-weight:700;letter-spacing:0.6px;'
        'text-transform:uppercase;color:#94A3B8;'
        "font-family:'Segoe UI',Helvetica,Arial,sans-serif;"
    )
    _card_table = (
        'border:1px solid #E2E8F0;border-radius:12px;overflow:hidden;margin-bottom:26px;'
    )

    body = f"""
    <h1 style="margin:0 0 6px;font-size:22px;font-weight:700;color:{DARK};
               font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
      Early Clock Out Notification
    </h1>
    <p style="margin:0 0 28px;font-size:14px;line-height:1.6;color:#64748B;
              font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
      An employee has clocked out before completing the required working hours.
    </p>

    <p style="{_section_label}">Employee</p>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="{_card_table}">
      {_rows(emp_pairs)}
    </table>

    <p style="{_section_label}">Work Details</p>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="{_card_table}">
      {_rows(work_pairs)}
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px;">
      <tr>
        <td style="background:#FEF2F2;border:1px solid #FECACA;border-radius:12px;padding:16px 18px;">
          <p style="margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:0.6px;
                    text-transform:uppercase;color:#B91C1C;
                    font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
            Reason for Early Clock Out
          </p>
          <p style="margin:0;font-size:14px;line-height:1.6;color:#1F2937;
                    font-family:'Segoe UI',Helvetica,Arial,sans-serif;">{safe_reason}</p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr><td align="center">{_btn("Open Temveris Admin Dashboard", dash_url)}</td></tr>
    </table>
    """
    html = _base_layout(body, f"Early Clock Out — {employee_name} ({_human_duration(remaining_seconds)} remaining)")
    subject = f"Early Clock Out — {employee_name}"
    return _send_all(recipients, subject, html,
                     classification="operational",
                     family="attendance.early_clockout")


# ══════════════════════════════════════════════
# 8. TIME ADJUSTMENT → EMPLOYEE
# Sent when an admin manually credits an employee with work-time that
# was genuinely worked but lost to a power cut / tracker crash / network
# outage. Goes to the employee's own work inbox so they can spot-check
# the entry (right amount, right date, plausible reason) and reply to
# the admin if it's wrong. HR is intentionally NOT cc'd — per the
# 2026-05-13 governance decision, manual time changes are admin-only
# and the email path stays narrow to avoid notification spam.
# ══════════════════════════════════════════════
def send_time_adjustment_email(
    to_email: str,
    employee_name: str,
    adjustment_date: str,
    duration_label: str,
    reason: str,
    admin_name: str = "Admin",
) -> bool:
    dash_url = f"{FRONTEND_URL}/dashboard"
    body = f"""
    <div style="background:#ECFDF5;border:2px solid #6EE7B7;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#065F46;font-size:20px;">Work time added to your day</h2>
      <p style="margin:0;color:#064E3B;font-size:13px;">
        Hi {employee_name}, your administrator added missing work time to your
        timesheet to correct for a tracker outage or system issue. Your
        worked-hours totals have been updated to reflect this.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Date",            adjustment_date)}
      {_info_row("Time Credited",   f'<strong style="color:#065F46;">{duration_label}</strong>', True)}
      {_info_row("Reason",          reason)}
      {_info_row("Added By",        admin_name, True)}
    </table>
    <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#1E3A8A;font-size:13px;line-height:1.6;">
        <strong>If this looks wrong:</strong> please reply to your admin so they
        can correct or revert the entry. Adjustments are audited and can be
        reversed without losing your original tracker history.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open My Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, "Work time added to your day")
    subject = f"Time added to your timesheet – {adjustment_date} ({duration_label})"
    return _send(to_email, subject, html)


def send_time_adjustment_reverted_email(
    to_email: str,
    employee_name: str,
    adjustment_date: str,
    duration_label: str,
    reason: str,
    admin_name: str = "Admin",
) -> bool:
    """Sent when an admin reverts a previously-added time adjustment.

    Employees see both directions of the audit trail — when time is
    added AND when it's removed — so a silent revert can't quietly
    shrink someone's hours without them knowing.
    """
    dash_url = f"{FRONTEND_URL}/dashboard"
    body = f"""
    <div style="background:#FFFBEB;border:2px solid #FCD34D;border-radius:10px;
                padding:20px 24px;margin-bottom:24px;">
      <h2 style="margin:0 0 6px;color:#92400E;font-size:20px;">Time adjustment reverted</h2>
      <p style="margin:0;color:#78350F;font-size:13px;">
        Hi {employee_name}, an earlier work-time credit on your timesheet has
        been removed by your administrator. Your worked-hours totals for that
        day have been updated.
      </p>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Date",                adjustment_date)}
      {_info_row("Amount Removed",      f'<strong style="color:#92400E;">{duration_label}</strong>', True)}
      {_info_row("Original Reason",     reason)}
      {_info_row("Reverted By",         admin_name, True)}
    </table>
    <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;">
      <p style="margin:0;color:#1E3A8A;font-size:13px;line-height:1.6;">
        <strong>If this looks wrong:</strong> please reply to your admin. Reverts
        are also audited and the underlying tracker record was never modified.
      </p>
    </div>
    <div style="text-align:center;">
      {_btn("Open My Dashboard", dash_url)}
    </div>
    """
    html = _base_layout(body, "A time adjustment on your timesheet was reverted")
    subject = f"Time adjustment reverted – {adjustment_date} ({duration_label})"
    return _send(to_email, subject, html)


# ══════════════════════════════════════════════
# IN-APP SUPPORT TICKET (Electron tracker "Need Help?" form)
# ══════════════════════════════════════════════
def send_support_ticket_email(
    name: str,
    work_email: str,
    issue: str,
    *,
    source: str = "desktop-tracker",
) -> bool:
    """
    Forwards a help-form submission to support@temveris.example.

    `reply_to` is set to the user's work_email so a single Reply from the
    support inbox lands directly in their mailbox — no copy-paste, no
    rewriting the To: header. The body still spells out who submitted it
    for the case where the support agent's client strips Reply-To.
    """
    safe_name  = (name or "").strip() or "(no name)"
    safe_email = (work_email or "").strip()
    # Escape HTML in user-supplied content so a malformed paste can't break
    # the email body. The plain-text alternative also goes through this
    # because the issue is rendered as <pre>.
    import html as _html_mod
    esc_name  = _html_mod.escape(safe_name)
    esc_email = _html_mod.escape(safe_email)
    esc_issue = _html_mod.escape(issue or "(empty)")
    esc_src   = _html_mod.escape(source)
    submitted = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td>
          <h2 style="margin:0 0 8px;color:{DARK};font-size:22px;">New support request</h2>
          <p style="color:#64748B;font-size:14px;line-height:1.7;margin:0 0 18px;">
            A worker just submitted a help request from the Temveris
            desktop tracker. Hit <strong>Reply</strong> to respond directly
            to them — Reply-To is already set.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:18px;">
            {_info_row("From",       esc_name)}
            {_info_row("Work email", esc_email, True)}
            {_info_row("Source",     esc_src)}
            {_info_row("Submitted",  submitted, True)}
          </table>
          <p style="margin:0 0 6px;font-weight:600;color:#374151;font-size:13px;">Issue:</p>
          <pre style="margin:0;padding:14px 16px;background:#F8FAFC;border:1px solid #E2E8F0;
                      border-radius:8px;font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;
                      font-size:13px;line-height:1.6;color:#1F2937;white-space:pre-wrap;word-wrap:break-word;">{esc_issue}</pre>
        </td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"Support request from {safe_name}")

    short_summary = (issue or "").strip().split("\n", 1)[0][:60] or "Support request"
    subject = f"[Support] {safe_name} — {short_summary}"

    # Route to the support inbox; Reply-To is the worker so a single click
    # in the support agent's mail client opens a reply to them.
    return _send(
        "support@temveris.example",
        subject,
        html,
        reply_to=safe_email or None,
    )

# ══════════════════════════════════════════════
# SCHEDULE PUBLISHED / CHANGED / CANCELED  (ZT-ATT-01)
# Operational - workforce. Sent to the affected worker when a shift assignment
# is published, changed, or canceled. Always sends (fail-open) — no preference
# context is passed. From notifications@ when sender identities are enabled;
# reply-to = schedule owner when provided. work_location / timezone are not in
# the assignment model and are intentionally omitted (not invented).
# ══════════════════════════════════════════════
_SCHEDULE_VARIANTS = {
    "published": ("Your schedule for {date} is available", "Your schedule has been published"),
    "changed":   ("Schedule change: {date}",                "Your schedule has been updated"),
    "cancelled": ("Shift canceled: {date}",                 "A scheduled shift has been canceled"),
}


def send_schedule_notification(
    to_email: str,
    name: str,
    variant: str,
    date: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    task_type: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    subj_tmpl, heading = _SCHEDULE_VARIANTS.get(variant, _SCHEDULE_VARIANTS["published"])
    subject = subj_tmpl.format(date=date)
    schedule_url = FRONTEND_URL.rstrip("/") + "/"
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            {heading}
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            {heading} for <strong>{date}</strong>.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Date", date)}
      {_info_row("Time", f"{start_time or '—'}–{end_time or '—'}", True) if (start_time or end_time) else ""}
      {_info_row("Type", task_type, not bool(start_time or end_time)) if task_type else ""}
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#64748B;line-height:1.7;">
            Open Temveris to review the details or report an issue according to your organization's policy.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("Review schedule", schedule_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"{heading} — {date}")
    return _send(
        to_email, subject, html,
        reply_to=reply_to, classification="operational", family="attendance.schedule",
    )


# ══════════════════════════════════════════════
# LEAVE REQUEST SUBMITTED → EMPLOYEE  (ZT-LVE-01)
# Operational - leave. Confirmation to the requesting worker on submit. Always
# sends (fail-open) — no preference context is passed. From approvals@ when
# sender identities are enabled. NEVER includes leave reason / medical details.
# ══════════════════════════════════════════════
def send_leave_submitted_to_employee(
    to_email: str,
    name: str,
    leave_type: str,
    from_date: str,
    to_date: str,
    leave_amount=None,
    reply_to: Optional[str] = None,
) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    leave_range = f"{from_date} to {to_date}"
    category = (leave_type or "").replace("_", " ").title()
    amount_row = (
        _info_row("Requested amount", f"{leave_amount} day(s)", True)
        if leave_amount is not None else ""
    )
    leave_url = FRONTEND_URL.rstrip("/") + "/leave"
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            Leave request submitted
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            Your leave request for <strong>{leave_range}</strong> has been submitted and is
            awaiting the configured approval process.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Leave category", category or "—")}
      {amount_row}
      {_info_row("Current status", "Pending approval")}
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#64748B;line-height:1.7;">
            The request is not approved until Temveris shows an approved status.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("View leave request", leave_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"Leave request submitted: {leave_range}")
    return _send(
        to_email, f"Leave request submitted: {leave_range}", html,
        reply_to=reply_to, classification="operational", family="leave.submitted",
    )


# ══════════════════════════════════════════════
# SUPPORT REQUEST RECEIVED → REQUESTER  (ZT-SUP-01)
# Essential - support, non-suppressible. Acknowledgment to the person who filed
# the ticket. From/reply-to support@ when sender identities are enabled. Carries
# only reference / subject / priority / status — never the ticket body or any
# sensitive data — plus the "don't send passwords/PII by email" note.
# ══════════════════════════════════════════════
def send_support_request_received(
    to_email: str,
    name: str,
    ticket_reference: str,
    subject: str,
    priority=None,
    status: str = "submitted",
) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    ticket_url = FRONTEND_URL.rstrip("/") + "/help"
    priority_row = _info_row("Priority", str(priority), True) if priority else ""
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            We received your support request
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            We received your Temveris support request. Your reference is
            <strong>{ticket_reference}</strong>.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Reference", ticket_reference)}
      {_info_row("Subject", subject or "—", True)}
      {priority_row}
      {_info_row("Status", status)}
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#64748B;line-height:1.7;">
            Add information through the support portal. Do not send passwords, verification
            codes, payment card details, or unnecessary employee data.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("View support request", ticket_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"We received your Temveris support request {ticket_reference}")
    try:
        from app.notifications import classification as _cls
        reply_to = _cls.sender_for(sender_key="support")
    except Exception:
        reply_to = None
    return _send(
        to_email, f"We received your Temveris support request {ticket_reference}", html,
        reply_to=reply_to, classification="essential", family="support.ticket",
    )


# ══════════════════════════════════════════════
# DATA REQUEST RECEIVED → REQUESTER  (ZT-DAT-01, received leg only)
# Critical - data governance, non-suppressible. Acknowledgment to the person who
# filed a data-rights request (DSAR / deletion / export / correction). From and
# reply-to privacy@ when sender identities are enabled. Carries ONLY reference /
# type / status / due date — never the request details or personal data.
# ══════════════════════════════════════════════
def send_data_request_received(
    to_email: str,
    name: str,
    request_reference,
    request_type: str,
    status: str = "submitted",
    due_date=None,
) -> bool:
    first_name = name.strip().split()[0] if name and name.strip() else "there"
    ref = str(request_reference)
    request_url = FRONTEND_URL.rstrip("/") + "/account-settings"
    due_row = _info_row("Due date", str(due_date), True) if due_date else ""
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0 0 6px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:22px;font-weight:700;color:{DARK};">
            We received your data request
          </p>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:14px;color:#64748B;line-height:1.7;">
            Hi <strong style="color:{DARK};">{first_name}</strong>,<br>
            We received your Temveris data request. Your reference is <strong>{ref}</strong>.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      {_info_row("Reference", ref)}
      {_info_row("Type", request_type or "—", True)}
      {_info_row("Status", status)}
      {due_row}
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
      <tr>
        <td>
          <p style="margin:0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                    font-size:13px;color:#64748B;line-height:1.7;">
            Sensitive data and identity evidence must be provided only through the secure
            request process. Do not send passwords or personal data by email.
          </p>
        </td>
      </tr>
    </table>

    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center">{_btn("Review data request", request_url)}</td>
      </tr>
    </table>
    """
    html = _base_layout(body, f"We received your Temveris data request {ref}")
    try:
        from app.notifications import classification as _cls
        reply_to = _cls.sender_for(sender_key="privacy")
    except Exception:
        reply_to = None
    return _send(
        to_email, f"We received your Temveris data request {ref}", html,
        reply_to=reply_to, classification="critical", family="data.rights",
    )
