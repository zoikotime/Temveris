"""
Temveris – Database Models
Operational Truth Engine: immutable + auditable records
"""

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Text, Float, Enum, JSON, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
from database.database import Base
from datetime import datetime
import enum


# ──────────────────────────────────────────────
# ENUMS
# ──────────────────────────────────────────────
class LeaveStatus(str, enum.Enum):
    DRAFT        = "draft"
    SUBMITTED    = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED     = "approved"
    REJECTED     = "rejected"
    LOCKED       = "locked_for_payroll"


class EventStatus(str, enum.Enum):
    PENDING   = "pending"
    PUBLISHED = "published"
    FAILED    = "failed"


class ValidationStatus(str, enum.Enum):
    PENDING   = "pending"
    VALIDATED = "validated"
    WARNING   = "warning"
    FLAGGED   = "flagged"


class FraudSeverity(str, enum.Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


# ──────────────────────────────────────────────
# USER MODEL
# ──────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id       = Column(Integer, primary_key=True, index=True)
    name     = Column(String(255), nullable=False)
    email    = Column(String(255), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=True)
    organization_id = Column(
    Integer,
    ForeignKey("organizations.id", ondelete="SET NULL"),
    nullable=True
)

    role        = Column(String(50),  default="employee", index=True)
    department  = Column(String(100), nullable=True)
    designation = Column(String(100), nullable=True)
    phone       = Column(String(30),  nullable=True)
    employment_type = Column(String(50), default="full_time")
    team        = Column(String(100), nullable=True)
    employee_id = Column(String(50),  nullable=True, unique=True, index=True)  # custom employee code (e.g. EMP001)

    # Profile picture stored as data URL (base64) — keeps deploys simple,
    # no shared file storage needed. Trimmed client-side before upload.
    profile_picture = Column(Text, nullable=True)

    is_active       = Column(Boolean, default=True)
    is_invited      = Column(Boolean, default=False)
    is_password_set = Column(Boolean, default=False)
    # Authoritative billing classification — see app/billing/worker_state.py.
    # INVITED_PENDING | ACTIVE_BILLABLE | SUSPENDED_BILLABLE | DEACTIVATED |
    # TERMINATED | ARCHIVED | MIGRATION_REVIEW.
    # is_active/is_invited must never be used to decide billability: is_invited
    # is not cleared on acceptance, and is_active=False conflates suspended
    # (billable) with deactivated/terminated (not billable).
    billing_state   = Column(String(24), nullable=True, index=True)

    default_clock_in_time      = Column(String(5), nullable=True)
    default_clock_in_permanent = Column(Boolean, default=True)
    default_clock_in_grace_min = Column(Integer, default=20)

    # Official employment start date — first working day. Drives leave
    # eligibility (`official_start_date + tenant_waiting_period_months`).
    # Nullable so existing pre-feature users keep working; the leave
    # eligibility check grandfathers null-date users as fully eligible.
    official_start_date = Column(DateTime, nullable=True, index=True)

    # Invite system – token + 24-hour expiry
    invite_token            = Column(String(255), nullable=True, index=True)
    invite_token_expires_at = Column(DateTime, nullable=True)
    invited_by_id           = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    # ── Relationships ──
    sessions       = relationship("ClockSession",  back_populates="user", cascade="all, delete-orphan")
    break_sessions = relationship("BreakSession",  back_populates="user", cascade="all, delete-orphan")
    activities     = relationship("Activity",      back_populates="user", cascade="all, delete-orphan")

    # ✅ FIX ADDED HERE
    leave_requests = relationship(
        "LeaveRequest",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="LeaveRequest.user_id"
    )

    audit_logs     = relationship("AuditLog",      back_populates="actor", foreign_keys="AuditLog.actor_id")

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


# ──────────────────────────────────────────────
# PASSWORD RESET OTP
# 4-digit one-time-code path for the Forgot Password flow.
# Kept in its own table (not on the User row) so an active employee
# invite (which lives in User.invite_token) cannot collide with a
# concurrent password reset for the same address.
# ──────────────────────────────────────────────
class PasswordResetOTP(Base):
    __tablename__ = "password_reset_otps"

    id          = Column(Integer, primary_key=True, index=True)
    # Owning employee. Nullable so an email-only lookup or a legacy row never
    # blocks the insert, but populated on every new send so resets can be
    # audited and joined back to the user.
    employee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    email       = Column(String(255), nullable=False, index=True)
    # We persist only an HMAC-SHA256 of the 4-digit code, never the code itself —
    # a DB read must not reveal a live OTP. The code is emailed once and never
    # stored in plaintext.
    otp_hash    = Column(String(255), nullable=True)
    # Legacy plaintext column — no longer written (kept nullable so old rows and
    # downgrades don't break). New rows populate otp_hash instead.
    otp_code    = Column(String(8),   nullable=True)
    expires_at  = Column(DateTime(timezone=True), nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    attempts    = Column(Integer, default=0, nullable=False) # incremented on every verify miss
    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    # Speeds up the "latest unexpired OTP for this email" lookup on every verify call.
    __table_args__ = (
        Index("ix_password_reset_otps_email_created", "email", "created_at"),
    )


# ──────────────────────────────────────────────
# CLOCK SESSION  (Operational Truth – immutable intent)
# ──────────────────────────────────────────────
class ClockSession(Base):
    __tablename__ = "clock_sessions"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="CASCADE"),  nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True,  index=True)

    clock_in_time  = Column(DateTime(timezone=True), nullable=False)
    clock_out_time = Column(DateTime(timezone=True), nullable=True)

    total_seconds = Column(Integer, default=0)
    break_seconds = Column(Integer, default=0)
    # Connectivity gaps reported by the client via POST /employees/network-pause,
    # kept SEPARATE from break_seconds. Both are subtracted from total_seconds,
    # but they mean different things: break_seconds is time the worker chose to
    # take, offline_seconds is time the tracker could not observe. Merging them
    # (the pre-2026-08 behaviour) polluted break analytics/governance with up to
    # 8h of "break" per session and made the deduction invisible in every UI.
    # Legacy rows keep their merged value in break_seconds; nothing is rewritten.
    offline_seconds = Column(Integer, default=0, server_default="0", nullable=False)
    is_active     = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user   = relationship("User",         back_populates="sessions")
    breaks = relationship("BreakSession", back_populates="session", cascade="all, delete-orphan")
    offline_intervals = relationship(
        "OfflineInterval", back_populates="session", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_clock_sessions_org_clockin", "organization_id", "clock_in_time"),
        Index("ix_clock_sessions_user_clock_in", "user_id", "clock_in_time"),
        Index("ix_clock_sessions_user_clock_out", "user_id", "clock_out_time"),
    )

    def __repr__(self):
        return f"<ClockSession user={self.user_id} active={self.is_active}>"


# ──────────────────────────────────────────────
# BREAK SESSION
# ──────────────────────────────────────────────
class BreakSession(Base):
    __tablename__ = "break_sessions"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="CASCADE"), nullable=False, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="CASCADE"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True, index=True)

    break_start   = Column(DateTime(timezone=True), nullable=False)
    break_end     = Column(DateTime(timezone=True), nullable=True)
    break_seconds = Column(Integer, default=0)
    is_active     = Column(Boolean, default=True)
    reason        = Column(Text, nullable=True)   # reason entered on Resume
    # "break" (short, default threshold 15m) or "lunch" (long, default 60m).
    # Drives the overtime pop-up in the worker dashboard and the admin alert.
    break_type    = Column(String(16), nullable=False, default="break", server_default="break")
    # Set once when the overtime alert fires so the polling loop doesn't
    # spam the worker every 30 seconds after the threshold is crossed.
    overtime_notified = Column(Boolean, nullable=False, default=False, server_default="false")

    # ── Break Governance (Break Entitlement & Overrun Provenance spec) ───────
    # Snapshot of the worker's employment type + the allowance (in minutes)
    # that applied when this break STARTED. Frozen at start so reports and
    # audit records stay stable even if workspace policy or the worker's
    # employment type changes afterwards.
    worker_type     = Column(String(16), nullable=True)
    allowed_minutes = Column(Integer,    nullable=True)
    # Lifecycle: "active" → "overdue" (allowance exceeded) → "ended".
    status          = Column(String(16), nullable=False, default="active", server_default="active")
    # First moment elapsed break time crossed the allowance (BREAK_ALLOWANCE_REACHED).
    allowance_reached_at = Column(DateTime(timezone=True), nullable=True)
    # True once this break has exceeded its allowed duration (drives RED indicator + reports).
    overrun_flag    = Column(Boolean, nullable=False, default=False, server_default="false")
    # HR escalation throttling: whether the first alert went out, the first
    # notification time, the most-recent reminder time, and the running count.
    # The monitor sends the first alert at the threshold, then a reminder
    # every 30 minutes while the break stays active.
    overrun_notification_sent = Column(Boolean, nullable=False, default=False, server_default="false")
    overrun_notified_at = Column(DateTime(timezone=True), nullable=True)
    last_alert_at   = Column(DateTime(timezone=True), nullable=True)
    alert_count     = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user    = relationship("User",         back_populates="break_sessions")
    session = relationship("ClockSession", back_populates="breaks")

    __table_args__ = (
        # Reports aggregate breaks by user over a date range.
        Index("ix_break_sessions_user_start", "user_id", "break_start"),
        # "Currently on break" = break_end IS NULL; composite keeps the
        # per-user active-break check off a full table scan.
        Index("ix_break_sessions_user_end", "user_id", "break_end"),
    )

    def __repr__(self):
        return f"<BreakSession user={self.user_id} active={self.is_active}>"


# ──────────────────────────────────────────────
# OFFLINE INTERVAL  (connectivity gap — audited deduction)
# ──────────────────────────────────────────────
class OfflineInterval(Base):
    """One client-reported connectivity gap, as an auditable row.

    Every deduction that reduces a worker's credited time gets a record here:
    what the client CLAIMED, what was actually APPLIED after clamping/capping,
    and whether it was capped. Before this table existed, /employees/network-pause
    added the claimed seconds straight onto ClockSession.break_seconds with no
    trace — so a deduction could not be explained, audited or reversed, and 73
    sessions lost up to 8h20m each with nothing to show why.

    ``ClockSession.offline_seconds`` is derived from these rows as the UNION of
    the applied windows (never a running sum) so a retrying client that re-posts
    an overlapping window cannot inflate the deduction — the same failure mode
    already fixed for BreakSession in _actual_break_seconds_for_sessions.
    """
    __tablename__ = "offline_intervals"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="CASCADE"), nullable=False, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True,  index=True)

    # The window actually applied, after clamping to the session and capping.
    offline_start = Column(DateTime(timezone=True), nullable=False)
    offline_end   = Column(DateTime(timezone=True), nullable=False)

    claimed_seconds = Column(Integer, nullable=False, default=0)  # raw client ask
    applied_seconds = Column(Integer, nullable=False, default=0)  # after clamp+cap
    capped          = Column(Boolean, nullable=False, default=False, server_default="false")
    cap_reason      = Column(String(32), nullable=True)   # single_interval | session_total

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user    = relationship("User")
    session = relationship("ClockSession", back_populates="offline_intervals")

    __table_args__ = (
        Index("ix_offline_intervals_session_start", "session_id", "offline_start"),
        Index("ix_offline_intervals_user_start", "user_id", "offline_start"),
    )

    def __repr__(self):
        return (
            f"<OfflineInterval session={self.session_id} "
            f"applied={self.applied_seconds}s capped={self.capped}>"
        )


# ──────────────────────────────────────────────
# ACTIVITY  (Screenshot + keyboard/mouse event)
# ──────────────────────────────────────────────
class Activity(Base):
    __tablename__ = "activities"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="CASCADE"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True, index=True)

    timestamp    = Column(DateTime(timezone=True), nullable=False, index=True)
    app_name     = Column(String(255), nullable=True)
    window_title = Column(Text,        nullable=True)
    file_path    = Column(Text,        nullable=True)

    mouse_clicks = Column(Integer, default=0)
    key_presses  = Column(Integer, default=0)
    is_active    = Column(Boolean, default=True)

    # Idle detection
    idle_seconds    = Column(Integer, default=0)
    # Real wall-clock length of this tracker tick, as measured and reported by
    # the desktop tracker (`interval_seconds` in the screenshot payload). The
    # active-time aggregators use THIS, not the configured `screenshot_interval`
    # setting, so a tracker that fires every 30s is never credited a full
    # 5-/6-minute tick. NULL for legacy rows written before this column
    # existed — aggregators fall back to the configured-interval/gap heuristic
    # for those.
    interval_seconds = Column(Integer, nullable=True)
    productivity_score = Column(Float, default=0.0)

    # Compressed JPEG thumbnail stored as base64 text (survives container restarts)
    thumbnail = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user = relationship("User", back_populates="activities")

    __table_args__ = (
        Index("ix_activities_user_ts", "user_id", "timestamp"),
        Index("ix_activities_org_ts",  "organization_id", "timestamp"),
    )

    def __repr__(self):
        return f"<Activity {self.app_name}>"


# ──────────────────────────────────────────────
# LIVE STATUS  (Current foreground app — one row per user, upserted)
# Populated by the desktop tracker via POST /tracking/heartbeat every ~15s
# so the admin "My Team" cards show what each employee is *actually* using
# right now, independent of the screenshot interval.
# ──────────────────────────────────────────────
class LiveStatus(Base):
    __tablename__ = "live_statuses"

    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    app_name        = Column(String(255), nullable=True)
    window_title    = Column(Text,        nullable=True)

    # Browser-level current state (populated by the Temveris browser extension).
    # NULL when the foreground app isn't a browser or the extension isn't installed.
    url          = Column(Text,        nullable=True)
    domain       = Column(String(255), nullable=True)
    tab_title    = Column(Text,        nullable=True)
    favicon_url  = Column(Text,        nullable=True)
    browser      = Column(String(64),  nullable=True)   # chrome | edge | firefox | ...
    is_idle      = Column(Boolean,     default=False)
    tab_since    = Column(DateTime(timezone=True), nullable=True)  # when the active tab last changed

    # Desktop tracker build the heartbeat came from (e.g. "1.0.11"). Lets the
    # admin dashboard surface "stuck on old version" without a separate poll.
    # NULL for browser-only sessions and for legacy trackers that don't report it.
    app_version  = Column(String(32),  nullable=True)

    updated_at   = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<LiveStatus user={self.user_id} app={self.app_name}>"


# ──────────────────────────────────────────────
# BROWSER ACTIVITY  (Per-tab sessions from the Temveris browser extension)
# One row per continuous tab focus interval — closed when the user switches
# tabs, the tab goes inactive for ≥ idle threshold, or the browser closes.
# ──────────────────────────────────────────────
class BrowserActivity(Base):
    __tablename__ = "browser_activities"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True, index=True)

    url          = Column(Text,        nullable=False)
    domain       = Column(String(255), nullable=False, index=True)
    tab_title    = Column(Text,        nullable=True)
    favicon_url  = Column(Text,        nullable=True)
    browser      = Column(String(64),  nullable=True)

    started_at   = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at     = Column(DateTime(timezone=True), nullable=True)
    duration_sec = Column(Integer, default=0)
    active_sec   = Column(Integer, default=0)   # seconds with user interaction
    idle_sec     = Column(Integer, default=0)

    mouse_clicks = Column(Integer, default=0)
    key_presses  = Column(Integer, default=0)
    scrolls      = Column(Integer, default=0)

    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_browser_activities_user_started", "user_id", "started_at"),
        Index("ix_browser_activities_user_domain",  "user_id", "domain"),
        Index("ix_browser_activities_org_started",  "organization_id", "started_at"),
    )

    def __repr__(self):
        return f"<BrowserActivity user={self.user_id} domain={self.domain} dur={self.duration_sec}s>"


# ──────────────────────────────────────────────
# APP SESSION  (desktop foreground-tool usage for the activity timeline)
# Written by the tracking heartbeat (/tracking/sync) whenever the foreground
# desktop TOOL changes — one point-event per switch (browsers excluded; their
# usage is already captured per-visit in browser_activities). This is a
# DISPLAY-ONLY source for the timeline: it is deliberately NOT read by any
# productivity / active-time / idle calculation, so it can never skew metrics.
# ──────────────────────────────────────────────
class AppSession(Base):
    __tablename__ = "app_sessions"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",          ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id",  ondelete="SET NULL"), nullable=True, index=True)

    app_name     = Column(String(255), nullable=True)
    window_title = Column(Text,        nullable=True)

    started_at   = Column(DateTime(timezone=True), nullable=False, index=True)

    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_app_sessions_user_started", "user_id", "started_at"),
        Index("ix_app_sessions_org_started",  "organization_id", "started_at"),
    )

    def __repr__(self):
        return f"<AppSession user={self.user_id} app={self.app_name}>"


# ──────────────────────────────────────────────
# DOMAIN CATEGORY OVERRIDES
# Admin-curated work/neutral/distraction labels that override the built-in
# defaults in app.browser.productivity.DEFAULT_CATEGORIES.
# ──────────────────────────────────────────────
class DomainCategory(Base):
    __tablename__ = "domain_categories"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    domain          = Column(String(255), nullable=False, index=True)
    category        = Column(String(32),  nullable=False)   # work | neutral | distraction
    updated_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<DomainCategory {self.domain}={self.category}>"


# ──────────────────────────────────────────────
# WORKFORCE INTELLIGENCE — derived analytics
# These tables store *computed* signals derived from BrowserActivity +
# Activity. They are write-rarely / read-often: a daily background job
# materialises them once per (user, date) so the cinematic admin views
# don't have to recompute heavy aggregates on every render. Cache key is
# always (user_id, day) — recomputing replaces the existing row.
# ──────────────────────────────────────────────
class FocusSession(Base):
    """A continuous block of deep work on a single domain or work-category app.

    Definition: ≥ FOCUS_MIN_MINUTES of activity on the same domain (or same
    work-category app), with no gap longer than FOCUS_GAP_SECONDS between
    successive samples. Idle seconds inside the block do not break the
    session — only a real context switch does."""
    __tablename__ = "focus_sessions"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at   = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at     = Column(DateTime(timezone=True), nullable=False)
    duration_sec = Column(Integer, default=0)
    primary_domain = Column(String(255), nullable=True)
    primary_app  = Column(String(255), nullable=True)
    category     = Column(String(32),  nullable=True)   # work | neutral | distraction
    depth_score  = Column(Float,       default=0.0)     # 0..1 — higher = more focused
    tab_switches = Column(Integer,     default=0)
    keystrokes   = Column(Integer,     default=0)
    mouse_clicks = Column(Integer,     default=0)
    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_focus_sessions_user_started", "user_id", "started_at"),
    )

    def __repr__(self):
        return f"<FocusSession user={self.user_id} {self.primary_domain} {self.duration_sec}s depth={self.depth_score}>"


class WorkflowTransition(Base):
    """A single (from → to) hop between tabs / apps. Used to reconstruct
    workflow sequences like GitHub → VS Code → Docker → Postman so the
    dashboard can show "this looks like a backend deploy flow"."""
    __tablename__ = "workflow_transitions"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    transitioned_at = Column(DateTime(timezone=True), nullable=False, index=True)
    from_kind     = Column(String(16), nullable=False)  # 'tab' | 'app'
    from_key      = Column(String(255), nullable=True)  # domain or app_name
    to_kind       = Column(String(16), nullable=False)
    to_key        = Column(String(255), nullable=True)
    gap_sec       = Column(Integer, default=0)          # idle gap between the two
    created_at    = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_workflow_transitions_user_at", "user_id", "transitioned_at"),
    )

    def __repr__(self):
        return f"<WorkflowTransition user={self.user_id} {self.from_key}→{self.to_key}>"


class DailyIntelligenceSummary(Base):
    """LLM-generated narrative summary of an employee's day, cached so we
    don't re-spend Claude API tokens on every dashboard view. One row per
    (user_id, summary_date). Recomputation replaces the existing row."""
    __tablename__ = "daily_intelligence_summaries"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    summary_date    = Column(DateTime(timezone=True), nullable=False, index=True)
    narrative       = Column(Text,    nullable=False)
    headline        = Column(String(255), nullable=True)
    productivity_pct = Column(Float, default=0.0)
    deep_work_min   = Column(Integer, default=0)
    context_switches = Column(Integer, default=0)
    burnout_risk    = Column(String(16), default="low")  # low | moderate | high
    primary_focus   = Column(String(255), nullable=True)
    generator       = Column(String(32), default="rules")  # 'claude' | 'rules'
    payload         = Column(JSON, nullable=True)          # raw inputs, for debugging
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_daily_intel_user_date", "user_id", "summary_date", unique=True),
    )

    def __repr__(self):
        return f"<DailyIntelligenceSummary user={self.user_id} date={self.summary_date} gen={self.generator}>"


# ──────────────────────────────────────────────
# APP CATEGORY (admin overrides for desktop app classification)
# Mirrors DomainCategory above. Drives the "App Review" admin page and
# the "Productive Apps %" metric on Today / dashboards.
# organization_id scopes overrides per tenant — org A cannot affect org B.
# ──────────────────────────────────────────────
class AppCategory(Base):
    __tablename__ = "app_categories"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    # Stored lowercase, no .exe — see app_review.productivity._normalize_app
    app_name        = Column(String(255), nullable=False, index=True)
    # productive | neutral | distracting | blocked
    category        = Column(String(32),  nullable=False)
    # Tracks where the row came from so the UI can show a "Suggested" badge
    # even when the admin hasn't reviewed yet. "manual" = explicit admin set,
    # "auto" = applied by the heuristic auto-suggest sweep.
    source          = Column(String(16),  nullable=False, default="manual")
    notes           = Column(Text,        nullable=True)
    updated_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AppCategory {self.app_name}={self.category} org={self.organization_id}>"


# ──────────────────────────────────────────────
# LEAVE POLICY
# ──────────────────────────────────────────────
class LeavePolicy(Base):
    __tablename__ = "leave_policies"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    leave_type      = Column(String(100), nullable=False)

    annual_limit      = Column(Integer, default=12)
    monthly_accrual   = Column(Float,   default=1.0)
    max_carry_forward = Column(Integer, default=0)
    requires_document = Column(Boolean, default=False)
    min_notice_days   = Column(Integer, default=0)
    approval_chain    = Column(JSON,    default=list)

    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "leave_type", name="uq_leave_policies_org_type"),
    )

    def __repr__(self):
        return f"<LeavePolicy org={self.organization_id} {self.leave_type}>"


# ──────────────────────────────────────────────
# LEAVE REQUEST  (state machine)
# Draft → Submitted → UnderReview → Approved/Rejected → LockedForPayroll
# ──────────────────────────────────────────────
class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)

    leave_type  = Column(String(100), nullable=False)
    from_date   = Column(DateTime,    nullable=False)
    to_date     = Column(DateTime,    nullable=False)

    # Mandatory description
    description = Column(Text, nullable=False, default="")
    reason      = Column(Text, nullable=True)   # legacy compat alias

    # State machine
    status         = Column(String(50), default=LeaveStatus.SUBMITTED, index=True)
    reviewed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at    = Column(DateTime, nullable=True)
    review_comment = Column(Text, nullable=True)

    # Document attachment (file path or S3 key)
    attachment_path = Column(Text, nullable=True)
    attachment_name = Column(String(255), nullable=True)

    # Enterprise leave management fields
    is_lwp     = Column(Boolean, default=False)            # auto-converted to Unpaid Leave
    is_awol    = Column(Boolean, default=False)            # marked as AWOL
    hr_flag    = Column(Boolean, default=False)            # flagged for HR review
    doc_status = Column(String(50), default="not_required") # not_required | pending | submitted
    # Number of working days the request consumes. Float so half-day requests
    # can store 0.5 — historical full-day rows persist as 1.0 after migration
    # because the ALTER TABLE keeps existing INTEGER values intact and SQLite
    # / Postgres both up-cast 1 → 1.0 transparently.
    leave_days = Column(Float, default=1.0)

    # Half-day support. is_half_day flips for any single-day request that the
    # employee tagged as half-day OR for any request whose leave_type is the
    # dedicated "Half Day Leave" type. half_day_period stores the user's pick
    # ("first_half" | "second_half") or NULL for full-day requests. Persisting
    # the intent in dedicated columns (vs. parsing the description prefix)
    # lets balance math and admin views read it directly without string
    # gymnastics, and keeps the audit log shape stable.
    is_half_day     = Column(Boolean, default=False, nullable=False)
    half_day_period = Column(String(20), nullable=True)

    # Compensatory Leave justification. JSON array of "YYYY-MM-DD" strings for
    # the days the employee already worked (overtime / weekend / public-holiday
    # work) that earn this comp-off. Lets HR verify the claim against the
    # listed dates. NULL for non-comp leave types and rows written before this
    # column existed.
    worked_dates = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    user        = relationship("User", back_populates="leave_requests", foreign_keys=[user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        # "Any leave covering today" = from_date <= today <= to_date, run on
        # every dashboard/team load. A composite on the date range lets the
        # overlap scan use the index instead of reading the whole table.
        Index("ix_leave_requests_dates", "from_date", "to_date"),
        # "This user's requests in state X" (e.g. pending approvals).
        Index("ix_leave_requests_user_status", "user_id", "status"),
        # Reports/dashboards predicate user_id + date window + status set in
        # one shot (e.g. payroll LWP scan). Neither prefix covers all four
        # columns, so without this composite the planner falls back to a
        # Seq Scan + filter on large tables.
        Index(
            "ix_leave_requests_user_dates_status",
            "user_id", "from_date", "to_date", "status",
        ),
    )

    def __repr__(self):
        return f"<LeaveRequest {self.user_id} – {self.leave_type} – {self.status}>"


# ──────────────────────────────────────────────
# LEAVE BALANCE MANAGEMENT  (HR per-employee allocation overrides)
# ──────────────────────────────────────────────
class EmployeeLeaveBalance(Base):
    """Per-employee leave allocation OVERRIDE, keyed by leave_type STRING.

    The base system derives every balance as `remaining = allocated - used`,
    where `allocated` defaults to the leave type's annual entitlement and
    `used` is summed live from non-rejected LeaveRequest rows. This table lets
    HR override only the `allocated` side for one (employee, leave_type): the
    mere presence of a row means "use this number instead of the policy
    default". `used` is NEVER stored here — it stays derived, so a manual grant
    can never silently erase days the employee already consumed.

    Keyed by the leave_type string (not a numeric id) so that every current and
    future leave type — including custom types HR creates at runtime — is
    supported automatically with no code change.
    """
    __tablename__ = "employee_leave_balances"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    leave_type      = Column(String(100), nullable=False)
    allocated_days  = Column(Float, nullable=False, default=0.0)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # One override per (employee, leave_type). Upserts target this key.
        UniqueConstraint("user_id", "leave_type", name="uq_employee_leave_balances_user_type"),
        Index("ix_employee_leave_balances_org", "organization_id"),
    )

    def __repr__(self):
        return f"<EmployeeLeaveBalance {self.user_id} – {self.leave_type} = {self.allocated_days}>"


class LeaveBalanceAdjustment(Base):
    """Immutable audit trail of every Grant / Deduct / Reset / Custom action.

    One row is written per HR adjustment and never updated, so the full history
    of how an employee's allocation changed (and why) is always reconstructable
    independent of the live override row in EmployeeLeaveBalance.
    """
    __tablename__ = "leave_balance_adjustments"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id     = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    leave_type          = Column(String(100), nullable=False)
    adjustment_type     = Column(String(20), nullable=False)   # grant | deduct | reset | custom
    adjustment_value    = Column(Float, nullable=True)         # NULL for reset
    previous_allocation = Column(Float, nullable=True)         # NULL if no override existed before
    new_allocation      = Column(Float, nullable=True)         # NULL after a reset
    reason              = Column(Text, nullable=True)
    effective_date      = Column(DateTime, nullable=True)
    created_by          = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at          = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_leave_balance_adjustments_user_type", "user_id", "leave_type"),
    )

    def __repr__(self):
        return f"<LeaveBalanceAdjustment {self.user_id} – {self.leave_type} – {self.adjustment_type}>"


# ──────────────────────────────────────────────
# AUDIT LOG  (Governance Layer – immutable evidence chain)
# ──────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_id        = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    target_id       = Column(Integer, nullable=True)      # e.g. affected user id
    entity          = Column(String(100), nullable=False) # "User" | "LeaveRequest" | etc.
    action          = Column(String(100), nullable=False) # "UserInvited" | "LeaveApproved" | etc.
    payload         = Column(JSON, nullable=True)         # full diff / metadata
    ip_address      = Column(String(45), nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    actor = relationship("User", back_populates="audit_logs", foreign_keys=[actor_id])

    __table_args__ = (
        # The hot path (dashboard "already notified Absent today" check)
        # filters entity + action + created_at together. Extending the
        # composite with created_at lets that whole predicate resolve from
        # one index; (entity, action) queries still use it as a prefix, so
        # this supersedes the old ix_audit_entity_action.
        Index("ix_audit_entity_action_created", "entity", "action", "created_at"),
    )

    def __repr__(self):
        return f"<AuditLog {self.action} by actor={self.actor_id}>"


# ──────────────────────────────────────────────
# EMAIL SUPPRESSION  (one-click List-Unsubscribe list)
# Backs the previously-dead unsubscribe URL. Per the Commercial Email
# Communications Estate, unsubscribe applies to MARKETING / subscribed
# messages only — essential account, security, attendance, approval, and
# billing communications are always delivered and ignore this list.
# ──────────────────────────────────────────────
class EmailSuppression(Base):
    __tablename__ = "email_suppressions"

    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String(255), nullable=False, unique=True, index=True)
    suppressed = Column(Boolean, nullable=False, default=True)
    scope      = Column(String(20), nullable=False, default="marketing")  # marketing | all
    source     = Column(String(30), nullable=True)   # one_click | manual | complaint | api
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<EmailSuppression {self.email} suppressed={self.suppressed} scope={self.scope}>"


# ──────────────────────────────────────────────
# OUTBOX EVENT  (Transactional Outbox Pattern)
# ──────────────────────────────────────────────
class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id           = Column(Integer, primary_key=True, index=True)
    event_type   = Column(String(100), nullable=False, index=True)  # "UserInvited", "LeaveRequested", etc.
    aggregate_id = Column(String(100), nullable=True)               # e.g. str(user.id)
    payload      = Column(JSON, nullable=False, default=dict)
    status       = Column(String(20), default=EventStatus.PENDING, index=True)
    attempts     = Column(Integer, default=0)
    error        = Column(Text, nullable=True)

    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow)
    published_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<OutboxEvent {self.event_type} status={self.status}>"


# ──────────────────────────────────────────────
# WORK EVIDENCE CHAIN  (WEC)
# Append-only, hash-chained immutable audit records.
# Every important user action produces one entry.
# ──────────────────────────────────────────────
class WorkEvidenceChain(Base):
    __tablename__ = "work_evidence_chain"

    id              = Column(Integer, primary_key=True, index=True)
    work_record_id  = Column(String(100), unique=True, nullable=False, index=True)  # UUID
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)

    timestamp   = Column(DateTime(timezone=True), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)   # login | logout | break_started | break_ended | screenshot | activity
    device_info = Column(JSON, nullable=True)           # ip_address, user_agent

    previous_hash     = Column(String(64), nullable=True)    # SHA-256 of previous WEC entry for this user
    current_hash      = Column(String(64), nullable=False)   # SHA-256 of this entry
    validation_status = Column(String(50), default="valid")  # valid | invalid | flagged
    integrity_score   = Column(Float, default=1.0)           # 0.0 – 1.0

    payload    = Column(JSON, nullable=True)   # action-specific data
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_wec_user_ts", "user_id", "timestamp"),
    )

    def __repr__(self):
        return f"<WorkEvidenceChain {self.action_type} user={self.user_id}>"


# ──────────────────────────────────────────────
# WORK EVENT  (Versioned Event Log)
# Idempotent, append-only event store.
# ──────────────────────────────────────────────
class WorkEvent(Base):
    __tablename__ = "work_events"

    id              = Column(Integer, primary_key=True, index=True)
    event_id        = Column(String(100), unique=True, nullable=False, index=True)  # UUID
    event_type      = Column(String(100), nullable=False, index=True)  # USER_LOGIN | BREAK_STARTED | etc.
    event_version   = Column(String(10), default="1.0")
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)
    payload         = Column(JSON, nullable=False, default=dict)
    idempotency_key = Column(String(255), nullable=True, unique=True, index=True)
    processed       = Column(Boolean, default=False)
    wec_id          = Column(String(100), nullable=True)   # linked WorkEvidenceChain.work_record_id

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_work_events_user_type", "user_id", "event_type"),
    )

    def __repr__(self):
        return f"<WorkEvent {self.event_type} event_id={self.event_id}>"


# ──────────────────────────────────────────────
# INTEGRITY SCORE  (Per-session fraud detection)
# ──────────────────────────────────────────────
class IntegrityScore(Base):
    __tablename__ = "integrity_scores"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)
    date            = Column(DateTime, nullable=False, index=True)

    score                = Column(Float, default=1.0)   # 0.0 = fraud risk, 1.0 = clean
    anomaly_flags        = Column(JSON,  default=list)  # list of anomaly description strings
    idle_spoofing_risk   = Column(Float, default=0.0)
    fake_break_risk      = Column(Float, default=0.0)
    time_inflation_risk  = Column(Float, default=0.0)
    unusual_gaps_risk    = Column(Float, default=0.0)

    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_integrity_user_date", "user_id", "date"),
    )

    def __repr__(self):
        return f"<IntegrityScore user={self.user_id} score={self.score}>"


# ══════════════════════════════════════════════════════════════════════════
# ACTIVE TIME ENGINE  (redesigned, screenshot-correlated activity scoring)
#
# Three-tier materialised pipeline so "Active Time %" is computed ONCE,
# server-side, and stored — instead of being re-derived on every request from
# a single-OR `is_active` boolean (the old design that saturated to 100%).
#
#   raw Activity rows  →  ActivityWindow (per screenshot interval, scored 0-100)
#                      →  ActivityMinuteScore (per minute, classified)
#                      →  ActivityDailyScore (per user/day, capped active_pct)
#
# All scoring lives in app/active_time/engine.py; these tables only persist the
# results. See docs/active-time-engine-analysis.md.
# ══════════════════════════════════════════════════════════════════════════
class ActivityWindow(Base):
    """One scored screenshot interval ("window"). Derived from an Activity row."""
    __tablename__ = "activity_windows"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id",          ondelete="SET NULL"), nullable=True, index=True)
    session_id  = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)
    activity_id = Column(Integer, ForeignKey("activities.id",     ondelete="SET NULL"), nullable=True)

    window_start    = Column(DateTime(timezone=True), nullable=False, index=True)
    window_end      = Column(DateTime(timezone=True), nullable=False)
    interval_seconds = Column(Integer, nullable=False, default=0)

    # Raw counts captured by the tracker for this interval
    key_presses   = Column(Integer, default=0)
    mouse_clicks  = Column(Integer, default=0)
    mouse_moves   = Column(Integer, default=0)
    mouse_scrolls = Column(Integer, default=0)
    mouse_events  = Column(Integer, default=0)   # clicks + scrolls + moves (combined "mouse strikes")
    active_seconds = Column(Integer, default=0)  # OS non-idle seconds within the interval
    idle_seconds   = Column(Integer, default=0)

    app_name      = Column(String(255), nullable=True)
    has_screenshot = Column(Boolean, default=False)
    hook_active   = Column(Boolean, default=True)   # native input hook loaded on the client?
    on_break      = Column(Boolean, default=False)

    # Weighted sub-scores (0..1) and the combined window score (0..100)
    keyboard_score       = Column(Float, default=0.0)
    mouse_score          = Column(Float, default=0.0)
    focus_score          = Column(Float, default=0.0)
    continuity_score     = Column(Float, default=0.0)
    screenshot_score     = Column(Float, default=0.0)
    app_engagement_score = Column(Float, default=0.0)
    score                = Column(Float, default=0.0)   # 0..100
    label            = Column(String(20), default="idle")  # inactive|low|moderate|active|highly_active|break

    fully_active = Column(Boolean, default=False)   # met BOTH min keystroke AND min mouse thresholds
    flags        = Column(JSON, nullable=True)      # e.g. ["LIMITED_TRACKING","EMPTY_SCREENSHOT_ANOMALY"]

    active_credit_seconds = Column(Integer, default=0)  # seconds of this window counted as active

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_activity_windows_user_start", "user_id", "window_start"),
        Index("ix_activity_windows_start", "window_start"),  # org-wide daily scans
    )

    def __repr__(self):
        return f"<ActivityWindow user={self.user_id} score={self.score} {self.label}>"


class ActivityMinuteScore(Base):
    """One minute classified ACTIVE / PARTIALLY_ACTIVE / LOW_ACTIVITY / IDLE / BREAK."""
    __tablename__ = "activity_minute_scores"

    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    window_id = Column(Integer, ForeignKey("activity_windows.id", ondelete="SET NULL"), nullable=True)

    minute_start   = Column(DateTime(timezone=True), nullable=False, index=True)  # truncated to the minute
    classification = Column(String(20), default="IDLE")
    score          = Column(Float, default=0.0)        # 0..100 inherited from the window
    active_seconds = Column(Integer, default=0)        # weight × 60
    on_break       = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_activity_minute_user_minute", "user_id", "minute_start", unique=True),
    )

    def __repr__(self):
        return f"<ActivityMinuteScore user={self.user_id} {self.minute_start} {self.classification}>"


class ActivityDailyScore(Base):
    """One per (user, IST day) — the authoritative, cached Active Time figures."""
    __tablename__ = "activity_daily_scores"

    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    day     = Column(DateTime(timezone=True), nullable=False)   # IST midnight anchor

    worked_seconds = Column(Integer, default=0)   # clocked working time minus breaks (denominator)
    active_seconds = Column(Integer, default=0)   # numerator from minute scores
    idle_seconds   = Column(Integer, default=0)
    break_seconds  = Column(Integer, default=0)

    active_pct     = Column(Float, default=0.0)   # capped (anti-inflation) — what the UI shows
    raw_active_pct = Column(Float, default=0.0)   # before cap, for transparency/audit
    coverage_ratio = Column(Float, default=0.0)   # tracked_seconds / worked_seconds (0..1)

    minutes_active  = Column(Integer, default=0)
    minutes_partial = Column(Integer, default=0)
    minutes_low     = Column(Integer, default=0)
    minutes_idle    = Column(Integer, default=0)
    window_count    = Column(Integer, default=0)

    flags       = Column(JSON, nullable=True)
    # window_start of the last window whose per-minute rows have been persisted
    # (write-once). Lets the reconcile worker append minute scores for windows
    # past the continuity horizon without ever rewriting earlier minutes.
    finalized_through = Column(DateTime(timezone=True), nullable=True)
    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_activity_daily_user_day", "user_id", "day", unique=True),
        Index("ix_activity_daily_day", "day"),
    )

    def __repr__(self):
        return f"<ActivityDailyScore user={self.user_id} day={self.day} pct={self.active_pct}>"


# ──────────────────────────────────────────────
# APP SETTINGS  (company-wide configurable parameters)
# ──────────────────────────────────────────────
class AppSettings(Base):
    __tablename__ = "app_settings"

    id              = Column(Integer, primary_key=True, index=True)
    # ondelete=RESTRICT on this and the schedule tables (shifts,
    # shift_assignments, holidays) is deliberate. Organizations are
    # soft-lifecycle entities — nothing in the codebase hard-deletes one; the
    # lifecycle is CommercialOrganization.commercial_state with a reversible
    # suspend/restore. SET NULL would drop the row into the shared
    # NULL-organization bucket, where an org-less read can pick it up (the
    # cross-tenant exposure repaired in P2). CASCADE would let one stray DELETE
    # silently destroy a tenant's whole configuration. RESTRICT makes an
    # accidental delete fail loudly and keeps the data. Migration: e8b3c6d1f472.
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True, index=True)
    # Scope tier for the 3-level settings cascade (employee > team > org > system
    # default). A NULL team_key AND NULL user_id row is org-level; team_key set is
    # a team override; user_id set is an employee override. Team membership is the
    # User.team name string (there is no teams.id FK on users), so team scope keys
    # on that same string. Resolution lives in app/settings/scope.py.
    team_key = Column(String(100), nullable=True, index=True)
    user_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    key   = Column(String(100), nullable=False, index=True)
    value = Column(Text, nullable=True)
    description = Column(String(255), nullable=True)

    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # ponytail: real uniqueness is a COALESCE(team_key,'')/COALESCE(user_id,0)
    # expression index created in the migration (Postgres treats NULLs as
    # distinct, so a plain UniqueConstraint would let two org-level rows for the
    # same key slip through). This composite index is just for lookup speed.
    __table_args__ = (
        Index("ix_app_settings_scope_lookup", "organization_id", "team_key", "user_id", "key"),
    )

    def __repr__(self):
        return f"<AppSettings org={self.organization_id} team={self.team_key} user={self.user_id} {self.key}={self.value}>"


# ──────────────────────────────────────────────
# RISK ALERT LOG  (auto-generated alerts for HR)
# ──────────────────────────────────────────────
class RiskAlert(Base):
    __tablename__ = "risk_alerts"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)

    alert_date     = Column(DateTime(timezone=True), nullable=False)
    total_seconds  = Column(Integer, default=0)      # actual seconds worked
    # Snapshot of the expected-hours policy in force when the alert fired —
    # always written explicitly by the risk checker from the resolver, so the
    # column carries no policy of its own.
    threshold_secs = Column(Integer, default=0)
    alert_type     = Column(String(50), default="insufficient_hours")
    email_sent     = Column(Boolean, default=False)
    notes          = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<RiskAlert user={self.user_id} date={self.alert_date}>"


# ──────────────────────────────────────────────
# PAYROLL CONFIG  (per-employee pay configuration)
# ──────────────────────────────────────────────
class PayrollConfig(Base):
    __tablename__ = "payroll_configs"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)

    pay_type      = Column(String(20), default="salary")  # salary | hourly
    hourly_rate   = Column(Float, default=0.0)
    basic_salary  = Column(Float, default=0.0)
    hra           = Column(Float, default=0.0)
    da            = Column(Float, default=0.0)
    ta            = Column(Float, default=0.0)
    pf            = Column(Float, default=0.0)
    tax           = Column(Float, default=0.0)
    bonus         = Column(Float, default=0.0)
    currency      = Column(String(10), default="USD")
    pay_frequency = Column(String(20), default="monthly")  # monthly | biweekly | weekly
    bank_account  = Column(String(100), nullable=True)
    notes         = Column(Text, nullable=True)

    effective_from = Column(DateTime(timezone=True), nullable=True)
    is_active      = Column(Boolean, default=True)
    created_at     = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at     = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<PayrollConfig user={self.user_id} basic={self.basic_salary}>"


# ──────────────────────────────────────────────
# PAYROLL RUN  (batch payroll processing record)
# ──────────────────────────────────────────────
class PayrollRun(Base):
    __tablename__ = "payroll_runs"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    period_start    = Column(DateTime(timezone=True), nullable=False)
    period_end      = Column(DateTime(timezone=True), nullable=False)
    status          = Column(String(20), default="draft")  # draft | processed | paid
    total_amount    = Column(Float, default=0.0)
    currency        = Column(String(10), default="USD")
    notes           = Column(Text, nullable=True)
    created_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)

    items = relationship("PayrollItem", back_populates="payroll_run", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PayrollRun {self.period_start} → {self.period_end} status={self.status}>"


# ──────────────────────────────────────────────
# PAYROLL ITEM  (per-employee line in a payroll run)
# ──────────────────────────────────────────────
class PayrollItem(Base):
    __tablename__ = "payroll_items"

    id              = Column(Integer, primary_key=True, index=True)
    payroll_run_id  = Column(Integer, ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)

    worked_hours    = Column(Float, default=0.0)
    basic_salary    = Column(Float, default=0.0)
    hra             = Column(Float, default=0.0)
    da              = Column(Float, default=0.0)
    ta              = Column(Float, default=0.0)
    bonus           = Column(Float, default=0.0)
    gross_pay       = Column(Float, default=0.0)
    pf_deduction    = Column(Float, default=0.0)
    tax_deduction   = Column(Float, default=0.0)
    other_deduction = Column(Float, default=0.0)
    net_pay         = Column(Float, default=0.0)
    status          = Column(String(20), default="pending")  # pending | paid
    paid_at         = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    payroll_run = relationship("PayrollRun", back_populates="items")
    user        = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<PayrollItem user={self.user_id} net={self.net_pay}>"


# ──────────────────────────────────────────────
# SHIFT  (reusable shift template)
# ──────────────────────────────────────────────
class Shift(Base):
    __tablename__ = "shifts"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True, index=True)
    name            = Column(String(100), nullable=False)
    shift_type      = Column(String(50), default="working")  # working | meeting | training
    start_time      = Column(String(8),  nullable=False)     # "09:00"
    end_time        = Column(String(8),  nullable=False)     # "18:00"
    days_of_week    = Column(JSON, default=lambda: [0,1,2,3,4])  # 0=Mon … 6=Sun
    color           = Column(String(20), default="#3B82F6")
    is_active       = Column(Boolean, default=True)
    created_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)

    # ── Work Schedule Policy fields (migration f1a9d4c72e86) ──────────────────
    # Required WORKED seconds for this shift, EXCLUDING breaks. Deliberately not
    # derived from (end_time - start_time): the window includes lunch, so it is
    # not required worked time. NULL means "fall back to the settings cascade".
    expected_work_seconds = Column(Integer, nullable=True)
    # Explicit night-shift marker (e.g. 22:00–06:00). The resolver infers this
    # from end <= start when unset; the column lets an org state it outright.
    crosses_midnight      = Column(Boolean, nullable=False, default=False, server_default="false")
    # The one shift seeded per organisation.
    is_default            = Column(Boolean, nullable=False, default=False, server_default="false")
    # Open-ended per-shift rules (overtime, night differential, hybrid/remote day
    # patterns, rotation cycles, break windows). This is the extensibility
    # contract: new rules are JSON keys, never new columns or tables.
    config                = Column(JSON, nullable=True)

    assignments = relationship("ShiftAssignment", back_populates="shift", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Shift {self.name} {self.start_time}–{self.end_time}>"


# ──────────────────────────────────────────────
# SHIFT ASSIGNMENT  (employee ↔ shift on a date)
# ──────────────────────────────────────────────
class ShiftAssignment(Base):
    __tablename__ = "shift_assignments"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id",  ondelete="CASCADE"),   nullable=False, index=True)
    shift_id        = Column(Integer, ForeignKey("shifts.id", ondelete="SET NULL"),  nullable=True,  index=True)

    date       = Column(String(10), nullable=False, index=True)  # "YYYY-MM-DD"
    start_time = Column(String(8),  nullable=True)
    end_time   = Column(String(8),  nullable=True)
    task_type  = Column(String(50), default="working")  # working | break | leave | meeting
    status     = Column(String(50), default="scheduled")  # scheduled | completed | absent | cancelled
    notes      = Column(Text, nullable=True)

    # ── Work Schedule Policy fields (migration f1a9d4c72e86) ──────────────────
    # Per-date required worked seconds — the highest-precedence override.
    expected_work_seconds = Column(Integer, nullable=True)
    # A rostered non-working day (rotating shifts, off-days that do not follow
    # the weekly pattern). Kept separate from `status` so a withdrawn assignment
    # ("cancelled") and a scheduled day off stay distinguishable.
    is_off_day            = Column(Boolean, nullable=False, default=False, server_default="false")

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    shift = relationship("Shift", back_populates="assignments")
    user  = relationship("User",  foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_shift_asgn_user_date", "user_id", "date"),
    )

    def __repr__(self):
        return f"<ShiftAssignment user={self.user_id} date={self.date}>"


# ──────────────────────────────────────────────
# EMPLOYEE AVAILABILITY
# ──────────────────────────────────────────────
class EmployeeAvailability(Base):
    __tablename__ = "employee_availability"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date         = Column(String(10), nullable=False)   # "YYYY-MM-DD"
    is_available = Column(Boolean, default=True)
    reason       = Column(String(255), nullable=True)
    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_availability_user_date", "user_id", "date"),
    )

    def __repr__(self):
        return f"<EmployeeAvailability user={self.user_id} date={self.date} avail={self.is_available}>"

# ──────────────────────────────────────────────
# WORK RECORD  (Proof-of-Work Evidence)
# Append-only, versioned, hash-chained.
# Extends the WEC concept with full evidence fields.
# ──────────────────────────────────────────────
class WorkRecord(Base):
    __tablename__ = "work_records"

    id              = Column(Integer, primary_key=True, index=True)
    record_id       = Column(String(100), unique=True, nullable=False, index=True)   # UUID
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id      = Column(Integer, ForeignKey("clock_sessions.id", ondelete="SET NULL"), nullable=True)

    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    duration  = Column(Integer, default=0)      # seconds of work this record covers

    # Location evidence
    latitude  = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Device fingerprint
    device_id = Column(String(255), nullable=True, index=True)

    # Task linkage
    task_id = Column(String(255), nullable=True)

    # Artifact evidence (screenshot URL / file reference)
    artifact_url  = Column(Text, nullable=True)
    artifact_hash = Column(String(64), nullable=True)    # SHA-256 of the artifact file

    # Behavioral signals
    keyboard_activity = Column(Integer, default=0)       # key_presses count
    mouse_activity    = Column(Integer, default=0)       # mouse_clicks count

    # Policy context (e.g. {"shift": "day", "work_hours": "09:00-18:00"})
    policy_context = Column(JSON, nullable=True)

    # Validation outcome
    validation_status = Column(
        String(20), default=ValidationStatus.PENDING, index=True
    )                                                    # pending | validated | warning | flagged
    confidence_score  = Column(Integer, default=100)     # 0–100

    # Append-only versioning
    version          = Column(Integer, default=1)
    parent_record_id = Column(String(100), nullable=True)  # previous version of same record

    # Hash chain (tamper-evident)
    previous_hash = Column(String(64), nullable=True)
    current_hash  = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_work_records_user_ts", "user_id", "timestamp"),
        Index("ix_work_records_artifact", "artifact_hash"),
        Index("ix_work_records_device", "device_id"),
    )

    def __repr__(self):
        return f"<WorkRecord {self.record_id} user={self.user_id} score={self.confidence_score}>"


# ──────────────────────────────────────────────
# FRAUD EVENT  (Logged when validation detects fraud)
# ──────────────────────────────────────────────
class FraudEvent(Base):
    __tablename__ = "fraud_events"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    wec_id          = Column(String(100), nullable=False, index=True)   # work_records.record_id

    reason   = Column(Text, nullable=False)
    severity = Column(String(20), nullable=False, default=FraudSeverity.LOW)  # low|medium|high|critical
    details  = Column(JSON, nullable=True)                     # structured analysis data

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fraud_events_wec", "wec_id"),
        Index("ix_fraud_events_severity", "severity"),
    )

    def __repr__(self):
        return f"<FraudEvent wec={self.wec_id} severity={self.severity}>"


# ──────────────────────────────────────────────
# USER PREFERENCES  (per-user profile module config)
# Holds work preferences (timezone, working hours, working days),
# notification toggles, and basic availability flags. One row per user.
# ──────────────────────────────────────────────
class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Work preferences
    timezone           = Column(String(64),  default="UTC")
    work_start_time    = Column(String(8),   default="09:00")
    work_end_time      = Column(String(8),   default="18:00")
    work_days          = Column(JSON,        default=lambda: [0, 1, 2, 3, 4])  # 0=Mon..6=Sun
    daily_target_hours = Column(Float,       default=8.0)
    is_available       = Column(Boolean,     default=True)
    away_message       = Column(String(255), nullable=True)

    # Notification preferences
    notify_schedule_email     = Column(Boolean, default=True)
    notify_schedule_push      = Column(Boolean, default=True)
    notify_activity_summary   = Column(Boolean, default=True)
    notify_system_updates     = Column(Boolean, default=True)
    notify_leave_status       = Column(Boolean, default=True)
    notify_security_alerts    = Column(Boolean, default=True)
    weekly_digest             = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<UserPreferences user={self.user_id} tz={self.timezone}>"


# ──────────────────────────────────────────────
# USER SESSION  (active device / token registry)
# Records each successful login so the Profile page can show
# "Active Sessions" and revoke individual devices.
# ──────────────────────────────────────────────
class UserSession(Base):
    __tablename__ = "user_sessions"

    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 of the JWT used by this device. We never store the raw token,
    # only the digest — so we can match incoming bearer tokens but a leaked
    # database row still doesn't reveal valid auth credentials.
    token_hash = Column(String(64), unique=True, nullable=False, index=True)

    user_agent  = Column(String(512), nullable=True)
    device_type = Column(String(50),  nullable=True)   # desktop | mobile | tablet | electron | other
    device_name = Column(String(120), nullable=True)
    browser     = Column(String(80),  nullable=True)
    os          = Column(String(80),  nullable=True)
    ip_address  = Column(String(45),  nullable=True)
    location    = Column(String(120), nullable=True)   # best-effort city/country (optional)

    is_revoked      = Column(Boolean, default=False)
    last_active_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)
    revoked_at      = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_user_sessions_user_active", "user_id", "is_revoked"),
    )

    def __repr__(self):
        return f"<UserSession user={self.user_id} device={self.device_type} revoked={self.is_revoked}>"


# ──────────────────────────────────────────────
# REFRESH TOKEN  (persistent "Remember me" login)
# Long-lived, opaque, single-use rotating credential. The raw token is NEVER
# stored — only its SHA-256 digest. Each refresh mints a NEW row and marks the
# old one `rotated`. All rotations share a `family_id`; if a token that was
# already rotated is presented again (replay / theft), we revoke the whole
# family — the standard refresh-token reuse-detection defence used by
# Google/Auth0/etc.
#
# `session_id` links back to the UserSession device row so the Profile
# "Active devices" list, last-seen timestamps, and per-device revocation all
# keep working as the access token rotates underneath them.
# ──────────────────────────────────────────────
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        Integer,
        ForeignKey("user_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # SHA-256 of the opaque refresh token. Unique so a presented token maps to
    # exactly one row. The raw value lives only in the client (HttpOnly cookie
    # on web, OS-encrypted safeStorage in Electron).
    token_hash = Column(String(64), unique=True, nullable=False, index=True)

    # Shared across every rotation descended from one login — the "session
    # family" used for reuse detection.
    family_id  = Column(String(36), nullable=False, index=True)

    # True for "Remember for 30 days"; drives the cookie Max-Age and the
    # expiry below. When False the refresh token is short-lived (session).
    remember   = Column(Boolean, default=False, nullable=False)

    issued_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # Set when this token is exchanged for a new one (single-use rotation).
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    # Hard kill — logout, password reset, reuse detection.
    is_revoked = Column(Boolean, default=False, nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(64), nullable=True)  # logout | rotated | reuse_detected | password_reset | expired

    user    = relationship("User", foreign_keys=[user_id])
    session = relationship("UserSession", foreign_keys=[session_id])

    __table_args__ = (
        Index("ix_refresh_tokens_user_active", "user_id", "is_revoked"),
        Index("ix_refresh_tokens_family", "family_id", "is_revoked"),
    )

    def __repr__(self):
        return f"<RefreshToken user={self.user_id} family={self.family_id} revoked={self.is_revoked}>"


# ══════════════════════════════════════════════════════════════════════════════
# BILLING MODULE  (Tier-0 launch — May 1)
# Plans, subscriptions, invoices, payment methods, billing contacts, tax
# profile, downgrade/enterprise requests, and a normalized audit trail.
# All commercial values are DB-driven so finance can re-price without a deploy.
# ══════════════════════════════════════════════════════════════════════════════
class BillingPlan(Base):
    __tablename__ = "billing_plans"

    id                          = Column(Integer, primary_key=True, index=True)
    code                        = Column(String(40),  unique=True, nullable=False, index=True)
    name                        = Column(String(120), nullable=False)
    description                 = Column(Text,        nullable=True)
    badge                       = Column(String(60),  nullable=True)   # Starter / Recommended / Compliance Grade / Private Architecture
    short_description           = Column(Text,        nullable=True)
    tier_order                  = Column(Integer,     default=0)
    monthly_price_per_user      = Column(Float,       default=0.0)
    annual_price_per_user       = Column(Float,       default=0.0)
    annual_discount_percentage  = Column(Float,       default=20.0)
    user_limit                  = Column(Integer,     nullable=True)   # NULL = unlimited
    feature_list_json           = Column(JSON,        default=list)    # [{"label": "...", "highlighted": false}, ...]
    feature_flags_json          = Column(JSON,        default=dict)
    is_most_popular             = Column(Boolean,     default=False)
    is_enterprise               = Column(Boolean,     default=False)
    trial_enabled               = Column(Boolean,     default=False)
    active_status               = Column(Boolean,     default=True)
    currency                    = Column(String(10),  default="USD")
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingPlan {self.code} ${self.monthly_price_per_user}/u>"


class BillingSubscription(Base):
    __tablename__ = "billing_subscriptions"

    id                          = Column(Integer, primary_key=True, index=True)
    organization_id             = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_id                     = Column(Integer, ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True)
    stripe_customer_id          = Column(String(120), nullable=True)
    stripe_subscription_id      = Column(String(120), nullable=True)
    billing_cycle               = Column(String(20),  default="monthly")   # monthly | annual
    subscription_status         = Column(String(30),  default="active")    # active | trial | past_due | cancel_pending | enterprise_contract
    active_users                = Column(Integer,     default=0)
    billable_users              = Column(Integer,     default=0)
    pending_invites             = Column(Integer,     default=0)
    suspended_users             = Column(Integer,     default=0)
    renewal_date                = Column(DateTime(timezone=True), nullable=True)
    trial_start_date            = Column(DateTime(timezone=True), nullable=True)
    trial_end_date              = Column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at   = Column(DateTime(timezone=True), nullable=True)
    # When the current run of payment failures began. Suspension is DERIVED from this
    # (anchor + grace days) rather than stored separately, so the two can never
    # disagree; clearing it on payment recovery restores the tenant atomically.
    # Not kept in subscription_status, which mirrors Stripe and is rewritten by every
    # webhook. See app/billing/dunning.py.
    dunning_started_at          = Column(DateTime(timezone=True), nullable=True, index=True)
    enterprise_contract_flag    = Column(Boolean,     default=False)
    currency                    = Column(String(10),  default="USD")
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    plan = relationship("BillingPlan", foreign_keys=[plan_id])

    def __repr__(self):
        return f"<BillingSubscription org={self.organization_id} plan={self.plan_id} status={self.subscription_status}>"


class BillingInvoice(Base):
    __tablename__ = "billing_invoices"

    id                     = Column(Integer, primary_key=True, index=True)
    organization_id        = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    subscription_id        = Column(Integer, ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True)
    stripe_invoice_id      = Column(String(120), nullable=True)
    invoice_number         = Column(String(60),  nullable=False, index=True)
    billing_period_start   = Column(DateTime(timezone=True), nullable=True)
    billing_period_end     = Column(DateTime(timezone=True), nullable=True)
    subtotal               = Column(Float,    default=0.0)
    tax_amount             = Column(Float,    default=0.0)
    discount_amount        = Column(Float,    default=0.0)
    total_amount           = Column(Float,    default=0.0)
    currency               = Column(String(10), default="USD")
    status                 = Column(String(30), default="issued")   # draft|issued|paid|pending|failed|past_due|voided|refunded
    due_date               = Column(DateTime(timezone=True), nullable=True)
    paid_date              = Column(DateTime(timezone=True), nullable=True)
    pdf_url                = Column(Text, nullable=True)
    created_at             = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at             = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingInvoice {self.invoice_number} {self.status} {self.total_amount}>"


class BillingPaymentMethod(Base):
    __tablename__ = "billing_payment_methods"

    id                          = Column(Integer, primary_key=True, index=True)
    organization_id             = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    provider                    = Column(String(40), default="stripe")
    stripe_payment_method_id    = Column(String(120), nullable=True)
    token_reference             = Column(String(160), nullable=True)
    payment_type                = Column(String(40), default="card")
    brand                       = Column(String(40), nullable=True)
    last_four                   = Column(String(4),  nullable=True)
    expiry_month                = Column(Integer,    nullable=True)
    expiry_year                 = Column(Integer,    nullable=True)
    billing_name                = Column(String(160), nullable=True)
    billing_address_json        = Column(JSON,       default=dict)
    is_default                  = Column(Boolean,    default=False)
    verification_status         = Column(String(30), default="verified")
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingPaymentMethod {self.brand} ****{self.last_four}>"


class BillingContact(Base):
    __tablename__ = "billing_contacts"

    id                          = Column(Integer, primary_key=True, index=True)
    organization_id             = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    name                        = Column(String(160), nullable=False)
    email                       = Column(String(160), nullable=False)
    role                        = Column(String(80),  default="Billing Admin")
    receives_invoices           = Column(Boolean, default=True)
    receives_payment_alerts     = Column(Boolean, default=True)
    receives_renewal_notices    = Column(Boolean, default=True)
    receives_tax_documents      = Column(Boolean, default=False)
    is_primary                  = Column(Boolean, default=False)
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingContact {self.email} primary={self.is_primary}>"


class BillingTaxProfile(Base):
    __tablename__ = "billing_tax_profiles"

    id                  = Column(Integer, primary_key=True, index=True)
    organization_id     = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, unique=True, index=True)
    legal_entity_name   = Column(String(200), nullable=True)
    trading_name        = Column(String(200), nullable=True)
    billing_address     = Column(Text,        nullable=True)
    city                = Column(String(120), nullable=True)
    state               = Column(String(120), nullable=True)
    postal_code         = Column(String(40),  nullable=True)
    country             = Column(String(80),  nullable=True)
    tax_id_label        = Column(String(40),  nullable=True)   # VAT / EIN / GST / ABN
    tax_id_value        = Column(String(80),  nullable=True)
    tax_exempt          = Column(Boolean,     default=False)
    exempt_certificate_url = Column(Text,     nullable=True)
    invoice_currency    = Column(String(10),  default="USD")
    updated_at          = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingTaxProfile {self.organization_id} {self.country}>"


class BillingDowngradeRequest(Base):
    __tablename__ = "billing_downgrade_requests"

    id                          = Column(Integer, primary_key=True, index=True)
    organization_id             = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    current_plan_id             = Column(Integer, ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True)
    requested_plan_id           = Column(Integer, ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True)
    requested_by_user_id        = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason                      = Column(Text, nullable=False)
    preferred_effective_date    = Column(DateTime(timezone=True), nullable=True)
    acknowledged_risk           = Column(Boolean, default=False)
    status                      = Column(String(30), default="pending")  # pending | reviewed | applied | rejected
    reviewed_by_user_id         = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    review_notes                = Column(Text, nullable=True)
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingDowngradeRequest {self.id} {self.status}>"


class BillingEnterpriseQuote(Base):
    __tablename__ = "billing_enterprise_quotes"

    id                          = Column(Integer, primary_key=True, index=True)
    organization_id             = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    company_name                = Column(String(200), nullable=False)
    work_email                  = Column(String(160), nullable=False)
    user_count                  = Column(String(80),  nullable=True)
    deployment_preference       = Column(String(40),  nullable=True)  # SaaS / Private Cloud / On-Premise / Not Sure
    required_integrations       = Column(Text, nullable=True)
    compliance_requirements     = Column(Text, nullable=True)
    notes                       = Column(Text, nullable=True)
    requested_by_user_id        = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status                      = Column(String(30), default="submitted")  # submitted | contacted | won | lost
    created_at                  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at                  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BillingEnterpriseQuote {self.company_name} {self.status}>"


class BillingAuditEvent(Base):
    __tablename__ = "billing_audit_events"

    id                  = Column(Integer, primary_key=True, index=True)
    organization_id     = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_user_id       = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_name          = Column(String(160), nullable=True)
    actor_email         = Column(String(160), nullable=True)
    action_key          = Column(String(80), nullable=False, index=True)
    summary             = Column(Text, nullable=True)
    details_json        = Column(JSON, default=dict)
    ip_address          = Column(String(45), nullable=True)
    user_agent_hash     = Column(String(64), nullable=True)
    created_at          = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<BillingAuditEvent {self.action_key} actor={self.actor_user_id}>"


class BillingDunningNotice(Base):
    """One row per dunning warning actually sent.

    The dunning sweep runs hourly, so without this the day-1 email would be sent 24
    times on day 1. ``UNIQUE (subscription_id, day_marker)`` makes a duplicate
    impossible at the database level rather than by a Python check.

    Rows are deleted when ``BillingSubscription.dunning_started_at`` is cleared on
    payment recovery, so a genuinely new failure cycle can warn again.
    """

    __tablename__ = "billing_dunning_notices"

    id                  = Column(Integer, primary_key=True, index=True)
    subscription_id     = Column(Integer, ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False)
    organization_id     = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    # day_1 | day_3 | day_7 | day_14
    day_marker          = Column(String(20), nullable=False)
    status              = Column(String(20), nullable=False, default="sent")
    recipient_email     = Column(String(160), nullable=True)
    last_error          = Column(Text, nullable=True)
    # The anchor this notice belongs to — kept for audit after a reset.
    dunning_started_at  = Column(DateTime(timezone=True), nullable=True)
    sent_at             = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("subscription_id", "day_marker",
                         name="uq_billing_dunning_notices_subscription_marker"),
    )

    def __repr__(self):
        return (f"<BillingDunningNotice sub={self.subscription_id} "
                f"{self.day_marker} {self.status}>")


class BillingWebhookEvent(Base):
    """Stripe webhook idempotency ledger — one row per Stripe event.id.

    ``stripe_event_id`` carries a database-level UNIQUE constraint, and that
    constraint (not any Python check) is what makes concurrent duplicate
    deliveries safe: both racing requests attempt the INSERT and exactly one
    survives. See app/billing/webhooks.py for the claim protocol.

    Billing-owned: no relationship to app/commercial, and ``organization_id`` is
    the canonical INTEGER tenant id. It stays NULL when the tenant cannot be
    resolved safely — the handler never guesses a tenant.
    """

    __tablename__ = "billing_webhook_events"

    id                  = Column(Integer, primary_key=True, index=True)
    # Uniqueness is declared once, in __table_args__ below, with the same name the
    # migration creates — so create_all() and Alembic produce identical schemas.
    stripe_event_id     = Column(String(160), nullable=False)
    event_type          = Column(String(80), nullable=False, index=True)
    api_version         = Column(String(40), nullable=True)
    # event.created at Stripe — diagnostic only. Deliberately not an ordering
    # watermark: authoritative state comes from re-fetching Stripe (K2).
    stripe_created_at   = Column(DateTime(timezone=True), nullable=True)
    organization_id     = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    # received | processed | failed | dead_letter
    status              = Column(String(20), nullable=False, default="received", index=True)
    attempts            = Column(Integer, nullable=False, default=0)
    last_error          = Column(Text, nullable=True)
    # Redacted subset for diagnostics. Never replayed as a source of truth.
    payload             = Column(JSON, nullable=True)
    received_at         = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    processed_at        = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("stripe_event_id", name="uq_billing_webhook_events_stripe_event_id"),
    )

    def __repr__(self):
        return (f"<BillingWebhookEvent {self.stripe_event_id} {self.event_type} "
                f"{self.status} attempts={self.attempts}>")


# ──────────────────────────────────────────────
# ORGANISATION SETTINGS
# ──────────────────────────────────────────────
class OrgSettings(Base):
    __tablename__ = "org_settings"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    company_name    = Column(String(255), default="Temveris")
    logo_path       = Column(Text, nullable=True)
    logo_url        = Column(Text, nullable=True)

    slug                 = Column(String(100), nullable=True)
    internal_description = Column(Text,        nullable=True)
    primary_country      = Column(String(2),   nullable=True)  # ISO 3166-1 alpha-2
    default_timezone     = Column(String(64),  nullable=True)  # IANA tz string
    default_currency     = Column(String(3),   nullable=True)  # ISO 4217

    # Contact & company profile
    hr_email            = Column(String(255), nullable=True)
    support_email       = Column(String(255), nullable=True)
    company_address     = Column(Text,        nullable=True)
    company_phone       = Column(String(50),  nullable=True)
    company_website     = Column(String(255), nullable=True)
    company_description = Column(Text,        nullable=True)

    # Branding
    primary_brand_color   = Column(String(7), nullable=True)  # #RRGGBB
    secondary_brand_color = Column(String(7), nullable=True)  # #RRGGBB

    # Workspace ownership. Nullable so that an unmigrated workspace
    # falls back to "first admin" derivation in the API layer.
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_since   = Column(DateTime(timezone=True), nullable=True)

    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_user_id])

    def __repr__(self):
        return f"<OrgSettings {self.company_name}>"


# ──────────────────────────────────────────────
# TEAMS
# ──────────────────────────────────────────────
class Team(Base):
    __tablename__ = "teams"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    name            = Column(String(100), nullable=False)
    head_user_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_teams_org_name"),
    )

    def __repr__(self):
        return f"<Team org={self.organization_id} {self.name}>"


# ──────────────────────────────────────────────
# HOLIDAYS
# ──────────────────────────────────────────────
from sqlalchemy import Date

class Holiday(Base):
    __tablename__ = "holidays"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(255), nullable=False)
    date            = Column(Date, nullable=False)
    holiday_type    = Column(String(50), default="Public")
    description     = Column(Text, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # Names a calendar within the org so one tenant can run several (per country,
    # per team). NULL is the org's single default calendar — nothing writes this
    # yet; it exists so multiple calendars need no schema redesign later.
    # Migration f1a9d4c72e86.
    calendar_key = Column(String(100), nullable=True)

    # ISO 3166-1 alpha-2 of the calendar this holiday belongs to. Migration
    # m7n2p5q8r1t4.
    #
    # NULL means UNSCOPED: a holiday that predates country-scoped calendars, or
    # one an admin deliberately applies org-wide. Unscoped rows stay visible
    # under every country filter, so adding this column could not hide any
    # existing holiday from attendance, payroll or the employee calendar.
    # Rows are NOT backfilled by the migration — see
    # `scripts/holidays_country_report.py` for the deliberate, reversible
    # classification path, and the audit finding about US-country organizations
    # that hold an Indian calendar for why guessing here would be wrong.
    #
    # `calendar_key` was evaluated first and kept as-is: it names a calendar
    # *within* a country (per team, per site), which is a different axis from
    # the country itself. Overloading it would have made "org 1's India
    # calendar" and "org 1's night-shift calendar" the same concept.
    country_code = Column(String(2), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Real uniqueness is the expression index uq_holidays_org_calendar_date
    # created in the migration — (organization_id, COALESCE(calendar_key,''),
    # COALESCE(country_code,''), date, name). A plain UniqueConstraint cannot
    # express COALESCE; declared here for SQLite/create_all parity only, with
    # the migration authoritative on PostgreSQL.
    #
    # `country_code` is in the key so one organization can hold both the US and
    # the Indian calendar — 2026-07-04 exists in each. `name` is in the key
    # because two distinct holidays legitimately share a date (Gandhi Jayanti
    # and Dussehra both fell on 2025-10-02), which the old date-only rule made
    # unstorable. Both additions widen the key, so no row that satisfied the
    # previous constraint can violate this one.
    __table_args__ = (
        UniqueConstraint("organization_id", "calendar_key", "country_code", "date", "name",
                         name="uq_holidays_org_calendar_country_date_name"),
    )

    def __repr__(self):
        return f"<Holiday {self.name} on {self.date}>"


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(255), nullable=False)

    is_active = Column(Boolean, default=True)

    # "internal" | "customer" — who pays for Temveris.
    #
    # An internal tenant (the platform owner, running the product it sells) gets
    # full plan entitlements but must never reach a Stripe write path. Defaults to
    # "customer" so a new organization bills unless deliberately exempted —
    # forgetting the flag can never give a paying client free service.
    #
    # Lives here rather than on billing_subscriptions because the classification
    # must exist before a subscription row does and must survive that row being
    # replaced. See app/billing/service.py::is_internal_tenant.
    billing_type = Column(String(20), nullable=False, default="customer",
                          server_default="customer", index=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False
    )

    isolation_mode = Column(String(50), default="shared_db")

    status = Column(String(50), default="active")

    region_code = Column(String(50), default="ap-south-1")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False
    )

    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False
    )

    name = Column(String(255), nullable=False)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
class Membership(Base):
    __tablename__ = "memberships"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False
    )

    role = Column(String(100), nullable=False)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


# ──────────────────────────────────────────────
# TIME ADJUSTMENT
# ──────────────────────────────────────────────
class TimeAdjustment(Base):
    __tablename__ = "time_adjustments"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    adjustment_date = Column(Date, nullable=False, index=True)
    seconds_added   = Column(Integer, nullable=False)
    reason          = Column(Text, nullable=False)

    created_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    is_active       = Column(Boolean, default=True, nullable=False, index=True)
    reverted_by     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reverted_at     = Column(DateTime(timezone=True), nullable=True)

    user        = relationship("User", foreign_keys=[user_id])
    creator     = relationship("User", foreign_keys=[created_by])
    reverter    = relationship("User", foreign_keys=[reverted_by])

    __table_args__ = (
        Index("ix_time_adjustments_user_date", "user_id", "adjustment_date"),
    )

    def __repr__(self):
        return f"<TimeAdjustment user={self.user_id} date={self.adjustment_date} +{self.seconds_added}s>"
