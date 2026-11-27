"""
Temveris – App productivity categorisation
===========================================
Maps a desktop-app name → one of {productive, neutral, distracting, blocked}.

Resolution order (first hit wins):
    1. Per-org override in `app_categories` table (admin configurable).
    2. Hard-coded defaults below — curated for "most teams most of the time".
    3. Fallback: "neutral".

App names come from the desktop tracker (`Activity.app_name`) and can vary in
casing / extension (`Code.exe` vs `code.exe` vs `Visual Studio Code`). They
are normalised to lowercase, stripped of `.exe` and trimmed before lookup.

Mirrors the structure of `app/browser/productivity.py` so admins reading
either file can immediately understand the other.
"""
from __future__ import annotations

from typing import Dict, Optional
from sqlalchemy.orm import Session

from database import models

CATEGORY_PRODUCTIVE  = "productive"
CATEGORY_NEUTRAL     = "neutral"
CATEGORY_DISTRACTING = "distracting"
CATEGORY_BLOCKED     = "blocked"
VALID_CATEGORIES     = {
    CATEGORY_PRODUCTIVE,
    CATEGORY_NEUTRAL,
    CATEGORY_DISTRACTING,
    CATEGORY_BLOCKED,
}

VALID_SOURCES = {"manual", "auto"}


# Curated defaults — keep conservative. When in doubt mark neutral so the admin
# is the one promoting/demoting via the override table.
DEFAULT_CATEGORIES: Dict[str, str] = {
    # — Productive: developer & collaboration tools —
    "code":                     CATEGORY_PRODUCTIVE,  # VS Code
    "visual studio code":       CATEGORY_PRODUCTIVE,
    "visual studio":            CATEGORY_PRODUCTIVE,
    "devenv":                   CATEGORY_PRODUCTIVE,  # VS .NET
    "rider":                    CATEGORY_PRODUCTIVE,
    "intellij idea":            CATEGORY_PRODUCTIVE,
    "idea":                     CATEGORY_PRODUCTIVE,
    "pycharm":                  CATEGORY_PRODUCTIVE,
    "pycharm64":                CATEGORY_PRODUCTIVE,
    "webstorm":                 CATEGORY_PRODUCTIVE,
    "phpstorm":                 CATEGORY_PRODUCTIVE,
    "goland":                   CATEGORY_PRODUCTIVE,
    "clion":                    CATEGORY_PRODUCTIVE,
    "android studio":           CATEGORY_PRODUCTIVE,
    "xcode":                    CATEGORY_PRODUCTIVE,
    "sublime_text":             CATEGORY_PRODUCTIVE,
    "sublime text":             CATEGORY_PRODUCTIVE,
    "atom":                     CATEGORY_PRODUCTIVE,
    "notepad++":                CATEGORY_PRODUCTIVE,
    "neovim":                   CATEGORY_PRODUCTIVE,
    "nvim":                     CATEGORY_PRODUCTIVE,
    "vim":                      CATEGORY_PRODUCTIVE,
    "emacs":                    CATEGORY_PRODUCTIVE,
    "windowsterminal":          CATEGORY_PRODUCTIVE,
    "wt":                       CATEGORY_PRODUCTIVE,
    "powershell":               CATEGORY_PRODUCTIVE,
    "pwsh":                     CATEGORY_PRODUCTIVE,
    "cmd":                      CATEGORY_PRODUCTIVE,
    "bash":                     CATEGORY_PRODUCTIVE,
    "iterm":                    CATEGORY_PRODUCTIVE,
    "iterm2":                   CATEGORY_PRODUCTIVE,
    "alacritty":                CATEGORY_PRODUCTIVE,
    "warp":                     CATEGORY_PRODUCTIVE,
    "git":                      CATEGORY_PRODUCTIVE,
    "github desktop":           CATEGORY_PRODUCTIVE,
    "githubdesktop":            CATEGORY_PRODUCTIVE,
    "sourcetree":               CATEGORY_PRODUCTIVE,
    "fork":                     CATEGORY_PRODUCTIVE,
    "docker desktop":           CATEGORY_PRODUCTIVE,
    "docker":                   CATEGORY_PRODUCTIVE,
    "postman":                  CATEGORY_PRODUCTIVE,
    "insomnia":                 CATEGORY_PRODUCTIVE,
    "bruno":                    CATEGORY_PRODUCTIVE,
    "tableplus":                CATEGORY_PRODUCTIVE,
    "dbeaver":                  CATEGORY_PRODUCTIVE,
    "pgadmin":                  CATEGORY_PRODUCTIVE,
    "mongodb compass":          CATEGORY_PRODUCTIVE,
    "datagrip":                 CATEGORY_PRODUCTIVE,

    # — Productive: design & creative —
    "figma":                    CATEGORY_PRODUCTIVE,
    "sketch":                   CATEGORY_PRODUCTIVE,
    "affinity designer":        CATEGORY_PRODUCTIVE,
    "affinity photo":           CATEGORY_PRODUCTIVE,
    "photoshop":                CATEGORY_PRODUCTIVE,
    "illustrator":              CATEGORY_PRODUCTIVE,
    "indesign":                 CATEGORY_PRODUCTIVE,
    "premiere pro":             CATEGORY_PRODUCTIVE,
    "after effects":            CATEGORY_PRODUCTIVE,
    "blender":                  CATEGORY_PRODUCTIVE,
    "canva":                    CATEGORY_PRODUCTIVE,

    # — Productive: communication & collaboration —
    "slack":                    CATEGORY_PRODUCTIVE,
    "microsoft teams":          CATEGORY_PRODUCTIVE,
    "teams":                    CATEGORY_PRODUCTIVE,
    "zoom":                     CATEGORY_PRODUCTIVE,
    "google meet":              CATEGORY_PRODUCTIVE,
    "webex":                    CATEGORY_PRODUCTIVE,
    "outlook":                  CATEGORY_PRODUCTIVE,
    "thunderbird":              CATEGORY_PRODUCTIVE,
    "gmail":                    CATEGORY_PRODUCTIVE,
    "spark":                    CATEGORY_PRODUCTIVE,
    "notion":                   CATEGORY_PRODUCTIVE,
    "obsidian":                 CATEGORY_PRODUCTIVE,
    "evernote":                 CATEGORY_PRODUCTIVE,
    "onenote":                  CATEGORY_PRODUCTIVE,
    "trello":                   CATEGORY_PRODUCTIVE,
    "asana":                    CATEGORY_PRODUCTIVE,
    "linear":                   CATEGORY_PRODUCTIVE,
    "jira":                     CATEGORY_PRODUCTIVE,
    "miro":                     CATEGORY_PRODUCTIVE,
    "loom":                     CATEGORY_PRODUCTIVE,

    # — Productive: office —
    "winword":                  CATEGORY_PRODUCTIVE,
    "word":                     CATEGORY_PRODUCTIVE,
    "excel":                    CATEGORY_PRODUCTIVE,
    "powerpoint":               CATEGORY_PRODUCTIVE,
    "powerpnt":                 CATEGORY_PRODUCTIVE,
    "onenote.exe":              CATEGORY_PRODUCTIVE,
    "libreoffice":              CATEGORY_PRODUCTIVE,
    "soffice":                  CATEGORY_PRODUCTIVE,
    "pages":                    CATEGORY_PRODUCTIVE,
    "numbers":                  CATEGORY_PRODUCTIVE,
    "keynote":                  CATEGORY_PRODUCTIVE,

    # — Productive: Temveris self —
    "temveris":                 CATEGORY_PRODUCTIVE,
    "workfolio":                CATEGORY_PRODUCTIVE,

    # — Neutral: browsers, file managers, system —
    "chrome":                   CATEGORY_NEUTRAL,
    "google chrome":            CATEGORY_NEUTRAL,
    "msedge":                   CATEGORY_NEUTRAL,
    "microsoft edge":           CATEGORY_NEUTRAL,
    "firefox":                  CATEGORY_NEUTRAL,
    "safari":                   CATEGORY_NEUTRAL,
    "opera":                    CATEGORY_NEUTRAL,
    "brave":                    CATEGORY_NEUTRAL,
    "arc":                      CATEGORY_NEUTRAL,
    "vivaldi":                  CATEGORY_NEUTRAL,
    "explorer":                 CATEGORY_NEUTRAL,  # Windows File Explorer
    "finder":                   CATEGORY_NEUTRAL,
    "settings":                 CATEGORY_NEUTRAL,
    "systemsettings":           CATEGORY_NEUTRAL,
    "calculator":               CATEGORY_NEUTRAL,
    "calc":                     CATEGORY_NEUTRAL,
    "preview":                  CATEGORY_NEUTRAL,
    "photos":                   CATEGORY_NEUTRAL,
    "snippingtool":             CATEGORY_NEUTRAL,
    "screensketch":             CATEGORY_NEUTRAL,
    "lockapp":                  CATEGORY_NEUTRAL,
    "searchapp":                CATEGORY_NEUTRAL,
    "shellexperiencehost":      CATEGORY_NEUTRAL,
    "applicationframehost":     CATEGORY_NEUTRAL,
    "dwm":                      CATEGORY_NEUTRAL,
    "desktop":                  CATEGORY_NEUTRAL,

    # — Business comms (NOT distracting) —
    # WhatsApp / Signal / Messenger are widely used for legitimate
    # work communication (especially in markets where it IS the
    # primary business comms channel). Defaulting them to neutral
    # means time spent doesn't count toward productive credit, but
    # also doesn't ding a worker's productivity score. Admins can
    # still mark them distracting per-org via app_categories table.
    "whatsapp":                 CATEGORY_NEUTRAL,
    "whatsapp.root":            CATEGORY_NEUTRAL,  # Windows WMC app name
    "whatsapprt":               CATEGORY_NEUTRAL,  # legacy WhatsApp RT
    "signal":                   CATEGORY_NEUTRAL,
    "messenger":                CATEGORY_NEUTRAL,
    "skype":                    CATEGORY_NEUTRAL,

    # — Distracting: social, entertainment, games —
    "discord":                  CATEGORY_DISTRACTING,
    "telegram":                 CATEGORY_DISTRACTING,
    "telegramdesktop":          CATEGORY_DISTRACTING,
    "spotify":                  CATEGORY_DISTRACTING,
    "apple music":              CATEGORY_DISTRACTING,
    "music":                    CATEGORY_DISTRACTING,
    "itunes":                   CATEGORY_DISTRACTING,
    "vlc":                      CATEGORY_DISTRACTING,
    "netflix":                  CATEGORY_DISTRACTING,
    "primevideo":               CATEGORY_DISTRACTING,
    "disneyplus":               CATEGORY_DISTRACTING,
    "youtube":                  CATEGORY_DISTRACTING,
    "twitch":                   CATEGORY_DISTRACTING,
    "obs":                      CATEGORY_DISTRACTING,  # streaming
    "obs64":                    CATEGORY_DISTRACTING,
    "steam":                    CATEGORY_DISTRACTING,
    "epic games":               CATEGORY_DISTRACTING,
    "epicgameslauncher":        CATEGORY_DISTRACTING,
    "battle.net":               CATEGORY_DISTRACTING,
    "league of legends":        CATEGORY_DISTRACTING,
    "valorant":                 CATEGORY_DISTRACTING,
    "minecraft":                CATEGORY_DISTRACTING,
    "fortnite":                 CATEGORY_DISTRACTING,
    "roblox":                   CATEGORY_DISTRACTING,
}


# Lightweight token-based hints — used by the auto-suggest sweep when the
# exact app name isn't in DEFAULT_CATEGORIES. Tokens are matched against the
# normalised name. First hit wins, in declaration order.
TOKEN_HINTS = (
    # productive
    ("productive", ("ide", "code", "studio", "terminal", "console", "git",
                     "compiler", "debugger", "editor", "shell", "vim", "emacs",
                     "docker", "postman", "linter", "sql", "database", "schema")),
    # distracting
    ("distracting", ("game", "play", "stream", "twitch", "tube", "music",
                      "spotify", "netflix", "video", "tiktok", "facebook",
                      "instagram", "snap", "tinder", "discord", "telegram")),
)


def _normalize_app(name: Optional[str]) -> str:
    """Lowercase, strip .exe, collapse whitespace. Returns "" for falsy input."""
    if not name:
        return ""
    n = str(name).strip().lower()
    if n.endswith(".exe"):
        n = n[: -len(".exe")]
    if n.endswith(".app"):
        n = n[: -len(".app")]
    return " ".join(n.split())


def _candidates(n: str):
    """Yield progressively-shorter candidate keys for category lookup so we
    catch real-world tracker noise like 'WhatsApp.Root', 'Code - file.py',
    or 'PyCharm 2024.1'. Order: full → first whitespace token → that token
    with dotted suffix stripped → bare alpha prefix."""
    if not n:
        return
    yield n
    head = n.split(maxsplit=1)[0] if n.split() else n
    if head and head != n:
        yield head
    if "." in head:
        yield head.split(".", 1)[0]
    # Bare alpha prefix — strips trailing digits or punctuation like
    # "pycharm64" → "pycharm", "code2" → "code".
    bare = ""
    for ch in head:
        if ch.isalpha():
            bare += ch
        else:
            break
    if bare and bare != head:
        yield bare


def categorise(
    name: Optional[str],
    overrides: Optional[Dict[str, str]] = None,
) -> str:
    """Return the category for a single app name. Resolution order, first
    hit wins: admin override → curated default — checked against multiple
    progressively-stripped candidate keys (see `_candidates`).
    Falls back to 'neutral'."""
    n = _normalize_app(name)
    if not n:
        return CATEGORY_NEUTRAL
    for key in _candidates(n):
        if overrides and key in overrides:
            return overrides[key]
        if key in DEFAULT_CATEGORIES:
            return DEFAULT_CATEGORIES[key]
    return CATEGORY_NEUTRAL


def auto_suggest(name: Optional[str]) -> Optional[str]:
    """Best-effort category for an *unreviewed* app, used for the
    'auto-suggest sweep'. Returns None when no confident match exists."""
    n = _normalize_app(name)
    if not n:
        return None
    if n in DEFAULT_CATEGORIES:
        return DEFAULT_CATEGORIES[n]
    head = n.split()[0] if n.split() else n
    if head in DEFAULT_CATEGORIES:
        return DEFAULT_CATEGORIES[head]
    for category, tokens in TOKEN_HINTS:
        for tok in tokens:
            if tok in n:
                return category
    return None  # leave it for the human reviewer


def load_overrides(db: Session, organization_id: Optional[int] = None) -> Dict[str, str]:
    """Load admin overrides from `app_categories`, scoped to organization_id.

    When organization_id is None (legacy / unauthenticated callers) all rows
    are returned for backward compatibility. Absent table = empty dict so we
    never crash startup on an older install missing the migration."""
    try:
        q = db.query(models.AppCategory)
        if organization_id is not None:
            q = q.filter(models.AppCategory.organization_id == organization_id)
        rows = q.all()
    except Exception:
        return {}
    return {
        _normalize_app(r.app_name): r.category
        for r in rows
        if r.category in VALID_CATEGORIES
    }


# Weight per category for the global productivity score.
#   productive  → 1.0 (counts in full)
#   neutral     → 0.5
#   distracting → 0.0
#   blocked     → 0.0 (also surfaces in the "Blocked Apps" alert list)
CATEGORY_WEIGHT = {
    CATEGORY_PRODUCTIVE:  1.0,
    CATEGORY_NEUTRAL:     0.5,
    CATEGORY_DISTRACTING: 0.0,
    CATEGORY_BLOCKED:     0.0,
}
