"""
Temveris -- FastAPI Entry Point
Workforce Assurance Platform
Operational Truth + Intelligence + Governance layers
"""

# Force UTF-8 on stdout/stderr BEFORE anything else imports. Windows defaults
# to cp1252 which can't encode the emojis in our config/log lines (📧, →, ✅,
# ❌). Without this reconfigure every emoji-laden logger.info raises
# UnicodeEncodeError on console write — and because logging writes to stderr,
# PowerShell renders the broken lines in red, making clean boots look like
# fatal errors. utf-8 with errors='replace' is the safest combo: real
# encoding bugs still show up but stay one-character glitches instead of
# tearing down whichever code path emitted the log.
import sys as _sys
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(_sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Load .env FIRST -- before any module-level os.getenv() calls in routers/services
from dotenv import load_dotenv

load_dotenv(override=False)  # system env vars take precedence; .env fills the rest

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    WebSocket,
    WebSocketDisconnect,
    Depends,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
from database.database import engine, get_db, SessionLocal
from database import models

from app.auth.service import router as auth_router, get_current_organization, get_current_user, require_super_admin
from app.auth.sso import router as auth_sso_router
from app.employees.router import router as employees_router
from app.leave.router import router as leave_router
from app.screenshots.router import router as screenshots_router
from app.intelligence.router import router as intelligence_router
from app.tracking.router import router as tracking_router
from app.desktop.router import router as desktop_router
from app.browser.router import router as browser_router
from app.wec.router import router as wec_router
from app.events.router import router as events_router
from app.settings.router import router as settings_router
from app.breaks.router import router as breaks_router
from app.risk.router import router as risk_router
from app.org.router import router as org_router
from app.payroll.router import router as payroll_router
from app.ai.router import router as ai_router
from app.proof_of_work.wec_routes import router as pow_wec_router
from app.proof_of_work.fraud_routes import router as pow_fraud_router
from app.reports.router import router as reports_router
from app.holidays.router import router as holidays_router
from app.app_review.router import router as app_review_router
from app.activity_log.router import router as activity_log_router
from app.schedule.router import router as schedule_router
from app.profile.router import router as profile_router
from app.my_data.router import router as my_data_router
from app.notifications.router import router as notifications_router, admin_router as admin_notifications_router, public_router as public_notifications_router
from app.help.router import router as help_router
from app.worker_settings.router import router as worker_settings_router
from app.eco_day_view.router import router as eco_day_router
from app.billing.router import router as billing_router
from app.kairos import router as kairos_router, ensure_tables as ensure_kairos_tables

# Workspace Settings backend — Tabs 4-10 of the Workspace Settings spec
# (Roles, Policies, Billing summary, Security, Integrations, Audit).
from app.workspace.router import router as workspace_router
from app.workspace.audit_governance import (
    router as audit_governance_router,
    ensure_tables as ensure_audit_governance_tables,
)
from app.workspace.security_center import (
    router as security_center_router,
    ensure_tables as ensure_security_center_tables,
)

# Super-admin portal — platform-ops view of every commercial organization.
# Strictly gated to users whose email is in SUPER_ADMIN_EMAILS env var;
# the auth bootstrap auto-promotes their role to `super_admin` on login.
from app.super_admin.router import router as super_admin_router

# Commercial Activation System (Tier-0 spec v3.0) — multi-tenant signup,
# entitlements, billing, fraud, CRM, admin console. Lives entirely under
# /api/v1/* and /api/v1/admin/commercial/* — does not touch legacy routes.
from app.commercial.bootstrap import (
    ensure_tables as commercial_ensure_tables,
    ensure_internal_identity as commercial_ensure_internal_identity,
    install_routers as commercial_install_routers,
)
from app.commercial import dunning as commercial_dunning
from app.commercial import alerts as commercial_alerts
# The customer-facing 14-day trial ladder. Replaces app.commercial.trial_lifecycle,
# which ran a conflicting 30-day ladder and is deliberately no longer imported or
# scheduled here — see the lifespan handler for why.
from app.billing import trial as billing_trial
from app import attendance_alerts
from app import _tz

# Offboarding & Former-Worker Lifecycle module — governed exit closure system
# (access revocation, tracking stop, audit evidence, legal hold, retention,
# former-worker portal). Lives entirely under /api/offboarding/*.
from app.offboarding.bootstrap import (
    ensure_tables as offboarding_ensure_tables,
    install_router as offboarding_install_router,
)
from app.offboarding import jobs as offboarding_jobs
from app.breaks import monitor as break_monitor
from app.active_time import worker as active_time_worker

from websocket_manager import manager
from app.auth.service import decode_access_token  # JWT validation for the /ws handshake
from app.org import lifecycle as org_lifecycle   # tenant suspension gate for /ws

import asyncio
import os, shutil, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from contextlib import asynccontextmanager

_db_url = os.getenv("DATABASE_URL", "")
print("DB URL:", _db_url[:30] + "..." if _db_url else "NOT SET")
print("SUPABASE URL:", os.getenv("SUPABASE_URL"))

# ----------------------------------------------
# DIRS
# ----------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "..", "tracker-data", "uploads")
ATTACH_DIR = os.path.join(BASE_DIR, "..", "tracker-data", "leave-attachments")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ATTACH_DIR, exist_ok=True)



# ══════════════════════════════════════════════════════════════════════════════
# DAILY SCREENSHOT CLEANUP TASK
# ══════════════════════════════════════════════════════════════════════════════
# Runs in the background, once every 24 hours. Deletes screenshots older than
# each organization's retention window from the Supabase bucket AND nulls out file_path/thumbnail
# fields in the activities table. Activity ROWS are preserved (timeline/stats/
# timesheet remain accurate) — only the image is removed.
#
# No external scheduler / cron / JWT needed. The task is started in lifespan
# and cancelled cleanly on shutdown.
#
# On Cloud Run, containers restart frequently (cold starts, deploys, idle
# scale-to-zero). That's fine — the task is idempotent, so worst case a
# screenshot gets deleted at day 31 or 32 instead of exactly day 30. Not a
# correctness issue for a retention policy.
# ══════════════════════════════════════════════════════════════════════════════
async def daily_cleanup_task():
    """Deletes screenshots past each organization's own retention window, every 24h.

    Runs forever until cancelled by the lifespan shutdown. Waits 24h between
    runs (the first run happens 24h after the app boots). Any exceptions are
    caught + logged so a single bad run doesn't kill the scheduler.
    """
    import requests as _requests

    # No global RETENTION_DAYS: the window is retention.screenshot_days per
    # organization (see app/storage/retention.py). The env var survives only as
    # that setting's SYSTEM DEFAULT so an existing deployment is unchanged.
    SLEEP_SECONDS = 24 * 60 * 60  # 24 hours

    print(
        "[scheduler] Daily cleanup task started (per-organization retention)",
        flush=True,
    )

    while True:
        try:
            # Wait FIRST so we don't slam the bucket during cold start.
            # First cleanup will run ~24h after boot.
            await asyncio.sleep(SLEEP_SECONDS)

            print("[scheduler] Running daily screenshot cleanup...", flush=True)

            backend = (os.getenv("STORAGE_BACKEND") or "local").strip().lower()
            supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
            bucket = os.getenv("SUPABASE_SCREENSHOTS_BUCKET", "screenshots")

            db = SessionLocal()
            try:
                # Retention is PER ORGANIZATION (retention.screenshot_days).
                # One global cutoff applied every tenant's data to whichever
                # value the operator happened to set — a customer with a
                # 30-day statutory limit and one with a 12-month audit
                # requirement cannot both be served by a single number.
                from app.storage.retention import retention_days as _retention_days

                org_ids = [
                    r[0] for r in db.query(models.Organization.id).all()
                ]
                now_utc = datetime.now(timezone.utc)
                old_activities = []
                for _org_id in org_ids:
                    _days = _retention_days(db, _org_id)
                    if _days is None:
                        continue          # this tenant keeps its data
                    _cutoff = now_utc - timedelta(days=_days)
                    old_activities.extend(
                        db.query(models.Activity)
                        .filter(
                            models.Activity.organization_id == _org_id,
                            models.Activity.timestamp < _cutoff,
                            models.Activity.file_path.isnot(None),
                        )
                        .all()
                    )

                if not old_activities:
                    print(
                        "[scheduler] No screenshots past any tenant's retention window",
                        flush=True,
                    )
                    continue

                print(
                    f"[scheduler] Found {len(old_activities)} screenshots to delete",
                    flush=True,
                )

                # Prepare (activity, filename_in_bucket) tuples.
                # file_path is stored as "/uploads/80_1776860076463.webp".
                # Supabase bucket filename is just "80_1776860076463.webp".
                to_delete = []
                for act in old_activities:
                    fname = (
                        act.file_path.replace("/uploads/", "")
                        if act.file_path
                        else None
                    )
                    if fname:
                        to_delete.append((act, fname))

                deleted = 0
                errors = 0
                batch_size = 100

                # Delete from Supabase bucket in batches of 100
                if backend == "supabase" and supabase_url and service_key:
                    for i in range(0, len(to_delete), batch_size):
                        batch = to_delete[i : i + batch_size]
                        filenames = [fname for _, fname in batch]
                        try:
                            # Offload the blocking HTTP call to a worker thread so
                            # the daily-cleanup batch loop never stalls the asyncio
                            # event loop (this runs inside an async task).
                            resp = await asyncio.to_thread(
                                _requests.delete,
                                f"{supabase_url}/storage/v1/object/{bucket}",
                                headers={
                                    "Authorization": f"Bearer {service_key}",
                                    "Content-Type": "application/json",
                                },
                                json={"prefixes": filenames},
                                timeout=30,
                            )
                            if resp.status_code in (200, 204):
                                deleted += len(batch)
                                print(
                                    f"[scheduler] deleted batch of {len(batch)} "
                                    f"files from bucket",
                                    flush=True,
                                )
                            else:
                                errors += len(batch)
                                print(
                                    f"[scheduler] bucket delete FAILED "
                                    f"status={resp.status_code} "
                                    f"body={resp.text[:300]}",
                                    flush=True,
                                )
                        except Exception as e:
                            errors += len(batch)
                            print(
                                f"[scheduler] bucket delete exception: {e!r}",
                                flush=True,
                            )
                else:
                    # Local fallback: delete files from disk
                    for act, fname in to_delete:
                        try:
                            local_path = os.path.join(UPLOAD_DIR, fname)
                            if os.path.exists(local_path):
                                os.remove(local_path)
                            deleted += 1
                        except Exception as e:
                            errors += 1
                            print(
                                f"[scheduler] local delete failed {fname}: {e!r}",
                                flush=True,
                            )

                # NULL out file_path/thumbnail so activity rows stay intact but
                # frontend shows the placeholder camera icon. Single bulk UPDATE.
                act_ids = [act.id for act, _ in to_delete]
                if act_ids:
                    db.query(models.Activity).filter(
                        models.Activity.id.in_(act_ids)
                    ).update(
                        {"file_path": None, "thumbnail": None},
                        synchronize_session=False,
                    )
                    db.commit()

                print(
                    f"[scheduler] CLEANUP DONE deleted={deleted} errors={errors} "
                    "per-organization retention",
                    flush=True,
                )
            finally:
                db.close()

        except asyncio.CancelledError:
            # Graceful shutdown
            print("[scheduler] Daily cleanup task cancelled, exiting", flush=True)
            raise
        except Exception as e:
            # Never let a single failed run kill the scheduler — just log and retry tomorrow
            print(
                f"[scheduler] Cleanup task error (will retry in 24h): {e!r}", flush=True
            )
            import traceback

            print(traceback.format_exc(), flush=True)


# ----------------------------------------------
# STARTUP / MIGRATIONS
# ----------------------------------------------
@asynccontextmanager
async def lifespan(_):

    # Give the WebSocket manager a reference to the running event loop so
    # broadcast_sync() works correctly from sync endpoints (threadpool workers).
    manager.set_loop(asyncio.get_running_loop())

    # ── Storage backend startup check ──────────────────────────────────────
    import os as _os

    _storage_backend = (_os.getenv("STORAGE_BACKEND") or "local").strip().lower()
    _supabase_url = _os.getenv("SUPABASE_URL", "")
    _supabase_key = _os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    _bucket = _os.getenv("SUPABASE_SCREENSHOTS_BUCKET", "screenshots")
    print(f"[startup] STORAGE_BACKEND={_storage_backend!r}", flush=True)
    print(f"[startup] SUPABASE_URL={'SET' if _supabase_url else 'MISSING'}", flush=True)
    print(
        f"[startup] SUPABASE_SERVICE_ROLE_KEY={'SET ('+_supabase_key[:12]+'...)' if _supabase_key else 'MISSING'}",
        flush=True,
    )
    print(f"[startup] SUPABASE_SCREENSHOTS_BUCKET={_bucket!r}", flush=True)

    # ── Start the daily screenshot cleanup task ────────────────────────────
    # Runs every 24 hours in the background. Cancelled cleanly on shutdown.
    cleanup_task_handle = asyncio.create_task(daily_cleanup_task())
    print("[startup] Daily screenshot cleanup task scheduled", flush=True)

    # Collapse SQLAlchemy/psycopg2 exception strings (they embed "\n\n
    # (Background on this error at: ...)" tails that turn a single
    # startup-failure print into 3 lines of console noise).
    def _summarize_exc(e: Exception) -> str:
        return " ".join(str(e).split())[:200]

    # ── Commercial Activation System startup ───────────────────────────────
    # 1. Ensure commercial_* tables exist (idempotent).
    # 2. Start the trial lifecycle scheduler (Day 0..35 emails + state moves).
    try:
        commercial_ensure_tables()
        print("[startup] Commercial Activation System: tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] Commercial table ensure failed: {_summarize_exc(_e)}", flush=True)

    # 1b. Give the vendor tenant a commercial identity so it resolves through the
    # entitlement system instead of around it. Idempotent; creates no billing
    # record and no Stripe object. Runs after the tables exist.
    try:
        commercial_ensure_internal_identity()
        print("[startup] Commercial Activation System: internal identity ensured", flush=True)
    except Exception as _e:
        print(f"[startup] Internal identity ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Password-reset OTP table (Forgot Password flow) ─────────────────────
    # The model lives in database/models.py; we just need to make sure the
    # physical table exists on every boot (idempotent).
    try:
        from database.database import engine as _engine
        from database.models import PasswordResetOTP as _PROTP
        _PROTP.__table__.create(bind=_engine, checkfirst=True)
        print("[startup] password_reset_otps table ensured", flush=True)
    except Exception as _e:
        print(f"[startup] password_reset_otps ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Refresh-token table (persistent "Remember for 30 days" login) ───────
    # New in the rotating-refresh-token auth system. user_sessions already
    # exists; we only need to materialise refresh_tokens (idempotent).
    try:
        from database.database import engine as _engine_rt
        from database.models import UserSession as _US, RefreshToken as _RT
        _US.__table__.create(bind=_engine_rt, checkfirst=True)
        _RT.__table__.create(bind=_engine_rt, checkfirst=True)
        print("[startup] refresh_tokens table ensured", flush=True)
    except Exception as _e:
        print(f"[startup] refresh_tokens ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Email suppression table (one-click List-Unsubscribe list) ───────────
    # Additive, idempotent. Backs the public /notifications/unsubscribe
    # endpoints. Does not touch any existing table or email flow.
    try:
        from database.database import engine as _engine_es
        from database.models import EmailSuppression as _ES
        _ES.__table__.create(bind=_engine_es, checkfirst=True)
        print("[startup] email_suppressions table ensured", flush=True)
    except Exception as _e:
        print(f"[startup] email_suppressions ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Offline-interval audit table (network-pause deduction provenance) ────
    # Additive, idempotent. Backs the bounded + audited /employees/network-pause
    # deduction. The clock_sessions.offline_seconds column it pairs with ships
    # via Alembic (d5e8a1c34b70) per the note on the disabled blocks below —
    # only the NEW table is materialised here.
    try:
        from database.database import engine as _engine_oi
        from database.models import OfflineInterval as _OI
        _OI.__table__.create(bind=_engine_oi, checkfirst=True)
        print("[startup] offline_intervals table ensured", flush=True)
    except Exception as _e:
        print(f"[startup] offline_intervals ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── users.official_start_date column (Leave eligibility feature) ────────
    # Postgres `ADD COLUMN IF NOT EXISTS` is supported in 9.6+ and is the
    # safest no-op when the column already exists. We avoid SQLAlchemy
    # create_all here because the users table already exists; we only need
    # one new column on it.
    # DISABLED 2026-06-18: column already exists in production (verified). The
    # startup ALTER only caused lock contention + LockNotAvailable log noise +
    # slow cross-region DDL on every instance boot. Uncomment to re-enable, or
    # add future columns via an Alembic migration instead.
    # try:
    #     from database.database import engine as _engine2
    #     from sqlalchemy import text as _sql_text
    #     with _engine2.begin() as _conn:
    #         _conn.execute(_sql_text("SET LOCAL lock_timeout = '5s'"))
    #         _conn.execute(_sql_text(
    #             "ALTER TABLE users ADD COLUMN IF NOT EXISTS official_start_date TIMESTAMP NULL"
    #         ))
    #     print("[startup] users.official_start_date column ensured", flush=True)
    # except Exception as _e:
    #     print(f"[startup] users.official_start_date ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── live_statuses.app_version column (desktop tracker build report) ─────
    # Same idempotent ADD COLUMN IF NOT EXISTS pattern as above. Lets the admin
    # dashboard see which version each employee's tracker is on without a
    # separate poll, so we can finally tell at a glance who missed an update.
    # DISABLED 2026-06-18: column already exists in production (verified). See
    # the note on the official_start_date block above. Uncomment to re-enable.
    # try:
    #     from database.database import engine as _engine_lsv
    #     from sqlalchemy import text as _sql_text2
    #     with _engine_lsv.begin() as _conn:
    #         _conn.execute(_sql_text2("SET LOCAL lock_timeout = '5s'"))
    #         _conn.execute(_sql_text2(
    #             "ALTER TABLE live_statuses ADD COLUMN IF NOT EXISTS app_version VARCHAR(32) NULL"
    #         ))
    #     print("[startup] live_statuses.app_version column ensured", flush=True)
    # except Exception as _e:
    #     print(f"[startup] live_statuses.app_version ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Workforce Intelligence derived tables (Phase 2) ─────────────────────
    try:
        from database.database import engine as _engine_wi
        from database.models import (
            FocusSession as _FS,
            WorkflowTransition as _WT,
            DailyIntelligenceSummary as _DIS,
        )
        _FS.__table__.create(bind=_engine_wi, checkfirst=True)
        _WT.__table__.create(bind=_engine_wi, checkfirst=True)
        _DIS.__table__.create(bind=_engine_wi, checkfirst=True)
        print("[startup] workforce intelligence tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] workforce intelligence ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── billing_subscriptions.stripe_customer_id (Stripe integration) ─────────
    # DISABLED 2026-06-18: column already exists in production (verified). See
    # the note on the official_start_date block above. Uncomment to re-enable.
    # try:
    #     from database.database import engine as _engine3
    #     from sqlalchemy import text as _sql_text3
    #     with _engine3.begin() as _conn:
    #         _conn.execute(_sql_text3("SET LOCAL lock_timeout = '5s'"))
    #         _conn.execute(_sql_text3(
    #             "ALTER TABLE billing_subscriptions ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(120) NULL"
    #         ))
    #     print("[startup] billing_subscriptions.stripe_customer_id column ensured", flush=True)
    # except Exception as _e:
    #     print(f"[startup] billing_subscriptions.stripe_customer_id ensure failed: {_summarize_exc(_e)}", flush=True)

    # Audit governance tables — policy versions, exports, evidence packages,
    # legal holds, retention rules. Seeds default retention + policy rows on
    # first run so the Audit & Compliance page has content out of the box.
    try:
        ensure_audit_governance_tables()
        print("[startup] audit governance tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] audit governance ensure failed: {_summarize_exc(_e)}", flush=True)

    # Security Center tables — trusted domains, exceptions, break-glass accounts
    # + seeds the AppSettings defaults that drive the 7-tab Security page.
    try:
        ensure_security_center_tables()
        print("[startup] security center tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] security center ensure failed: {_summarize_exc(_e)}", flush=True)

    # Kairos chatbot tables (chatbot_conversations, chatbot_messages,
    # chatbot_unknown_prompts, chatbot_email_rate_limits). Without this the
    # chat endpoint 500s on the first DB write because the tables only exist
    # if app/kairos/schema.sql was applied manually.
    try:
        ensure_kairos_tables()
        print("[startup] Kairos chat tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] Kairos table ensure failed: {_summarize_exc(_e)}", flush=True)

    # Offboarding & Former-Worker Lifecycle tables (offboarding_cases,
    # offboarding_tasks, access_revocation_records, former_worker_access,
    # retention_assignments, legal_holds, completion_certificates,
    # evidence_packages, audit_events, domain_events, blockers, waivers,
    # reconciliation queue). Idempotent on every boot.
    try:
        offboarding_ensure_tables()
        print("[startup] Offboarding lifecycle tables ensured", flush=True)
    except Exception as _e:
        print(f"[startup] Offboarding table ensure failed: {_summarize_exc(_e)}", flush=True)

    # ── Customer trial lifecycle ──────────────────────────────────────────────
    # NOT STARTED: app.commercial.trial_lifecycle. It ran a 30-DAY ladder
    # (commercial constants.TRIAL_LENGTH_DAYS = 30) against commercial_subscriptions
    # while the billing page showed a 14-DAY countdown from
    # billing_subscriptions.trial_end_date — two clocks 16 days apart, with the email
    # coming from one and the customer-visible number from the other.
    #
    # Temveris's trial is 14 days (confirmed 2026-08-18), so the billing clock is now
    # authoritative and app/billing/trial.py is the only sender of trial mail. The
    # commercial module itself is left untouched (its models, state machine and
    # templates still exist and its tests still pass) — it is simply no longer
    # scheduled, which is what stops the second ladder without deleting working code.
    #
    # Starting both would send every customer two sets of trial emails on two
    # timetables. If the commercial ladder is ever wanted again, the billing one must be
    # stopped in the same change.
    trial_task_handle = asyncio.create_task(
        billing_trial.run_forever(interval_seconds=3600, initial_delay_seconds=1020)
    )
    print("[startup] Billing trial lifecycle started (14-day ladder, 1h cadence)",
          flush=True)

    commercial_dunning_task_handle = asyncio.create_task(
        commercial_dunning.run_forever(interval_seconds=3600)
    )
    print("[startup] Dunning scheduler started (1h cadence)", flush=True)

    # §21.3 alert monitor — polls events + ratios, dispatches when a
    # threshold (provisioning failure, payment spike, etc.) is breached.
    alerts_task_handle = asyncio.create_task(
        commercial_alerts.run_forever(interval_seconds=300)
    )
    print("[startup] Alert monitor started (5m cadence)", flush=True)

    # Offboarding enforcement scheduler — disabled to reduce DB load on web
    # servers. Re-enable by restoring the create_task call below, or move this
    # to a dedicated Cloud Run Job / Cloud Tasks worker.
    # offboarding_jobs_task_handle = asyncio.create_task(
    #     offboarding_jobs.run_forever(interval_seconds=300)
    # )
    # print("[startup] Offboarding enforcement scheduler started (5m cadence)", flush=True)

    # Break overrun monitor — continuous monitoring of active breaks: marks
    # OVERDUE, appends BREAK_ALLOWANCE_REACHED/BREAK_OVERRUN provenance, and
    # escalates to HR (first alert at the threshold, reminder every 30m).
    break_monitor_task_handle = asyncio.create_task(
        break_monitor.run_forever(interval_seconds=60)
    )
    print("[startup] Break overrun monitor started (1m cadence)", flush=True)

    # Requires-Attention monitor — notifies Admin + HR about workers who are
    # Late (past their own shift start + grace, before their absence cutoff) or
    # Absent (no clock-in past that cutoff), whether or not anyone has the
    # Overview dashboard open. Reuses the EmployeeMarkedAbsent dedup shared with
    # /employees/team.
    #
    # "noon" is NOT the rule and has not been since the absence cutoff became
    # shift-relative: it is only the fallback hour for a worker with no
    # scheduled start (schedule.absent_after_hour, per organization). Saying
    # "noon" here is what kept the old mental model alive while afternoon
    # shifts were being marked absent at their own start time.
    attendance_alerts_task_handle = asyncio.create_task(
        attendance_alerts.run_forever(interval_seconds=300)
    )
    print("[startup] Requires-Attention monitor started (5m cadence)", flush=True)

    # Active Time reconcile worker — recomputes the authoritative daily Active
    # Time % (continuity-aware) for recently-active workers and finalises their
    # per-minute scores. Keeps /employees/team a single indexed read instead of
    # an org-wide Activity scan.
    active_time_task_handle = asyncio.create_task(
        active_time_worker.run_forever(interval_seconds=60)
    )
    print("[startup] Active Time reconcile worker started (1m cadence)", flush=True)

    # ── Billing background jobs are deliberately staggered ────────────────────
    # Each `initial_delay_seconds` offset exists for a concrete reason. Every billing
    # sweep used to fire the instant the container booted: five at once, each taking a
    # connection from a pool of pool_size=5 + max_overflow=10 per worker, and three of
    # them (invoices, payment methods, reconcile) holding that connection through slow
    # Stripe HTTP calls. All of this while employees were clocking in after a deploy.
    # They then stayed in lockstep on the same 3600s sleep and collided again every
    # hour, forever.
    #
    # Billing must never be the reason the platform stalls. The offsets below spread the
    # first run across ~17 minutes and keep the hourly jobs permanently out of phase
    # with each other.
    # Billing seat reconciliation — compares each organization's billable seat
    # count against its Stripe subscription quantity and corrects any drift left
    # by a failed lifecycle sync or an unreachable Stripe. Read-only when
    # everything already agrees. (ZT-COM-BILL-001 Phase 2.2)
    from app.billing import seats as billing_seats
    seat_reconcile_task_handle = asyncio.create_task(
        billing_seats.run_forever(interval_seconds=900, initial_delay_seconds=90)
    )
    print("[startup] Billing seat reconciliation started (15m cadence)", flush=True)

    # Subscription reconciliation — status, plan/price, billing cycle, renewal
    # date and cancellation state, using Stripe as the source of truth. Catches
    # drift the seat sweep above cannot see: a missed webhook, an out-of-order
    # delivery, or a plan change that reached Stripe but failed to commit locally.
    # Hourly rather than 15m because it is a whole-catalog comparison, and seat
    # quantity (the fast-moving dimension) is already covered above.
    # (ZT-COM-BILL-001 Phase 3)
    from app.billing import reconcile as billing_reconcile
    subscription_reconcile_task_handle = asyncio.create_task(
        billing_reconcile.run_forever(interval_seconds=3600, initial_delay_seconds=300)
    )
    print("[startup] Billing subscription reconciliation started (1h cadence)", flush=True)

    # Invoice mirror sweep — safety net for invoices that arrived while a webhook was
    # missed. invoice.paid/payment_failed already refresh the mirror immediately, so
    # this only catches gaps (and open invoices, which produce no payment event).
    # Internal tenants are excluded in the query. (Phase 6)
    from app.billing import invoices as billing_invoices
    invoice_sync_task_handle = asyncio.create_task(
        billing_invoices.run_forever(interval_seconds=3600, initial_delay_seconds=480)
    )
    print("[startup] Billing invoice mirror started (1h cadence)", flush=True)

    # Payment-method mirror sweep — catches cards added or removed directly in the
    # Stripe portal. In-app additions are mirrored immediately by
    # /payment-method/confirm, so no new webhook event type is needed. (Phase 7)
    from app.billing import payment_methods as billing_payment_methods
    payment_method_sync_task_handle = asyncio.create_task(
        billing_payment_methods.run_forever(interval_seconds=3600, initial_delay_seconds=660)
    )
    print("[startup] Billing payment method mirror started (1h cadence)", flush=True)

    # Dunning ladder — sends the day 1/3/7/14 warnings and lets the 14-day grace period
    # expire into read-only. Duplicate sends are impossible: billing_dunning_notices has
    # UNIQUE (subscription_id, day_marker). Internal tenants are excluded in SQL. (Phase 8)
    from app.billing import dunning as billing_dunning
    dunning_task_handle = asyncio.create_task(
        billing_dunning.run_forever(interval_seconds=3600, initial_delay_seconds=840)
    )
    print("[startup] Billing dunning ladder started (1h cadence)", flush=True)

    # Applies APPROVED downgrade requests whose effective date has arrived. Pending
    # requests are never auto-applied — approval is super-admin only. (Phase 8)
    from app.billing import downgrades as billing_downgrades

    def _downgrade_sweep_once():
        db = SessionLocal()
        try:
            return billing_downgrades.apply_due(db)
        finally:
            db.close()          # a leaked session here would exhaust the pool

    async def _downgrade_sweep_forever(interval_seconds: int = 3600):
        while True:
            try:
                await asyncio.to_thread(_downgrade_sweep_once)
            except Exception as exc:
                print(f"[downgrade-sweep] failed: {exc}", flush=True)
            await asyncio.sleep(interval_seconds)

    downgrade_task_handle = asyncio.create_task(_downgrade_sweep_forever(3600))
    print("[startup] Billing downgrade application sweep started (1h cadence)", flush=True)

    try:
        yield
    finally:
        # Graceful shutdown: cancel the cleanup task and wait briefly for it
        cleanup_task_handle.cancel()
        try:
            await asyncio.wait_for(cleanup_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Daily cleanup task stopped", flush=True)

        trial_task_handle.cancel()
        try:
            await asyncio.wait_for(trial_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing trial lifecycle stopped", flush=True)

        # Was `dunning_task_handle`, which the billing dunning task later reassigned:
        # this line cancelled billing dunning, the billing shutdown block cancelled it
        # again, and the commercial dunning task was never cancelled at all — it kept
        # running through shutdown until the process died.
        commercial_dunning_task_handle.cancel()
        try:
            await asyncio.wait_for(commercial_dunning_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Commercial dunning scheduler stopped", flush=True)

        alerts_task_handle.cancel()
        try:
            await asyncio.wait_for(alerts_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Alert monitor stopped", flush=True)

        # offboarding_jobs_task_handle.cancel()
        # try:
        #     await asyncio.wait_for(offboarding_jobs_task_handle, timeout=5.0)
        # except (asyncio.CancelledError, asyncio.TimeoutError):
        #     pass
        # print("[shutdown] Offboarding enforcement scheduler stopped", flush=True)

        break_monitor_task_handle.cancel()
        try:
            await asyncio.wait_for(break_monitor_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Break overrun monitor stopped", flush=True)

        attendance_alerts_task_handle.cancel()
        try:
            await asyncio.wait_for(attendance_alerts_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Requires-Attention monitor stopped", flush=True)

        active_time_task_handle.cancel()
        try:
            await asyncio.wait_for(active_time_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Active Time reconcile worker stopped", flush=True)

        seat_reconcile_task_handle.cancel()
        try:
            await asyncio.wait_for(seat_reconcile_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing seat reconciliation stopped", flush=True)

        subscription_reconcile_task_handle.cancel()
        try:
            await asyncio.wait_for(subscription_reconcile_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing subscription reconciliation stopped", flush=True)

        invoice_sync_task_handle.cancel()
        try:
            await asyncio.wait_for(invoice_sync_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing invoice mirror stopped", flush=True)

        payment_method_sync_task_handle.cancel()
        try:
            await asyncio.wait_for(payment_method_sync_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing payment method mirror stopped", flush=True)

        dunning_task_handle.cancel()
        try:
            await asyncio.wait_for(dunning_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing dunning ladder stopped", flush=True)

        downgrade_task_handle.cancel()
        try:
            await asyncio.wait_for(downgrade_task_handle, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        print("[shutdown] Billing downgrade sweep stopped", flush=True)


# ----------------------------------------------
# APP
# ----------------------------------------------
app = FastAPI(
    title="Temveris API",
    description="Workforce Assurance Platform - Operational Truth + Intelligence + Governance",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — credentialed (cookie) requests can't use a wildcard origin: the
# browser rejects `Access-Control-Allow-Origin: *` when credentials are sent.
# So we REFLECT the request origin instead. Lock this down in production by
# setting CORS_ALLOW_ORIGINS to a comma-separated allowlist; leave it unset (or
# "*") to reflect any origin (matches the previous open behaviour, needed for
# the HttpOnly refresh-token cookie to work from the SPA + Electron shell).
_cors_env = (os.getenv("CORS_ALLOW_ORIGINS") or "").strip()
if _cors_env and _cors_env != "*":
    # Explicit allowlist (production/staging): exact origins + credentials.
    # Include the Electron shell's origin here too if it makes credentialed
    # requests from a non-localhost URL.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_env.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # No explicit allowlist configured → DEV ONLY. Restrict to localhost (any
    # port) instead of echoing ANY origin with credentials. The previous
    # wildcard-with-credentials fallback (`allow_origin_regex=".*"`) has been
    # removed — production MUST set CORS_ALLOW_ORIGINS, otherwise only local
    # dev origins are permitted (fails safe rather than open).
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# GZip every response over 500 bytes. Activity / employee / screenshot
# payloads are JSON-heavy and shrink 5-10x on the wire, which directly
# cuts the dashboard's perceived load time.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

# ── Per-endpoint API rate limiting (slowapi) ──
# Uses app.state.limiter only — NO SlowAPIMiddleware.
# SlowAPIMiddleware with default_limits=[] incorrectly blocks ALL routes at 0ms
# (including @limiter.exempt ones) because the middleware intercepts requests
# before FastAPI resolves the route, making exempt/decorator checks unreliable.
# With app.state.limiter alone, @limiter.limit(...) decorators on individual
# routes work correctly. Auth routes (login/OTP) apply their own 200/min limits.
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.rate_limit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Global handler: turn unhandled DB / connection errors into 503 instead of 500
from sqlalchemy.exc import (
    OperationalError,
    DisconnectionError,
    TimeoutError as SATimeoutError,
)

import logging as _logging

# Quiet the SQLAlchemy stack-trace dumps that swamp the console when the DB
# is unreachable. Each failed request would otherwise log a ~30-line trace
# repeating the same root cause (DNS failure, refused connection, etc.).
# We still surface a one-line ERROR per failure so the issue stays visible.
_logging.getLogger("sqlalchemy.pool").setLevel(_logging.ERROR)
_logging.getLogger("sqlalchemy.engine").setLevel(_logging.WARNING)

# Phase 3.21 shadow observability — the entitlement shadow logger emits at INFO,
# but the root logger defaults to WARNING and nothing here raises it, so its
# records would be dropped before reaching stdout -> Cloud Logging. This is the
# single, isolated fix required for the traffic-bearing shadow window: it sets
# ONLY this logger to INFO with its own stdout handler (propagate=False so it
# neither depends on nor disturbs any other logging). It changes no entitlement
# decision and turns on no enforcement.
import sys as _sys
_shadow_logger = _logging.getLogger("zoiko.entitlement.shadow")
_shadow_logger.setLevel(_logging.INFO)
if not _shadow_logger.handlers:
    _shadow_handler = _logging.StreamHandler(_sys.stdout)
    _shadow_handler.setFormatter(_logging.Formatter("%(message)s"))
    _shadow_logger.addHandler(_shadow_handler)
    _shadow_logger.propagate = False


# When the DB is unreachable, every `logger.exception(...)` call in the
# codebase (~18 sites: commercial bootstrap, audit governance, dunning,
# trial lifecycle, alerts, workspace, kairos, etc.) dumps a 30-line trace
# whose root is always the same `psycopg2.OperationalError`. Strip the
# traceback from those records so the console keeps one line per failure
# instead of a wall of red. Real bugs (non-DB exceptions) keep their full
# traceback so we don't lose debuggability.
class _DBUnavailableTracebackFilter(_logging.Filter):
    _DB_HINTS = (
        "could not translate host name",
        "could not connect to server",
        "Connection refused",
        "Name or service not known",
        "Tenant or user not found",
        "server closed the connection unexpectedly",
    )

    def filter(self, record: _logging.LogRecord) -> bool:
        if not record.exc_info:
            return True
        _, exc, _tb = record.exc_info
        cause = exc
        msgs: list[str] = []
        while cause is not None:
            msgs.append(str(cause))
            cause = cause.__cause__ or cause.__context__
        joined = " | ".join(msgs)
        if any(hint in joined for hint in self._DB_HINTS):
            # Replace the traceback with a short, one-line marker. Standard
            # `logger.exception` still emits its message; the suppressed
            # traceback is summarised inline. Collapse whitespace because
            # SQLAlchemy embeds "\n\n(Background ...)" in its exc.__str__.
            record.exc_info = None
            record.exc_text = None
            raw = msgs[-1] if msgs else "DB unavailable"
            short = " ".join(raw.split())[:160]
            record.msg = f"{record.getMessage()} [DB unavailable: {short}]"
            record.args = ()
        return True


# Attach to every existing handler on the root logger so records that
# propagate up from child loggers (where `logger.exception` was called)
# are filtered before being emitted. We also attach to the root logger
# itself for records that originate there.
_db_filter = _DBUnavailableTracebackFilter()
_logging.getLogger().addFilter(_db_filter)
for _h in _logging.getLogger().handlers:
    _h.addFilter(_db_filter)

# Throttle: only log the first DB failure per minute so a flapping connection
# (e.g. IPv6 route blip) doesn't fill the terminal with identical errors.
_DB_ERR_LOG_INTERVAL_S = 60.0
_db_err_logger = _logging.getLogger("zoikotime.db")
_db_err_last_logged = {"ts": 0.0, "msg": ""}


def _log_db_error_once(exc: Exception) -> None:
    import time as _t
    msg = str(exc).strip().splitlines()[0][:200]
    now = _t.monotonic()
    if msg == _db_err_last_logged["msg"] and (now - _db_err_last_logged["ts"]) < _DB_ERR_LOG_INTERVAL_S:
        return
    _db_err_last_logged["ts"] = now
    _db_err_last_logged["msg"] = msg
    _db_err_logger.error("DB unavailable -> 503 (suppressing duplicates for %ds): %s",
                         int(_DB_ERR_LOG_INTERVAL_S), msg)


@app.exception_handler(OperationalError)
@app.exception_handler(DisconnectionError)
@app.exception_handler(SATimeoutError)
async def db_error_handler(request: Request, exc: Exception):
    _log_db_error_once(exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please try again."},
    )


# Long-cache immutable static assets (uploaded screenshots, org logos,
# hashed JS/CSS bundles) so the browser doesn't re-download them on every
# dashboard refresh.
@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/leave-attachments/") or path.endswith("/attachment"):
        # Leave attachments are medical / personal records. `public` here put
        # them in shared proxy caches; they must never be cached outside the
        # authenticated response itself.
        response.headers["Cache-Control"] = "private, no-store"
    elif (
        path.startswith("/uploads/")
        or path.startswith("/static/org/")
    ):
        response.headers.setdefault("Cache-Control", "public, max-age=86400, immutable")
    elif path.startswith("/assets/"):
        # Vite emits hashed filenames, so these can be cached forever.
        response.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
    return response


# SPA / API path-collision guard.
#
# A handful of frontend React Router paths collide with backend API routes
# that live at the same path (e.g. /settings, /leave, /profile). Without a
# guard, refreshing the page at one of those URLs returns the raw API JSON
# instead of the SPA shell because FastAPI matches the API route first.
#
# This middleware intercepts the collision only when the browser is navigating
# directly (Accept: text/html). The frontend's `fetch()` calls don't include
# text/html in Accept, so they continue to hit the API exactly as before.
_SPA_API_COLLISION_PATHS = frozenset(
    {
        "/settings",
        "/settings/",
        "/leave",
        "/leave/",
        "/profile",
        "/profile/",
        "/onboarding",
        "/onboarding/",
    }
)


@app.middleware("http")
async def spa_collision_guard(request: Request, call_next):
    if request.method == "GET" and request.url.path in _SPA_API_COLLISION_PATHS:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            index_path = os.path.join("static", "index.html")
            if os.path.isfile(index_path):
                return FileResponse(index_path)
    return await call_next(request)


# Lightweight request-timing middleware. Adds an `X-Process-Time-Ms` response
# header on every request and logs any request slower than SLOW_REQUEST_MS
# (default 200 ms) so slow endpoints can be identified from the logs without
# any external APM. Registered last → outermost → measures total handling time.
# Purely observational: it never alters status, body, or behaviour.
_perf_logger = _logging.getLogger("zoikotime.perf")
_SLOW_REQUEST_MS = float(os.getenv("SLOW_REQUEST_MS", "200"))


@app.middleware("http")
async def request_timing(request: Request, call_next):
    import time as _t
    _start = _t.perf_counter()
    response = await call_next(request)
    _elapsed_ms = (_t.perf_counter() - _start) * 1000.0
    response.headers["X-Process-Time-Ms"] = f"{_elapsed_ms:.1f}"
    if _elapsed_ms >= _SLOW_REQUEST_MS:
        _perf_logger.warning(
            "SLOW %.0fms %s %s", _elapsed_ms, request.method, request.url.path
        )
    return response


# ----------------------------------------------
# WEBSOCKET
# ----------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None):
    # Reject unauthenticated connections before they join the broadcast pool.
    _ws_token = token or websocket.query_params.get("token")
    if not _ws_token or not decode_access_token(_ws_token):
        await websocket.close(code=1008)
        return

    # Resolve organization_id from JWT so the socket is placed in the correct
    # org room — enabling server-side tenant isolation on broadcast events.
    #
    # The same pass also enforces the account and organization gates. A socket
    # is a long-lived read channel that was previously admitted on a valid
    # signature alone, so neither a revoked account nor a suspended tenant was
    # ever disconnected from it. `_ws_denied` is checked after the lookup so a
    # transient DB failure keeps today's behaviour (connect) rather than
    # dropping every socket.
    organization_id: Optional[int] = None
    _ws_denied = False
    try:
        payload = decode_access_token(_ws_token)
        if payload:
            user_id = payload.get("user_id")
            if user_id:
                db = SessionLocal()
                try:
                    ws_user = db.query(models.User).filter(models.User.id == user_id).first()
                    if ws_user:
                        organization_id = ws_user.organization_id
                        if not ws_user.is_active:
                            _ws_denied = True
                        elif payload.get("impersonated_by") is None and \
                                org_lifecycle.is_suspended(db, organization_id):
                            _ws_denied = True
                finally:
                    db.close()
    except Exception:
        pass

    if _ws_denied:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, organization_id)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send ping to check if client is still alive
                try:
                    await websocket.send_text('{"type":"ping"}')
                except Exception:
                    manager.disconnect(websocket, organization_id)
                    return
                continue
            try:
                parsed = json.loads(data)
                if parsed.get("type") == "pong":
                    continue
                response = {
                    "type": "update",
                    "userId": parsed.get("userId"),
                    "status": "unknown",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                t = parsed.get("type", "")
                if t == "clock_in":
                    response["status"] = "clocked_in"
                elif t == "clock_out":
                    response["status"] = "clocked_out"
                elif t == "break":
                    response["status"] = "on_break"
                elif t == "resume":
                    response["status"] = "working"
                # Only forward status events to authenticated, org-scoped connections.
                # Unauthenticated clients must not receive any business data.
                if organization_id is not None:
                    await manager.broadcast_to_org(organization_id, json.dumps(response))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket, organization_id)
    except Exception:
        manager.disconnect(websocket, organization_id)


# ----------------------------------------------
# ROUTES
# ----------------------------------------------
app.include_router(auth_router)
app.include_router(auth_sso_router)
app.include_router(employees_router)
app.include_router(leave_router, prefix="/leave")
app.include_router(screenshots_router, prefix="/api")
app.include_router(intelligence_router, prefix="/api")
app.include_router(tracking_router)  # /tracking/status, /tracking/screenshot
app.include_router(desktop_router)   # /desktop/latest, /desktop/download/{platform}
app.include_router(
    browser_router
)  # /browser/heartbeat, /browser/event, /browser/live, /browser/history, /browser/summary
app.include_router(
    wec_router, prefix="/api"
)  # /api/break-log, /api/evidence, /api/wec/*
app.include_router(events_router, prefix="/api")  # /api/events
app.include_router(settings_router)  # /settings, /settings/update
app.include_router(breaks_router)  # /break/start, /break/resume
app.include_router(risk_router)  # /risk-alert, /risk-alert/history
app.include_router(org_router)  # /org/settings, /org/logo, /org/teams
app.include_router(payroll_router)  # /payroll/configs, /payroll/runs, /payroll/summary
app.include_router(ai_router)  # /ai/summary/{user_id}, /ai/summary-weekly/{user_id}
app.include_router(pow_wec_router)  # /pow/records, /pow/dashboard
app.include_router(pow_fraud_router)  # /pow/fraud-events
app.include_router(reports_router)  # /reports/* admin reports section
app.include_router(holidays_router)  # /holidays CRUD + CSV import
app.include_router(app_review_router, prefix="/api")  # /api/app-review/*
app.include_router(activity_log_router, prefix="/api")  # /api/activity-log/timeline + /summary
app.include_router(schedule_router)  # /schedule/*
app.include_router(profile_router)   # /profile/me, /preferences, /password, /sessions, etc.
app.include_router(my_data_router)   # /my-data/* — capture-config, who-viewed, corrections, exports, dsar
app.include_router(notifications_router)  # /notifications/* — preferences, history, test
app.include_router(admin_notifications_router)  # /admin/notifications/* — org-wide settings, test, event log
app.include_router(public_notifications_router)  # /notifications/unsubscribe (public, no-auth, token-guarded)
app.include_router(help_router)  # /worker/help/* — home, tickets, escalation, kairos placeholder
app.include_router(worker_settings_router)  # /worker/settings/*, /worker/calendar-connections/*
app.include_router(eco_day_router)  # /api/eco/day — GV-17 read surface (WF-ECO-03/04)
app.include_router(billing_router)  # /api/admin/billing/* — plans, subscriptions, invoices, contacts, tax, audit
app.include_router(kairos_router)   # /api/v1/kairos/* — chat, sessions, history, mail (in-process port of legacy KAIROS-BE Node service)
app.include_router(workspace_router)  # /api/workspace/* — Roles, Policies, Billing summary, Security, Integrations, Audit (Workspace Settings spec)
app.include_router(audit_governance_router)  # /api/workspace/audit/* — full governance center (policy history, access events, exports, evidence, legal holds, retention)
app.include_router(security_center_router)   # /api/workspace/security-center/* — seven-tab Security page (mfa overview, password policy, sessions policy, SSO, trusted domains, exceptions, break-glass, events, evidence export)
app.include_router(super_admin_router)  # /api/super-admin/* — platform-ops portal (gated to SUPER_ADMIN_EMAILS)

# Commercial Activation System v1 routers:
#   POST   /api/v1/signup
#   POST   /api/v1/email-verification/verify
#   POST   /api/v1/email-verification/resend
#   GET    /api/v1/entitlements/current
#   GET    /api/v1/entitlements/by-org/{organization_id}
#   POST   /api/v1/leads
#   POST   /api/v1/billing/checkout-session
#   POST   /api/v1/billing/webhooks/{provider}
#   GET    /api/v1/onboarding/checklist
#   POST   /api/v1/onboarding/milestone/{milestone}
#   GET    /api/v1/consent/me
#   POST   /api/v1/consent/marketing/grant
#   POST   /api/v1/consent/marketing/withdraw
#   /api/v1/admin/commercial/* — tenant search, manual review, fraud, demo/sandbox/enterprise tenant creation, events viewer
commercial_install_routers(app)

# Offboarding & Former-Worker Lifecycle router:
#   POST   /api/offboarding/cases
#   GET    /api/offboarding/cases
#   GET    /api/offboarding/cases/{case_id}
#   PATCH  /api/offboarding/cases/{case_id}
#   POST   /api/offboarding/cases/{case_id}/{submit|approve|cancel|reverse}
#   POST   /api/offboarding/cases/{case_id}/{begin|close-access|final-review|
#          close-payroll|close-billing|review-legal-hold|setup-former-access|
#          generate-certificate|complete|legal-hold|blockers|waivers|unblock}
#   GET    /api/offboarding/cases/{case_id}/{readiness|timeline|audit|blockers}
offboarding_install_router(app)

# V1 enterprise route aliases
app.include_router(employees_router, prefix="/v1/admin/users", tags=["V1 Admin"])


# ----------------------------------------------
# HEALTH
# ----------------------------------------------
@app.get("/api", tags=["Health"])
def home():
    return {
        "message": "Temveris API v2.0 - Workforce Assurance Platform",
        "status": "operational",
    }


@app.get("/health", tags=["Health"])
@limiter.exempt
def health(request: Request):
    return {
        "status": "healthy",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------
# LANGUAGE DETECTION  (IP → country → language)
# ----------------------------------------------
COUNTRY_LANG_MAP = {
    "IN": "hi",
    "US": "en",
    "GB": "en",
    "ES": "es",
    "MX": "es",
    "FR": "fr",
    "DE": "de",
    "IT": "it",
    "PT": "pt",
    "BR": "pt",
    "JP": "ja",
    "KR": "ko",
    "CN": "zh",
    "TW": "zh-TW",
    "RU": "ru",
    "SA": "ar",
    "AE": "ar",
    "EG": "ar",
    "ID": "id",
    "TR": "tr",
    "PL": "pl",
    "NL": "nl",
    "SE": "sv",
    "NO": "no",
    "DK": "da",
    "FI": "fi",
    "GR": "el",
    "RO": "ro",
    "UA": "uk",
    "TH": "th",
    "VN": "vi",
    "PH": "tl",
    "MY": "ms",
    "BD": "bn",
    "PK": "ur",
    "LK": "si",
    "NP": "ne",
    "MM": "my",
    "KH": "km",
    "LA": "lo",
    "GE": "ka",
    "AM": "hy",
    "AZ": "az",
    "KZ": "kk",
    "UZ": "uz",
    "IL": "he",
    "IR": "fa",
    "AF": "ps",
    "ET": "am",
    "KE": "sw",
    "NG": "ha",
    "ZA": "af",
    "CZ": "cs",
    "SK": "sk",
    "HU": "hu",
    "BG": "bg",
    "RS": "sr",
    "HR": "hr",
    "SI": "sl",
    "BA": "bs",
    "MK": "mk",
    "AL": "sq",
    "LT": "lt",
    "LV": "lv",
    "EE": "et",
    "IE": "ga",
    "AT": "de",
    "CH": "de",
    "BE": "nl",
    "LU": "lb",
    "MT": "mt",
    "CY": "el",
    "NZ": "en",
    "AU": "en",
    "CA": "en",
    "SG": "en",
    "HK": "zh",
    "MN": "mn",
    "CO": "es",
    "AR": "es",
    "CL": "es",
    "PE": "es",
    "VE": "es",
    "EC": "es",
}


@app.get("/detect-language", tags=["Language"])
def detect_language(request: Request):
    """Detect user's language from their IP address using free geolocation API.

    Defined as a SYNC handler on purpose: it makes blocking `requests.get` calls
    to external geo-IP services (up to 4–8s on the fallback path). FastAPI runs
    sync handlers in its threadpool, so those blocking calls no longer freeze the
    event loop (and every other concurrent request / WebSocket) the way they did
    when this was `async def`.
    """
    # Get client IP
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.headers.get("x-real-ip", "")
    if not client_ip:
        client_ip = request.client.host if request.client else ""

    country = "US"
    detected_lang = "en"

    if client_ip and client_ip not in ("127.0.0.1", "::1", "localhost"):
        import requests as req

        try:
            resp = req.get(
                f"http://ip-api.com/json/{client_ip}?fields=countryCode", timeout=4
            )
            if resp.status_code == 200:
                data = resp.json()
                country = data.get("countryCode", "US")
                detected_lang = COUNTRY_LANG_MAP.get(country, "en")
        except Exception:
            try:
                resp = req.get(f"https://ipapi.co/{client_ip}/json/", timeout=4)
                if resp.status_code == 200:
                    data = resp.json()
                    country = data.get("country_code", "US")
                    detected_lang = COUNTRY_LANG_MAP.get(country, "en")
            except Exception:
                pass

    return {
        "country": country,
        "language": detected_lang,
        "ip": client_ip,
    }


# ----------------------------------------------
# DESKTOP APP DOWNLOAD
# ----------------------------------------------
@app.get("/download/app", tags=["Download"], include_in_schema=True)
def download_desktop_app():
    url = os.getenv("APP_DOWNLOAD_URL", "")
    if not url:
        return JSONResponse(
            status_code=404,
            content={
                "error": "Desktop app download not configured yet.",
                "help": "Set APP_DOWNLOAD_URL in your backend .env to a hosted .exe URL.",
            },
        )
    return RedirectResponse(url=url, status_code=302)


# ──────────────────────────────────────────────
# EMAIL DIAGNOSTICS  (updated to use new email_service)
# ──────────────────────────────────────────────
@app.get("/api/email-health", tags=["Diagnostics"])
def email_health():
    """
    GET /api/email-health
    Returns which email transports are configured and reachable.
    No email is sent — safe to call anytime.

    Use this FIRST to check your config before sending test emails.
    """
    from app.email_service import email_health_check

    return email_health_check()


@app.get("/api/email-test", tags=["Diagnostics"])
def email_test(
    to: str = "test@example.com",
    _: models.User = Depends(require_super_admin),
):
    """
    GET /api/email-test?to=your@email.com
    Sends a real test email and returns detailed success/failure info.
    Requires super_admin role — not accessible to tenant users or unauthenticated callers.

    Steps to debug:
      1. Call /api/email-health first to check config
      2. Call /api/email-test?to=your@email.com to send a test
      3. Check email_logs.txt for detailed error traces
    """
    from app.email_service import send_test_email

    return send_test_email(to)


@app.get("/api/app-info", tags=["Download"])
def app_info():
    url = os.getenv("APP_DOWNLOAD_URL", "")
    return {
        "available": bool(url),
        "download_url": url or None,
        "platform": "windows",
        "name": "Temveris Work Tracker",
        "description": "Silent background tracker -- auto-starts with Windows, captures screenshots, monitors activity.",
        "version": "1.0.0",
    }


# ----------------------------------------------
# AUDIT LOG ENDPOINT (Governance Layer)
# ----------------------------------------------
@app.get("/api/audit-logs", tags=["Governance"])
def get_audit_logs(
    entity: str = None,
    action: str = None,
    actor_id: int = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    from sqlalchemy import or_ as _or
    q = db.query(models.AuditLog).filter(
        models.AuditLog.organization_id == current_user.organization_id
    ).order_by(models.AuditLog.created_at.desc())
    if entity:
        q = q.filter(models.AuditLog.entity == entity)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if actor_id:
        q = q.filter(models.AuditLog.actor_id == actor_id)
    # Clamp caller-supplied limit so a single request can't drag the entire
    # audit table into memory + serialization (e.g. ?limit=100000).
    logs = q.limit(min(max(limit, 1), 500)).all()
    return [
        {
            "id": l.id,
            "actor_id": l.actor_id,
            "entity": l.entity,
            "action": l.action,
            "target_id": l.target_id,
            "payload": l.payload,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


# ----------------------------------------------
# OPTIONAL AUTH HELPER (for /api/track)
# ----------------------------------------------
_optional_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _get_optional_user(
    token: str = Depends(_optional_oauth2),
    db: Session = Depends(get_db),
):
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    return (
        db.query(models.User).filter(models.User.id == payload.get("user_id")).first()
    )


# ----------------------------------------------
# TRACK ACTIVITY  (Desktop tracker -> backend -- legacy multipart endpoint)
# ----------------------------------------------
@app.post("/api/track", tags=["Activity"])
async def track_activity(
    file: UploadFile = File(...),
    timestamp: str = Form(...),
    app_name: str = Form(...),
    window_title: str = Form(None),
    mouse_clicks: int = Form(0),
    key_presses: int = Form(0),
    idle_seconds: int = Form(0),
    db: Session = Depends(get_db),
    current_user=Depends(_get_optional_user),
):
    # Offboarding enforcement gate (§2): a worker whose access has been closed
    # must never produce new tracking data — decided server-side, not by client
    # logout. Anonymous posts (no resolved user) are left to the existing path.
    if current_user is not None:
        from app.offboarding import enforcement as _offb_enforcement
        from app.offboarding.constants import TelemetryKind as _TK
        _decision = _offb_enforcement.check_and_quarantine_telemetry(
            db, worker_id=current_user.id, telemetry_kind=_TK.ACTIVITY.value,
            payload_meta={"app_name": app_name, "window_title": window_title,
                          "mouse_clicks": mouse_clicks, "key_presses": key_presses,
                          "idle_seconds": idle_seconds, "client_ts": timestamp},
        )
        if not _decision.allowed:
            return JSONResponse(
                status_code=403,
                content={"error": "tracking_stopped", "reason": _decision.reason,
                         "message": "Tracking is disabled for this account "
                                    "(offboarding access closure)."},
            )

    # ── Upload validation (SEC-2) ──────────────────────────────────────────
    _ALLOWED_CT = {"image/png", "image/jpeg", "image/webp"}
    _MAX_UPLOAD_BYTES = 15 * 1024 * 1024
    _ct = (file.content_type or "").split(";")[0].strip().lower()
    if _ct not in _ALLOWED_CT:
        return JSONResponse(status_code=415, content={
            "error": "unsupported_media_type",
            "message": "Only image/png, image/jpeg, image/webp uploads are accepted.",
        })
    try:
        file.file.seek(0, os.SEEK_END)
        _size = file.file.tell()
        file.file.seek(0)
        _head = file.file.read(16)
        file.file.seek(0)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_upload"})
    if _size > _MAX_UPLOAD_BYTES:
        return JSONResponse(status_code=413, content={
            "error": "payload_too_large",
            "message": "Screenshot exceeds the 15 MB limit.",
        })
    _is_image = (
        _head.startswith(b"\x89PNG\r\n\x1a\n")
        or _head.startswith(b"\xff\xd8\xff")
        or (_head[:4] == b"RIFF" and _head[8:12] == b"WEBP")
    )
    if not _is_image:
        return JSONResponse(status_code=415, content={
            "error": "unsupported_media_type",
            "message": "Upload is not a valid PNG/JPEG/WebP image.",
        })
    _base = os.path.basename(file.filename or "shot")
    _safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in _base)[:128] or "shot"
    org_prefix = f"org_{current_user.organization_id}/" if current_user and current_user.organization_id else ""
    filename = f"{org_prefix}{int(datetime.now().timestamp()*1000)}_{_safe}"
    from app.storage import put_upload_file, SCREENSHOTS

    is_active = not (mouse_clicks == 0 and key_presses == 0)

    # Compute productivity score
    prod_score = 0.0
    if mouse_clicks > 0 or key_presses > 0:
        activity_proxy = (mouse_clicks * 2) + (key_presses * 1)
        prod_score = round(min(1.0, activity_proxy / max(300, activity_proxy)), 4)

    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.now(timezone.utc)

    # This is the highest-volume write path in the app (every tracker screenshot,
    # every clocked-in worker). The screenshot upload (`put_upload_file` → Supabase
    # HTTP PUT / disk write) AND the SQLAlchemy commit are BLOCKING. When this ran
    # inline in `async def` they froze the event loop — and therefore every other
    # concurrent request and WebSocket — for the full upload+commit duration.
    # We move all of it onto a worker thread; the async broadcast stays on the loop.
    def _persist_activity():
        file_url = put_upload_file(SCREENSHOTS, filename, file)

        # Find active session to link
        session_id = None
        if current_user:
            from sqlalchemy import and_

            sess = (
                db.query(models.ClockSession)
                .filter(
                    and_(
                        models.ClockSession.user_id == current_user.id,
                        models.ClockSession.clock_out_time == None,
                    )
                )
                .first()
            )
            if sess:
                session_id = sess.id

        # `thumbnail` mirrors `file_path` — both point at the same Supabase object
        # (or local /uploads/ file). Screenshots router resolves either to a
        # signed URL at read time. No base64 blobs are stored in the DB anymore.
        activity = models.Activity(
            user_id=current_user.id if current_user else None,
            organization_id=current_user.organization_id if current_user else None,
            session_id=session_id,
            timestamp=ts,
            app_name=app_name,
            window_title=window_title,
            file_path=file_url,
            mouse_clicks=mouse_clicks,
            key_presses=key_presses,
            idle_seconds=idle_seconds,
            productivity_score=prod_score,
            is_active=is_active,
            thumbnail=file_url or None,
        )
        db.add(activity)

        # Emit event (Outbox pattern)
        db.add(
            models.OutboxEvent(
                event_type="ActivityTracked",
                aggregate_id=str(current_user.id) if current_user else "anon",
                payload={
                    "app_name": app_name,
                    "mouse_clicks": mouse_clicks,
                    "key_presses": key_presses,
                    "is_active": is_active,
                    "idle_seconds": idle_seconds,
                },
                status="pending",
            )
        )

        db.commit()
        db.refresh(activity)
        return activity.id, activity.user_id

    activity_id, activity_user_id = await asyncio.to_thread(_persist_activity)

    if current_user and current_user.organization_id:
        await manager.broadcast_to_org(
            current_user.organization_id,
            json.dumps(
                {
                    "type":            "new_screenshot",
                    "organization_id": current_user.organization_id,
                    "activity_id":     activity_id,
                    "user_id":         activity_user_id,
                    "app_name":        app_name,
                    "is_active":       is_active,
                    "timestamp":       timestamp,
                }
            ),
        )

    return {"success": True, "id": activity_id, "is_active": is_active}


# Dynamic base URL instead of hardcoded localhost
def _activity_url(file_path, request: Request = None):
    if not file_path:
        return ""
    fp = file_path.replace("\\", "/")
    if fp.startswith("/uploads/"):
        return fp
    return "/uploads/" + fp.split("/")[-1]


# ----------------------------------------------
# GET ACTIVITIES
# ----------------------------------------------
@app.get("/api/activities", tags=["Activity"])
def get_activities(
    user_id: int = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    # Hard cap the limit so a misconfigured caller can never blow up
    # response time / memory by asking for "all activities".
    limit = max(1, min(limit, 500))
    q = db.query(models.Activity).filter(
        models.Activity.organization_id == current_org.id
    )
    if user_id:
        q = q.filter(models.Activity.user_id == user_id)
    acts = q.order_by(models.Activity.id.desc()).limit(limit).all()

    return {
        "status": "success",
        "data": [
            {
                "id": a.id,
                "user_id": a.user_id,
                "timestamp": a.timestamp,
                "app_name": a.app_name,
                "window_title": a.window_title,
                "file_path": _activity_url(a.file_path),
                "mouse_clicks": a.mouse_clicks,
                "key_presses": a.key_presses,
                "idle_seconds": a.idle_seconds,
                "productivity_score": a.productivity_score,
                "is_active": a.is_active,
            }
            for a in acts
        ],
    }


# ----------------------------------------------
# APP HISTORY TIMELINE
# ----------------------------------------------
@app.get("/api/app-history", tags=["Activity"])
def get_app_history(
    user_id: int = None,
    date: str = None,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    from datetime import timedelta

    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            target_date = datetime.now(timezone.utc).date()
    else:
        target_date = datetime.now(timezone.utc).date()

    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    # Project only the six columns the rollup below reads, rather than
    # hydrating whole Activity entities (which also drags the Text
    # `window_title` and `thumbnail` columns over the wire for every tick).
    # Ordering, filters, scoping and the emitted response are unchanged.
    q = db.query(
        models.Activity.app_name,
        models.Activity.timestamp,
        models.Activity.mouse_clicks,
        models.Activity.key_presses,
        models.Activity.is_active,
        models.Activity.file_path,
    ).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp >= start,
        models.Activity.timestamp < end,
    )
    if user_id:
        q = q.filter(models.Activity.user_id == user_id)
    acts = q.order_by(models.Activity.timestamp.asc()).all()

    app_map = defaultdict(
        lambda: {
            "app_name": "",
            "total_seconds": 0,
            "mouse_clicks": 0,
            "key_presses": 0,
            "activity_type": "inactive",
            "screenshots": [],
            "first_seen": None,
            "last_seen": None,
        }
    )
    prev_map = {}

    for a in acts:
        key = a.app_name or "Unknown"
        entry = app_map[key]
        entry["app_name"] = key

        if key in prev_map:
            diff = int((a.timestamp - prev_map[key]).total_seconds())
            if diff < 300:
                entry["total_seconds"] += diff
        prev_map[key] = a.timestamp

        entry["mouse_clicks"] += a.mouse_clicks or 0
        entry["key_presses"] += a.key_presses or 0
        if a.is_active:
            entry["activity_type"] = "active"
        if entry["first_seen"] is None:
            entry["first_seen"] = a.timestamp.isoformat()
        entry["last_seen"] = a.timestamp.isoformat()

        if a.file_path:
            entry["screenshots"].append(
                {
                    "url": _activity_url(a.file_path),
                    "timestamp": a.timestamp.isoformat(),
                    "mouse_clicks": a.mouse_clicks,
                    "key_presses": a.key_presses,
                    "is_active": a.is_active,
                }
            )

    timeline = sorted(app_map.values(), key=lambda x: x["first_seen"] or "")
    return {"status": "success", "date": str(target_date), "timeline": timeline}


# ----------------------------------------------
# APP ANALYTICS (per-user breakdown + KPIs)
# ----------------------------------------------
@app.get("/api/app-analytics", tags=["Activity"])
def app_analytics(
    date: str = None,
    user_id: int = None,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """
    Per-user, per-app analytics for a single calendar day (IST).

    Time-accounting model — hybrid, gracefully degrades per user
    -------------------------------------------------------------
    Authoritative source: `ClockSession` (clock-in/out, breaks subtracted).
    For each user we pick the best-available accounting strategy:

      A. User has session(s) in window AND has activities
         → Anchor on session work_secs. Distribute active/idle and
           per-app time by activity-sample ratios. Best accuracy —
           a 9h workday accounts to exactly 9h.

      B. User has session(s) but no activities
         → All session time → idle. Surfaces "tracker offline" days
           honestly instead of silently dropping them.

      C. User has activities but no session in window
         → Fall back to timestamp-delta math (per-user gap, clamped to
           [1, 3600s]). Catches old/orphan rows whose session_id is
           missing or points outside the window. We don't drop these.

    Activity.idle_seconds is NEVER summed — it's a monotonically growing
    OS watchdog counter that wildly over-counts when summed.
    """
    from datetime import timedelta as td, timezone as tz
    # `func` and `case` are not imported at module scope; without these
    # inline imports the endpoint NameErrors on every call → 500.
    from sqlalchemy import or_, func, case

    # The organization's wall clock. This was a hardcoded +05:30, which put
    # every other tenant's "today" on the wrong calendar day.
    IST = _tz.org_timezone(db, current_org.id)
    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            target = datetime.now(IST).date()
    else:
        target = datetime.now(IST).date()

    # Local day boundary → UTC for DB query.
    # Build local midnight as a tz-aware datetime so all subsequent
    # max()/min() comparisons against ClockSession.clock_in_time (which is
    # tz-aware once coerced below) stay homogeneous — mixing naive + aware
    # raises TypeError and 500s the entire endpoint.
    start_ist = datetime.combine(target, datetime.min.time(), tzinfo=IST)
    start_utc = start_ist.astimezone(tz.utc)
    end_utc   = start_utc + td(days=1)
    now_utc   = datetime.now(tz.utc)

    # Per-sample anomaly clamps for click/key totals — defang stuck sensor
    # counters. Real users won't exceed these in a single screenshot window.
    CLICKS_CAP = 600    # ~10 cps over 60s
    KEYS_CAP   = 1500   # ~25 kps over 60s
    # Fallback time-delta clamps used in mode C (no session for the user).
    SAMPLE_GAP_MAX      = 60 * 60
    SAMPLE_GAP_FALLBACK = 30

    # ── 1. Sessions overlapping the day, grouped per user ──────────────
    sess_q = db.query(models.ClockSession).filter(
        models.ClockSession.organization_id == current_org.id,
        models.ClockSession.clock_in_time < end_utc,
        or_(
            models.ClockSession.clock_out_time.is_(None),
            models.ClockSession.clock_out_time > start_utc,
        ),
    )
    if user_id:
        sess_q = sess_q.filter(models.ClockSession.user_id == user_id)
    sessions = sess_q.all()

    # uid → list of {work_secs, break_secs, eff_start, eff_end}
    user_sessions: dict = {}
    for s in sessions:
        s_in  = s.clock_in_time
        s_out = s.clock_out_time or now_utc
        # Naive-ts safety: coerce to aware UTC before arithmetic.
        if s_in.tzinfo is None:
            s_in = s_in.replace(tzinfo=tz.utc)
        if s_out.tzinfo is None:
            s_out = s_out.replace(tzinfo=tz.utc)
        eff_start = max(s_in,  start_utc)
        eff_end   = min(s_out, end_utc)
        elapsed   = int((eff_end - eff_start).total_seconds())
        if elapsed <= 0:
            continue
        full_secs = max(1, int((s_out - s_in).total_seconds()))
        frac      = elapsed / full_secs
        break_in_window = int(round((s.break_seconds or 0) * frac))
        work_secs = max(0, elapsed - break_in_window)
        user_sessions.setdefault(s.user_id, []).append({
            "work_secs":  work_secs,
            "break_secs": break_in_window,
        })

    # ── 2. Activities for the day, aggregated per user+app ──────────────
    # Using GROUP BY reduces memory usage by 100x+ by only returning one row
    # per app/user pair instead of every individual screenshot tick.
    act_q = db.query(
        models.Activity.user_id,
        models.Activity.app_name,
        func.count(models.Activity.id).label("total_samples"),
        func.sum(case((models.Activity.is_active == True, 1), else_=0)).label("active_samples"),
        # Column names on the Activity model are mouse_clicks / key_presses;
        # the bare names `clicks`/`keys` don't exist as attributes and raise
        # AttributeError at query construction → endpoint returns 500. Keep
        # the SQL aliases as `clicks`/`keys` so r.clicks / r.keys downstream
        # still work without further changes.
        func.sum(models.Activity.mouse_clicks).label("clicks"),
        func.sum(models.Activity.key_presses).label("keys"),
        # For "Mode C" (no session), we still need an anchor timestamp to know which day to bucket into.
        func.min(models.Activity.timestamp).label("min_ts"),
        func.max(models.Activity.timestamp).label("max_ts")
    ).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp >= start_utc,
        models.Activity.timestamp <  end_utc,
    )
    if user_id:
        act_q = act_q.filter(models.Activity.user_id == user_id)

    act_rows = act_q.group_by(models.Activity.user_id, models.Activity.app_name).all()

    user_acts_agg: dict = {}
    for r in act_rows:
        if r.user_id is None:
            continue
        user_acts_agg.setdefault(r.user_id, []).append(r)

    # ── 3. Build user names map ──
    uids_seen = set(user_sessions.keys()) | set(user_acts_agg.keys())
    user_map = {}
    if uids_seen:
        users = (
            db.query(models.User)
              .filter(
                  models.User.id.in_(uids_seen),
                  models.User.organization_id == current_org.id,
              )
              .all()
        )
        user_map = {u.id: u.name or u.email or f"User {u.id}" for u in users}

    # Load App Review admin overrides once so per-app categorisation is
    # cheap and consistent with the rest of the dashboard.
    cat_overrides = _ar_load_overrides(db, current_org.id)

    # ── 4. Aggregate per user → per-user + per-app metrics ─────────────
    per_user: dict = {}
    per_app:  dict = {}

    def _user(uid):
        if uid not in per_user:
            per_user[uid] = {
                "user_id":         uid,
                "name":            user_map.get(uid, f"User {uid}"),
                "active_seconds":  0.0,
                "idle_seconds":    0.0,
                "total_seconds":   0.0,
                "break_seconds":   0,
                "total_clicks":    0,
                "total_keys":      0,
                "apps":            {},   # app → {"active", "idle", "total"}
                "active_samples":  0,
                "total_samples":   0,
                "source":          "session",  # session | delta | session-no-acts
            }
        return per_user[uid]

    def _app(name):
        if name not in per_app:
            per_app[name] = {
                "app_name":        name,
                "total_seconds":   0.0,
                "active_seconds":  0.0,
                "idle_seconds":    0.0,
                "clicks":          0,
                "keys":            0,
                "active_count":    0,
                "total_count":     0,
                "users":           set(),
            }
        return per_app[name]

    def _credit_app(uid, app, total_s, active_s, n_total, n_active, clicks, keys):
        """Common helper that books per-user + per-app aggregates."""
        idle_s = total_s - active_s
        u = _user(uid)
        ua = u["apps"].setdefault(app, {"active": 0.0, "idle": 0.0, "total": 0.0})
        ua["total"]  += total_s
        ua["active"] += active_s
        ua["idle"]   += idle_s
        ap = _app(app)
        ap["total_seconds"]  += total_s
        ap["active_seconds"] += active_s
        ap["idle_seconds"]   += idle_s
        ap["clicks"]         += clicks
        ap["keys"]           += keys
        ap["active_count"]   += n_active
        ap["total_count"]    += n_total
        ap["users"].add(uid)

    # Iterate the union of users that show up in either sessions or
    # activities so we never silently drop someone.
    for uid in uids_seen:
        sess_list = user_sessions.get(uid, [])
        agg_rows  = user_acts_agg.get(uid, [])
        u         = _user(uid)
        u["break_seconds"] += sum(s["break_secs"] for s in sess_list)
        sess_total_secs = sum(s["work_secs"] for s in sess_list)

        # ── Mode B: sessions but no activities ──
        if sess_list and not agg_rows:
            u["total_seconds"] += sess_total_secs
            u["idle_seconds"]  += sess_total_secs
            u["source"] = "session-no-acts"
            continue

        # ── Mode C: activities but no session in window ──
        if not sess_list and agg_rows:
            u["source"] = "delta"
            for r in agg_rows:
                # Use standard tracker interval as fallback (30s)
                app_total  = r.total_samples * 30
                app_active = r.active_samples * 30
                
                u["total_clicks"]  += r.clicks
                u["total_keys"]    += r.keys
                u["total_samples"] += r.total_samples
                u["total_seconds"] += app_total
                u["active_seconds"] += app_active
                u["active_samples"] += r.active_samples
                u["idle_seconds"]   += (app_total - app_active)

                app = (r.app_name or "Unknown").strip() or "Unknown"
                _credit_app(uid, app, app_total, app_active, r.total_samples, r.active_samples, r.clicks, r.keys)
            continue

        # ── Mode A: sessions + activities (best accuracy) ──
        u["total_seconds"] += sess_total_secs
        n_total_samples = sum(r.total_samples for r in agg_rows)
        u["total_samples"] += n_total_samples
        
        for r in agg_rows:
            app = (r.app_name or "Unknown").strip() or "Unknown"
            weight = r.total_samples / n_total_samples if n_total_samples else 0
            app_total_secs  = sess_total_secs * weight
            app_active_secs = app_total_secs * (r.active_samples / r.total_samples) if r.total_samples else 0
            
            _credit_app(uid, app, app_total_secs, app_active_secs, r.total_samples, r.active_samples, r.clicks, r.keys)
            
            u["total_clicks"]   += r.clicks
            u["total_keys"]     += r.keys
            u["active_samples"] += r.active_samples
            u["active_seconds"] += app_active_secs
            u["idle_seconds"]   += (app_total_secs - app_active_secs)

    # ── 4b. Unify Active/Idle with the Active Time Engine ───────────────
    # The Activity Log and the employee Dashboard read Active/Idle from
    # ActivityDailyScore — the authoritative, reconcile-worker-maintained
    # per-IST-day score (models.ActivityDailyScore). This endpoint used to
    # derive its OWN session×tick-ratio estimate instead, so the two admin
    # dashboards contradicted each other (e.g. 97% active here vs 38% there,
    # 9h idle vs 160h). Override the headline per-user Active/Idle with the
    # engine score wherever the day is engine-scored (window_count > 0), so
    # the KPI tiles + Activity Split donut + per-user cards match the Activity
    # Log byte-for-byte. `start_utc` is exactly the IST-midnight anchor the
    # engine keys `day` on. The per-app breakdown below keeps its tick-ratio
    # split (it drives the App Usage bars, a separate view). Users with no
    # engine score yet (offline / not-yet-reconciled) keep the estimate above.
    score_q = (
        db.query(
            models.ActivityDailyScore.user_id,
            models.ActivityDailyScore.active_seconds,
            models.ActivityDailyScore.idle_seconds,
        )
        .join(models.User, models.User.id == models.ActivityDailyScore.user_id)
        .filter(
            models.ActivityDailyScore.day == start_utc,
            models.ActivityDailyScore.window_count > 0,
            models.User.organization_id == current_org.id,
        )
    )
    if user_id:
        score_q = score_q.filter(models.ActivityDailyScore.user_id == user_id)
    for r in score_q.all():
        u = per_user.get(r.user_id)
        if u is None:
            continue
        u["active_seconds"] = int(r.active_seconds or 0)
        u["idle_seconds"]   = int(r.idle_seconds or 0)
        u["total_seconds"]  = u["active_seconds"] + u["idle_seconds"]
        u["source"]         = "engine"

    # ── 5. Build response ──
    # Round all seconds to ints so the frontend display is stable.
    def _ri(x): return int(round(x))

    users_list = []
    for u in sorted(per_user.values(), key=lambda x: x["active_seconds"], reverse=True):
        if u["apps"]:
            top_app = max(u["apps"], key=lambda k: u["apps"][k]["total"])
            top_app_secs = u["apps"][top_app]["total"]
        else:
            top_app, top_app_secs = "—", 0

        denom = u["active_seconds"] + u["idle_seconds"]
        prod_pct = round((u["active_seconds"] / denom) * 100) if denom else 0

        users_list.append({
            "user_id":            u["user_id"],
            "name":               u["name"],
            "active_seconds":     _ri(u["active_seconds"]),
            "idle_seconds":       _ri(u["idle_seconds"]),
            "total_seconds":      _ri(u["total_seconds"]),
            "break_seconds":      u["break_seconds"],
            "total_clicks":       u["total_clicks"],
            "total_keys":         u["total_keys"],
            "productivity":       prod_pct,
            "top_app":            top_app,
            "top_app_seconds":    _ri(top_app_secs),
            "top_app_category":   _ar_categorise(top_app, cat_overrides) if top_app != "—" else None,
            "total_samples":      u["total_samples"],
            "apps": sorted(
                [
                    {
                        "app":            k,
                        "seconds":        _ri(v["total"]),
                        "active_seconds": _ri(v["active"]),
                        "idle_seconds":   _ri(v["idle"]),
                        "category":       _ar_categorise(k, cat_overrides),
                    }
                    for k, v in u["apps"].items()
                ],
                key=lambda x: x["seconds"],
                reverse=True,
            )[:10],
        })

    apps_list = []
    for ap in sorted(per_app.values(), key=lambda x: x["total_seconds"], reverse=True):
        denom = ap["active_seconds"] + ap["idle_seconds"]
        active_pct = round((ap["active_seconds"] / denom) * 100) if denom else 0
        apps_list.append({
            "app_name":       ap["app_name"],
            "category":       _ar_categorise(ap["app_name"], cat_overrides),
            "total_seconds":  _ri(ap["total_seconds"]),
            "active_seconds": _ri(ap["active_seconds"]),
            "idle_seconds":   _ri(ap["idle_seconds"]),
            "clicks":         ap["clicks"],
            "keys":           ap["keys"],
            "active_pct":     active_pct,
            "user_count":     len(ap["users"]),
        })

    total_active = sum(u["active_seconds"] for u in per_user.values())
    total_idle   = sum(u["idle_seconds"]   for u in per_user.values())
    total_break  = sum(u["break_seconds"]  for u in per_user.values())
    total_clocked = total_active + total_idle  # session work time minus breaks
    total_clicks = sum(u["total_clicks"]   for u in per_user.values())
    total_keys   = sum(u["total_keys"]     for u in per_user.values())
    top_app_overall = apps_list[0]["app_name"] if apps_list else "—"
    denom = total_active + total_idle
    productivity_pct = round((total_active / denom) * 100) if denom else 0

    return {
        "status": "success",
        "date":   str(target),
        "kpi": {
            "total_active_seconds":  _ri(total_active),
            "total_idle_seconds":    _ri(total_idle),
            "total_seconds":         _ri(total_clocked),
            "total_break_seconds":   total_break,
            "total_clicks":          total_clicks,
            "total_keys":            total_keys,
            "top_app":               top_app_overall,
            "productivity_pct":      productivity_pct,
            "total_users":           len(per_user),
            "total_apps":            len(per_app),
        },
        "users": users_list,
        "apps":  apps_list,
    }


# ----------------------------------------------
# PRODUCTIVITY INSIGHTS
# Multi-day rollup over the Activity table, classifying each sample's
# foreground app as work / neutral / distraction and projecting that onto
# daily trend, per-app leaderboards, peak hours and a user leaderboard.
# Consumed by the admin "Productivity Insights" tab.
#
# Categorisation is delegated to app/app_review/productivity.py — the
# single source of truth shared with the App Review admin page. That
# means admin overrides set there (e.g. promoting "Slack" to productive)
# automatically flow through to this endpoint, the dashboard
# "Productive Apps %" KPI and the daily trend.
# ----------------------------------------------
from app.app_review.productivity import (
    categorise as _ar_categorise,
    load_overrides as _ar_load_overrides,
    CATEGORY_PRODUCTIVE  as _AR_PRODUCTIVE,
    CATEGORY_NEUTRAL     as _AR_NEUTRAL,
    CATEGORY_DISTRACTING as _AR_DISTRACTING,
    CATEGORY_BLOCKED     as _AR_BLOCKED,
)

# Productivity Insights speaks the legacy 3-bucket vocabulary
# (work / neutral / distraction). Map the App Review 4-bucket model down:
#     productive  → work
#     neutral     → neutral
#     distracting → distraction
#     blocked     → distraction (zero credit, surfaced on the same chart)
_AR_TO_LEGACY = {
    _AR_PRODUCTIVE:  "work",
    _AR_NEUTRAL:     "neutral",
    _AR_DISTRACTING: "distraction",
    _AR_BLOCKED:     "distraction",
}


def _classify_app(app_name: str, overrides: dict | None = None) -> str:
    """Return work / neutral / distraction using the unified App Review
    categoriser. `overrides` is an optional preloaded dict (admin overrides)
    so callers can avoid re-querying the DB per row."""
    cat = _ar_categorise(app_name, overrides)
    return _AR_TO_LEGACY.get(cat, "neutral")


@app.get("/api/productivity-insights", tags=["Activity"])
def productivity_insights(
    days: int = 7,
    user_id: int = None,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """
    Rollup productivity signal over the last `days` days (IST).

    Classifies each Activity sample's foreground app as work/neutral/distraction
    and returns the material a Productivity Insights dashboard needs:

      - kpi.productivity_score  → weighted (work=1, neutral=0.5, distraction=0)
      - daily_trend             → score + time breakdown per IST day
      - category_breakdown      → pie-chart totals
      - top_productive_apps     → bar chart of highest work-category time
      - top_distracting_apps    → bar chart of highest distraction time
      - hourly_distribution     → active seconds by hour-of-day (0–23)
      - user_leaderboard        → per-user productivity ranking
    """
    from datetime import timedelta as td, timezone as tz
    # `func` is not imported at module scope; without this inline import
    # the endpoint NameErrors on every call → 500.
    from sqlalchemy import or_, func

    IST = _tz.org_timezone(db, current_org.id)   # the org's clock, not +05:30
    days = max(1, min(90, days or 7))  # hard cap prevents abuse
    today_ist = datetime.now(IST).date()
    start_ist_date = today_ist - td(days=days - 1)
    # Build IST window edges as tz-aware datetimes so every comparison /
    # max()/min() with ClockSession.clock_in_time below stays homogeneous.
    # Mixing naive + aware datetimes raises TypeError and 500s the endpoint.
    start_ist = datetime.combine(start_ist_date, datetime.min.time(), tzinfo=IST)
    end_ist   = datetime.combine(today_ist,      datetime.max.time(), tzinfo=IST)
    start_utc = start_ist.astimezone(tz.utc)
    end_utc   = end_ist.astimezone(tz.utc)
    now_utc   = datetime.now(tz.utc)

    # ── 1. Sessions overlapping the window ────────────────────────────────
    # Same accounting model as /api/app-analytics: anchor user-level totals
    # on ClockSession (authoritative), then DISTRIBUTE the work-second total
    # across categories / apps / hours by sample-count ratios. This keeps a
    # 9-hour workday accounting to ~9h regardless of the configured
    # screenshot interval (30s vs 30min) or any dropped samples.
    # Hybrid model — same as /api/app-analytics. Per user we pick the most
    # accurate accounting strategy that the data supports:
    #   A. session(s) + activities    → distribute session work_secs by
    #                                    activity-sample ratios (most accurate)
    #   B. session(s) only            → bucket session time as neutral
    #   C. activities only            → fall back to timestamp-delta math
    SAMPLE_GAP_MAX      = 60 * 60
    SAMPLE_GAP_FALLBACK = 30

    sess_q = db.query(models.ClockSession).filter(
        models.ClockSession.organization_id == current_org.id,
        models.ClockSession.clock_in_time < end_utc,
        or_(
            models.ClockSession.clock_out_time.is_(None),
            models.ClockSession.clock_out_time > start_utc,
        ),
    )
    if user_id:
        sess_q = sess_q.filter(models.ClockSession.user_id == user_id)
    sessions = sess_q.all()

    # uid → total work_secs across all sessions in window (used in mode A/B)
    user_session_secs: dict = {}
    # uid → representative IST date for mode-B "tracker offline" bucketing
    user_session_day:  dict = {}
    for s in sessions:
        s_in  = s.clock_in_time
        s_out = s.clock_out_time or now_utc
        if s_in.tzinfo is None:
            s_in  = s_in.replace(tzinfo=tz.utc)
        if s_out.tzinfo is None:
            s_out = s_out.replace(tzinfo=tz.utc)
        eff_start = max(s_in,  start_utc)
        eff_end   = min(s_out, end_utc)
        elapsed   = int((eff_end - eff_start).total_seconds())
        if elapsed <= 0:
            continue
        full_secs = max(1, int((s_out - s_in).total_seconds()))
        frac = elapsed / full_secs
        break_in_window = int(round((s.break_seconds or 0) * frac))
        work_secs = max(0, elapsed - break_in_window)
        user_session_secs[s.user_id] = user_session_secs.get(s.user_id, 0) + work_secs
        user_session_day.setdefault(s.user_id, eff_start)

    # ── 2. Activity counts per user+app (and day/hour) ────────────────────
    # We group by user, app, and local hour to feed the trend and distribution
    # charts without loading millions of individual ticks.
    _org_tz_name = _tz.org_timezone_name(db, current_org.id)
    act_q = db.query(
        models.Activity.user_id,
        models.Activity.app_name,
        # Bucket by the organization's local date and hour. Always pass the
        # IANA name — Postgres `timezone('IST', …)` is ambiguous and may
        # resolve to Israel Standard Time or raise outright.
        func.date(func.timezone(_org_tz_name, func.timezone('UTC', models.Activity.timestamp))).label('date'),
        func.extract('hour', func.timezone(_org_tz_name, func.timezone('UTC', models.Activity.timestamp))).label('hour'),
        func.count(models.Activity.id).label("total_samples")
    ).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp >= start_utc,
        models.Activity.timestamp <= end_utc,
    )
    if user_id:
        act_q = act_q.filter(models.Activity.user_id == user_id)

    act_rows = act_q.group_by(
        models.Activity.user_id, 
        models.Activity.app_name, 
        'date', 
        'hour'
    ).all()

    # Organize by user for the distribution loop
    user_acts_agg: dict = {}
    for r in act_rows:
        if r.user_id is None: continue
        user_acts_agg.setdefault(r.user_id, []).append(r)

    # ── 3. Daily / app / user / hourly buckets ────────────────────────────
    day_stats = {}
    for d in range(days):
        key = (start_ist_date + td(days=d)).isoformat()
        day_stats[key] = {
            "date": key,
            "work": 0.0,
            "neutral": 0.0,
            "distraction": 0.0,
            "active_s": 0.0,
        }

    app_totals: dict = {}     # app → {seconds, category}
    user_stats: dict = {}     # uid → {name, work, neutral, distraction, active_s}
    hourly = [0.0] * 24

    # Load App Review admin overrides ONCE so per-sample classification is
    # cheap. Anything an admin has tagged on the App Review page (e.g.
    # "Slack productive") flows through here automatically.
    cat_overrides = _ar_load_overrides(db, current_org.id)

    # Build user name map
    uids = set(user_session_secs.keys()) | set(user_acts_agg.keys())
    user_names = {}
    if uids:
        for u in db.query(models.User).filter(models.User.id.in_(uids)).all():
            user_names[u.id] = u.name or u.email or f"User {u.id}"

    def _u(uid):
        if uid not in user_stats:
            user_stats[uid] = {
                "user_id":    uid,
                "name":       user_names.get(uid, f"User {uid}"),
                "work":       0.0,
                "neutral":    0.0,
                "distraction": 0.0,
                "active_s":   0.0,
            }
        return user_stats[uid]

    def _credit(day_str, hour, app, cat, secs, uid):
        """Book `secs` worth of activity into all the buckets."""
        if day_str in day_stats:
            day_stats[day_str][cat]        += secs
            day_stats[day_str]["active_s"] += secs
            if 0 <= hour <= 23:
                hourly[int(hour)]          += secs
        if app not in app_totals:
            app_totals[app] = {"app": app, "seconds": 0.0, "category": cat}
        app_totals[app]["seconds"] += secs
        u = _u(uid)
        u[cat]        += secs
        u["active_s"] += secs

    # ── 4. Per-user aggregation ───────────────────────────────────────────
    for uid in uids:
        agg_rows  = user_acts_agg.get(uid, [])
        sess_secs = user_session_secs.get(uid, 0)

        if sess_secs > 0 and not agg_rows:
            # Mode B: session but no activity
            day_anchor = user_session_day.get(uid, start_utc)
            ts_ist = day_anchor.astimezone(IST)
            _credit(ts_ist.date().isoformat(), ts_ist.hour, "Unknown", "neutral", sess_secs, uid)
            continue

        if not agg_rows:
            continue

        if sess_secs > 0:
            # Mode A: Distribute session time
            total_samples = sum(r.total_samples for r in agg_rows)
            for r in agg_rows:
                app = r.app_name or "Unknown"
                cat = _classify_app(app, cat_overrides)
                weight = r.total_samples / total_samples
                _credit(r.date.isoformat(), r.hour, app, cat, sess_secs * weight, uid)
            continue

        # Mode C: Activity only (fallback)
        for r in agg_rows:
            app = r.app_name or "Unknown"
            cat = _classify_app(app, cat_overrides)
            # Estimate 30s per sample
            _credit(r.date.isoformat(), r.hour, app, cat, r.total_samples * 30, uid)

    # ── Build response ─────────────────────────────────────────────────────
    def score(work, neutral, distraction):
        total = work + neutral + distraction
        if total <= 0:
            return 0
        return round(((work + 0.5 * neutral) / total) * 100)

    total_work = sum(d["work"] for d in day_stats.values())
    total_neutral = sum(d["neutral"] for d in day_stats.values())
    total_distraction = sum(d["distraction"] for d in day_stats.values())
    total_active = total_work + total_neutral + total_distraction

    # Daily trend with per-day score
    daily_trend = []
    for d in sorted(day_stats.keys()):
        row = day_stats[d]
        daily_trend.append(
            {
                "date": row["date"],
                "score": score(row["work"], row["neutral"], row["distraction"]),
                "work": row["work"],
                "neutral": row["neutral"],
                "distraction": row["distraction"],
                "active_s": row["active_s"],
            }
        )

    # Top apps by category
    work_apps = sorted(
        (a for a in app_totals.values() if a["category"] == "work"),
        key=lambda x: x["seconds"],
        reverse=True,
    )[:10]
    distraction_apps = sorted(
        (a for a in app_totals.values() if a["category"] == "distraction"),
        key=lambda x: x["seconds"],
        reverse=True,
    )[:10]
    neutral_apps = sorted(
        (a for a in app_totals.values() if a["category"] == "neutral"),
        key=lambda x: x["seconds"],
        reverse=True,
    )[:10]

    top_work = work_apps[0]["app"] if work_apps else "—"
    top_distr = distraction_apps[0]["app"] if distraction_apps else "—"

    # User leaderboard
    leaderboard = []
    for u in user_stats.values():
        leaderboard.append(
            {
                "user_id": u["user_id"],
                "name": u["name"],
                "active_s": u["active_s"],
                "work": u["work"],
                "neutral": u["neutral"],
                "distraction": u["distraction"],
                "score": score(u["work"], u["neutral"], u["distraction"]),
            }
        )
    leaderboard.sort(key=lambda x: x["score"], reverse=True)

    # Hourly distribution (return all 24 hours so the chart draws an even x-axis)
    hourly_dist = [{"hour": h, "active_s": hourly[h]} for h in range(24)]

    return {
        "status": "success",
        "date_range": {
            "from": start_ist_date.isoformat(),
            "to": today_ist.isoformat(),
            "days": days,
        },
        "kpi": {
            "productivity_score": score(total_work, total_neutral, total_distraction),
            "total_active_seconds": total_active,
            "productive_seconds": total_work,
            "neutral_seconds": total_neutral,
            "distracting_seconds": total_distraction,
            "top_productive_app": top_work,
            "top_distracting_app": top_distr,
            "total_users": len(user_stats),
            "total_apps": len(app_totals),
        },
        "daily_trend": daily_trend,
        "category_breakdown": [
            {
                "category": "work",
                "seconds": total_work,
                "pct": round((total_work / max(1, total_active)) * 100, 1),
            },
            {
                "category": "neutral",
                "seconds": total_neutral,
                "pct": round((total_neutral / max(1, total_active)) * 100, 1),
            },
            {
                "category": "distraction",
                "seconds": total_distraction,
                "pct": round((total_distraction / max(1, total_active)) * 100, 1),
            },
        ],
        "top_productive_apps": work_apps,
        "top_distracting_apps": distraction_apps,
        "top_neutral_apps": neutral_apps,
        "hourly_distribution": hourly_dist,
        "user_leaderboard": leaderboard,
    }


# ----------------------------------------------
# ACTIVITY HEATMAP
# ----------------------------------------------
@app.get("/api/activity-heatmap", tags=["Activity"])
def activity_heatmap(
    user_id: int = None,
    days: int = 30,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tzinfo=timezone.utc)

    # Project ONLY the two columns the rollup below reads. This used to select
    # whole Activity entities — every mapped column, including the Text
    # `window_title` / `file_path` / `thumbnail` fields — and hydrate them as ORM
    # objects, for a `days`-wide window (the admin dashboard asks for 7). The
    # aggregation, response shape, org scoping, user filter and date range are
    # all unchanged; only the columns fetched over the wire are narrower.
    q = db.query(models.Activity.timestamp, models.Activity.is_active).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp >= start,
    )
    if user_id:
        q = q.filter(models.Activity.user_id == user_id)
    acts = q.all()

    heatmap: dict = {}
    for a in acts:
        day = a.timestamp.strftime("%Y-%m-%d")
        hour = a.timestamp.hour
        key = f"{day}_{hour}"
        if key not in heatmap:
            heatmap[key] = {"date": day, "hour": hour, "count": 0, "active": 0}
        heatmap[key]["count"] += 1
        if a.is_active:
            heatmap[key]["active"] += 1

    return {"status": "success", "heatmap": list(heatmap.values())}


# ----------------------------------------------
# STATIC FILES + SPA CATCH-ALL (must be LAST)
# ----------------------------------------------
ORG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tracker-data", "org"
)
os.makedirs(ORG_DIR, exist_ok=True)

# Storage-aware file serving. In local mode we mount StaticFiles; in supabase
# mode the same URLs (/uploads/<f>, /leave-attachments/<f>, /static/org/<f>)
# 302-redirect to a short-lived signed URL so stored paths never need to change.
from app.storage import (
    backend_name as _storage_backend,
    signed_url as _signed_url,
    SCREENSHOTS as _SCR,
    ATTACHMENTS as _ATT,
    ORG_LOGOS as _ORG,
)

if _storage_backend() == "supabase":

    def _redirect_to_signed(bucket, filename: str):
        url = _signed_url(bucket, filename)
        if not url:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return RedirectResponse(url=url, status_code=302)

    @app.get("/uploads/{filename:path}", include_in_schema=False)
    def _serve_upload(filename: str):
        return _redirect_to_signed(_SCR, filename)

    @app.get("/leave-attachments/{filename:path}", include_in_schema=False)
    def _serve_attachment(filename: str, _user=Depends(get_current_user)):
        return _redirect_to_signed(_ATT, filename)

    @app.get("/static/org/{filename:path}", include_in_schema=False)
    def _serve_org_logo(filename: str):
        return _redirect_to_signed(_ORG, filename)

else:
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

    # Leave attachments are NOT a StaticFiles mount.
    #
    # They were, and that served every stored medical certificate, hospital
    # letter and birth certificate to anyone who had the URL — no token, no
    # tenant check. The bucket is declared `public=False` in app/storage; this
    # mount was the one place that did not honour it.
    #
    # A logged-in check is the most this path can do: it knows a filename, not
    # which leave request owns it. The per-request authorization (owner, or
    # admin/hr in the same organization) lives on GET /leave/{id}/attachment,
    # which is what the Apply Leave and Manage Leave screens now call. This
    # route stays only so previously stored `attachment_path` values and any
    # older client still resolve.
    @app.get("/leave-attachments/{filename:path}", include_in_schema=False)
    def _serve_attachment_local(filename: str, _user=Depends(get_current_user)):
        from app.storage import local_root as _local_root
        root = os.path.realpath(_local_root(_ATT))
        full = os.path.realpath(os.path.join(root, filename))
        # commonpath raises ValueError when the two land on different Windows
        # drives — an absolute `filename` does exactly that, so treat it the
        # same as any other path that escapes the root.
        try:
            contained = os.path.commonpath([root, full]) == root
        except ValueError:
            contained = False
        if not contained or not os.path.isfile(full):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return FileResponse(full, headers={"Cache-Control": "private, no-store"})

    app.mount("/static/org", StaticFiles(directory=ORG_DIR), name="org-static")

# Public, unauthenticated mount for branding assets referenced from outbound
# email HTML (logo, etc.). Email clients hot-link these — they MUST be reachable
# without auth and over HTTPS in production. Update EMAIL_LOGO_URL in the env
# to point at this URL on the deployed backend (e.g. https://api.temveris.example/
# email-assets/temveris_logo.png) so Gmail/Outlook can fetch it.
_EMAIL_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "email_assets")
if os.path.isdir(_EMAIL_ASSETS_DIR):
    app.mount("/email-assets", StaticFiles(directory=_EMAIL_ASSETS_DIR), name="email-assets")

# Serve static assets (JS, CSS, images from React build)
if os.path.isdir(os.path.join("static", "assets")):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join("static", "assets")),
        name="static-assets",
    )


# SPA catch-all: serve index.html for ALL non-API routes
# This makes React Router work properly (login, dashboard, admin, etc.)
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    # If the requested file exists in static/, serve it directly
    static_file = os.path.join("static", full_path)
    if full_path and os.path.isfile(static_file):
        return FileResponse(static_file)
    # Otherwise serve index.html -- React Router handles the route client-side
    index_path = os.path.join("static", "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return {"detail": "Not Found"}
