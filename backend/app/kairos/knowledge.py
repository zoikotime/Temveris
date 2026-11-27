"""
Knowledge-base loader. Reads `data/knowledge.json` once at import time and
exposes the normalised intent list, default suggestions, quick actions, and
welcome message — same shape the Node service produced.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent / "data" / "knowledge.json"

_DEFAULT_DOC: dict[str, Any] = {
    "_meta": {"assistantName": "Kairos", "product": "Temveris"},
    "intents": [],
    "fallback": "Knowledge base failed to load",
    "default_suggestions": [],
    "search_redirects": [],
}


def _load_doc() -> dict[str, Any]:
    try:
        with _DATA_PATH.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        intent_count = len(doc.get("intents") or [])
        logger.info("Kairos knowledge loaded: %d intents", intent_count)
        return doc
    except Exception as exc:
        logger.error("Failed to load Kairos knowledge: %s", exc)
        return _DEFAULT_DOC


KNOWLEDGE: dict[str, Any] = _load_doc()


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    out = re.sub(r"[^\w\s]", " ", value.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", out).strip()


def _slugify(value: str) -> str:
    slug = _normalize_text(value).replace(" ", "-")
    return slug or str(uuid.uuid4())


def _first_name(user_name: str | None) -> str:
    """Pull a usable first-name from a full-name string. Empty / blank → ''."""
    if not user_name:
        return ""
    parts = user_name.strip().split()
    return parts[0] if parts else ""


def _personalize(text: str, *, user_name: str | None = None) -> str:
    name = (KNOWLEDGE.get("_meta") or {}).get("assistantName") or "Kairos"
    # Rename the ASSISTANT only. There used to be a second pass rewriting
    # "Temveris assistant" -> "<name> assistant", which turned the greeting
    # into "I am Kairos, your Kairos assistant" — it clobbered the PRODUCT name
    # with the assistant name. "Temveris" is the product and must survive a
    # rename: an assistant called "Ava" is still "your Temveris assistant".
    out = text.replace("Kairos", name)
    # Optional `{first_name}` token so knowledge.json (or runtime fallbacks)
    # can inject the user's name without each caller having to format it.
    first = _first_name(user_name)
    out = out.replace("{first_name}", first or "there")
    return out


def _extract_question(intent: dict[str, Any]) -> str:
    if intent.get("question"):
        return intent["question"]
    if intent.get("title"):
        return intent["title"]
    keywords = intent.get("keywords") or []
    if keywords:
        return keywords[0]
    return "General help"


def _build_intents() -> list[dict[str, Any]]:
    raw = KNOWLEDGE.get("intents") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not entry or not entry.get("response"):
            continue
        question = _extract_question(entry)
        keywords = list(entry.get("keywords") or [])
        # Pre-normalise keywords + question ONCE at load time. The scorer used
        # to call normalize_text/_tokenize on every keyword for every chat
        # message — for 66 intents × ~27 keywords each that was ~1.8k regex
        # passes per request. Doing it once shaves the bulk of that work.
        kw_norm   = [_normalize_text(k) for k in keywords]
        kw_tokens = [[t for t in nk.split(" ") if t] for nk in kw_norm]
        q_norm    = _normalize_text(question)
        q_tokens  = [t for t in q_norm.split(" ") if t]
        out.append({
            "id":        entry.get("id") or _slugify(question),
            "question":  question,
            "answer":    _personalize(entry["response"]),
            "keywords":  keywords,
            "category":  entry.get("category") or "general",
            "raw":       entry,
            "kw_norm":   kw_norm,
            "kw_tokens": kw_tokens,
            "q_norm":    q_norm,
            "q_tokens":  q_tokens,
        })
    return out


INTENTS: list[dict[str, Any]] = _build_intents()


def _build_default_suggestions() -> list[str]:
    raw = KNOWLEDGE.get("default_suggestions")
    if isinstance(raw, list) and raw:
        return list(raw)
    return [intent["question"] for intent in INTENTS[:6]]


DEFAULT_SUGGESTIONS: list[str] = _build_default_suggestions()


def _build_quick_actions() -> list[dict[str, Any]]:
    raw = KNOWLEDGE.get("search_redirects") or []
    out: list[dict[str, Any]] = []
    for item in raw[:6]:
        label = item.get("actionLabel") or item.get("prompt") or item.get("intentId") or "Open"
        prompt = item.get("prompt") or item.get("actionLabel") or "Open section"
        out.append({
            "id":      item.get("id"),
            "label":   label,
            "prompt":  prompt,
            "message": item.get("prompt") or item.get("actionLabel") or item.get("intentId") or "Help",
        })
    return out


QUICK_ACTIONS: list[dict[str, Any]] = _build_quick_actions()


def _find_greeting_intent() -> dict[str, Any] | None:
    triggers = {"hi", "hello", "hey", "help", "start"}
    for intent in INTENTS:
        for kw in intent["keywords"]:
            if _normalize_text(kw) in triggers:
                return intent
    return INTENTS[0] if INTENTS else None


GREETING_INTENT = _find_greeting_intent()


def build_welcome_message(user_name: str | None = None) -> str:
    if GREETING_INTENT and GREETING_INTENT.get("answer"):
        msg = GREETING_INTENT["answer"]
    else:
        msg = _personalize(KNOWLEDGE.get("fallback") or "How can I help you today?")
    # Swap the generic "Hey there 👋" greeting for the user's first name
    # when we have one. Keeps the rest of the welcome intent untouched so
    # the spec-managed copy stays intact.
    first = _first_name(user_name)
    if first:
        msg = msg.replace("Hey there", f"Hey {first}")
    return msg


def chat_context() -> dict[str, Any]:
    """Mirror of Node `getChatContext` — feeds the React widget header."""
    welcome = build_welcome_message()
    quick_actions = QUICK_ACTIONS or [
        {"id": _slugify(item), "label": item, "prompt": item, "message": item}
        for item in DEFAULT_SUGGESTIONS[:6]
    ]
    return {
        "assistantName":       _personalize((KNOWLEDGE.get("_meta") or {}).get("assistantName") or "Kairos"),
        "productName":         (KNOWLEDGE.get("_meta") or {}).get("product") or "Temveris",
        "assistantBadge":      "AI WORK ASSISTANT",
        "statusText":          "Live knowledge base active",
        "welcomeMessage":      welcome,
        "welcomeMessageHi":    welcome,
        "quickActions":        quick_actions,
        "defaultSuggestions":  DEFAULT_SUGGESTIONS[:6],
        "retentionHours":      24,
    }


# Re-exported helpers used by intent_matcher.
def normalize_text(value: str) -> str:
    # Unicode-aware normalisation so non-Latin chars don't get stripped
    # before comparison (Hindi, Arabic, etc.).
    return _normalize_text(unicodedata.normalize("NFKC", value or ""))


def personalize(text: str, *, user_name: str | None = None) -> str:
    return _personalize(text, user_name=user_name)


def _prepend_personal_opener(body: str, user_name: str | None) -> str:
    """Prepends a short, warm opener using the user's first name so fallback
    and near-miss replies don't read like a stock error message. Only adds
    the opener when we actually have a name — otherwise returns `body`
    untouched so non-authenticated paths stay neutral.

    The body's original capitalization is preserved (no lowercasing) so the
    pronoun "I" and other proper nouns at the start of the sentence aren't
    mangled by the join.
    """
    first = _first_name(user_name)
    if not first or not body:
        return body
    return f"Hey {first} — {body}"


def fallback_text(user_name: str | None = None) -> str:
    body = KNOWLEDGE.get("fallback") or (
        "I didn't quite catch that — could you tell me what you're trying to do?"
    )
    return _prepend_personal_opener(body, user_name)


def near_miss_fallback_text(user_name: str | None = None) -> str:
    """Soft prompt for borderline matches — shown when the scorer has *some*
    signal but isn't confident. The suggestions list gets populated with the
    top-3 candidate intents so the user can disambiguate with one tap.
    """
    body = KNOWLEDGE.get("near_miss_fallback") or (
        "I'm not 100% sure I understood — did you mean one of these?"
    )
    return _prepend_personal_opener(body, user_name)


def email_manager_prompt() -> dict[str, Any] | None:
    return KNOWLEDGE.get("email_manager_prompt")
