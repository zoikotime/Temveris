"""Foreground-app classification shared across routers.

Distinguishes real desktop *tools* (IDEs, Office, design apps…) from browsers
(whose usage is already captured per-visit by the extension) and from OS / idle
noise. Used by the AI summary "Top tools" view and by the tracking heartbeat,
which logs desktop-tool usage into the activity timeline so a tester who lives
in Eclipse sees tool activity, not just website visits.
"""

# Browser processes — excluded from "tools". Website usage is represented by the
# browser/domain views, so surfacing browser processes here would be redundant.
BROWSER_PROCS = {
    "chrome", "google chrome", "msedge", "microsoft edge", "edge", "brave",
    "brave browser", "firefox", "mozilla firefox", "opera", "opera gx", "safari",
    "iexplore", "internet explorer", "vivaldi", "chromium", "arc",
}

# OS shell / host / idle noise — surfaced as the foreground window momentarily
# but not something the user actively "works in".
NON_TOOL_PROCS = {
    "idle", "desktop", "lockapp", "lock screen", "searchhost",
    "startmenuexperiencehost", "shellexperiencehost", "applicationframehost",
    "unknown", "",
    "pickerhost", "textinputhost", "systemsettings", "dwm", "sihost", "ctfmon",
    "runtimebroker", "fontdrvhost", "dllhost", "smartscreen",
    "securityhealthsystray", "mcuicnt",
}


def normalize_proc(name):
    """Lowercase, trimmed, '.exe'-stripped process key for set comparisons."""
    key = (name or "").strip().lower()
    if key.endswith(".exe"):
        key = key[:-4]
    return key


def is_desktop_tool(name):
    """True for foreground apps that are real desktop tools (IDEs, Office,
    design apps…) — i.e. not browsers and not OS/idle noise."""
    key = normalize_proc(name)
    return bool(key) and key not in BROWSER_PROCS and key not in NON_TOOL_PROCS
