"""
Intent matcher — direct port of `KAIROS-BE/backend/services/chatService.js
> generateChatReply` and `scoreEntry`. Behaviour is intentionally identical
so the user-facing answer set matches the legacy bot exactly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.kairos.knowledge import (
    DEFAULT_SUGGESTIONS,
    INTENTS,
    KNOWLEDGE,
    email_manager_prompt,
    fallback_text,
    near_miss_fallback_text,
    normalize_text,
    personalize,
)


def _tokenize(text: str) -> list[str]:
    return [t for t in normalize_text(text).split(" ") if t]


# ── Phrase shortcuts ──────────────────────────────────────────────────────
# Some navigation phrases must route deterministically, regardless of how
# many other intents share common tokens with them. The token-accumulation
# scorer used to let `agent_escalation` win over `welcome` on "Back to Main
# Menu" because dozens of agent keywords contained the word "to". This
# table is consulted first and bypasses scoring on an exact phrase hit.
_PHRASE_SHORTCUTS: dict[str, str] = {
    "back to main menu":  "welcome",
    "main menu":          "welcome",
    "back to menu":       "welcome",
    "home menu":          "welcome",
    "start over":         "welcome",
    "start again":        "welcome",
    "restart":            "welcome",
    "show main menu":     "welcome",
    "go to main menu":    "welcome",
    "go back":            "welcome",
    "speak to a human":   "agent_escalation",
    "speak to a human agent": "agent_escalation",
    "human agent":        "agent_escalation",
    "talk to a human":    "agent_escalation",
    "talk to agent":      "agent_escalation",
    "talk to sales":      "book_demo",
    "book a demo":        "book_demo",
    "start free trial":   "free_trial",
    "free trial":         "free_trial",
    "pricing":            "pricing_plans",
    "plans":              "pricing_plans",
    "thank you":          "thank_you",
    "thanks":             "thank_you",
    "bye":                "goodbye",
    "goodbye":            "goodbye",
}

# Substring shortcuts — if the phrase appears ANYWHERE in the normalised
# message, route to the listed intent. Ordered by specificity (longest phrase
# wins first). These bail out the scorer for canonical commercial topics whose
# natural phrasings ("difference between organization and workspace", "support
# multi-tenancy", "configure overtime policy") otherwise lose token-count
# fights with intents like `whats_recorded` or `agent_escalation`.
_SUBSTRING_SHORTCUTS: list[tuple[str, str]] = [
    ("difference between organization and workspace", "multi_tenancy_data_isolation"),
    ("difference between organisation and workspace", "multi_tenancy_data_isolation"),
    ("organization and workspace",   "multi_tenancy_data_isolation"),
    ("organisation and workspace",   "multi_tenancy_data_isolation"),
    ("organization and a workspace", "multi_tenancy_data_isolation"),
    ("organisation and a workspace", "multi_tenancy_data_isolation"),
    ("difference between an organization", "multi_tenancy_data_isolation"),
    ("difference between an organisation", "multi_tenancy_data_isolation"),
    ("what is a workspace",          "multi_tenancy_data_isolation"),
    ("what is an organization",      "multi_tenancy_data_isolation"),
    ("what is an organisation",      "multi_tenancy_data_isolation"),
    ("organization vs workspace",    "multi_tenancy_data_isolation"),
    ("organisation vs workspace",    "multi_tenancy_data_isolation"),
    ("multi tenancy",                "multi_tenancy_data_isolation"),
    ("multitenancy",                 "multi_tenancy_data_isolation"),
    ("multi tenant",                 "multi_tenancy_data_isolation"),
    ("data isolation",               "multi_tenancy_data_isolation"),
    ("data separation",              "multi_tenancy_data_isolation"),
    ("unverified user",              "multi_tenancy_data_isolation"),
    ("provisional access",           "multi_tenancy_data_isolation"),
    ("before verification",          "multi_tenancy_data_isolation"),
    ("overtime policy",              "workspace_policies_tab"),
    ("overtime policies",            "workspace_policies_tab"),
    ("break policy",                 "workspace_policies_tab"),
    ("break policies",               "workspace_policies_tab"),
    ("configure policies",           "workspace_policies_tab"),
    ("configure policy",             "workspace_policies_tab"),
    ("workforce policies",           "workspace_policies_tab"),
    ("policy versioning",            "workspace_policies_tab"),
    ("workspace settings",           "workspace_settings_overview"),
    ("workspace setup",              "workspace_settings_overview"),
    ("admin console",                "workspace_settings_overview"),
    ("free trial",                   "free_trial"),
    ("trial period",                 "free_trial"),
    ("30 day trial",                 "free_trial"),
    ("30-day trial",                 "free_trial"),
    ("no credit card",               "free_trial"),
    ("how much does it cost",        "pricing_plans"),
    ("how much is temveris",        "pricing_plans"),
    ("plan pricing",                 "pricing_plans"),
    ("subscription cost",            "pricing_plans"),
    ("book a demo",                  "book_demo"),
    ("request a demo",               "book_demo"),
    ("schedule a demo",              "book_demo"),
    ("recalculate my payslip",       "payslip_scope_clarification"),
    ("recalculate payslip",          "payslip_scope_clarification"),
    ("does temveris do payroll",    "payslip_scope_clarification"),
    ("temveris payroll",            "payslip_scope_clarification"),
    ("best fit for",                 "best_fit_customers"),
    ("who is temveris for",         "best_fit_customers"),
    ("how do i sign up",             "commercial_signup_routes"),
    ("how to sign up",               "commercial_signup_routes"),
    ("how do i buy",                 "commercial_signup_routes"),
    ("is this surveillance",         "what_is_zoikotime"),
    ("employee surveillance",        "what_is_zoikotime"),
]


def _score_entry(message_norm: str, message_tokens: set[str], entry: dict[str, Any]) -> float:
    """Scorer using pre-normalised entry data. `message_norm` is the lowercased,
    punctuation-stripped message; `message_tokens` is the set of its tokens."""
    score = 0.0

    if entry["q_norm"] and entry["q_norm"] in message_norm:
        score += 10.0

    kw_norm   = entry["kw_norm"]
    kw_tokens = entry["kw_tokens"]
    for nk, kt in zip(kw_norm, kw_tokens):
        if not nk:
            continue
        if nk in message_norm:
            score += 5.0
            continue
        if kt:
            matched = sum(1 for t in kt if t in message_tokens)
            ratio = matched / len(kt)
            if ratio >= 0.5:
                score += ratio * 3.5
            if matched:
                score += matched * 1.25

    for token in entry["q_tokens"]:
        if token in message_tokens:
            score += 0.85

    return score


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fallback_reply(user_name: str | None = None) -> dict[str, Any]:
    return {
        "id":              str(uuid.uuid4()),
        "answer":          personalize(fallback_text(user_name), user_name=user_name),
        "matchedQuestion": "Fallback response",
        "confidence":      0.24,
        "suggestions":     DEFAULT_SUGGESTIONS[:3],
        "route":           None,
        "intent":          "fallback",
        "timestamp":       _now_iso(),
    }


def _near_miss_reply(ranked_top: list[dict[str, Any]], user_name: str | None = None) -> dict[str, Any]:
    """Soft fallback for borderline matches — instead of dumping the full
    main menu, surface the top 3 candidate questions as tappable suggestions
    so the user can pick the intended intent in one click.

    Used when the best score is in the "I'm-not-sure" band (0.6–1.5). Below
    0.6 we have no real signal and use the full fallback menu; above 1.5 we
    trust the match outright.
    """
    return {
        "id":              str(uuid.uuid4()),
        "answer":          personalize(near_miss_fallback_text(user_name), user_name=user_name),
        "matchedQuestion": "Did you mean…",
        "confidence":      0.35,
        "suggestions":     [t["question"] for t in ranked_top[:3]] or DEFAULT_SUGGESTIONS[:3],
        "route":           None,
        "intent":          "near_miss",
        "timestamp":       _now_iso(),
    }


def _reply_from_intent(entry: dict[str, Any], confidence: float, ranked_tail: list | None = None, user_name: str | None = None) -> dict[str, Any]:
    raw_intent = entry.get("raw") or {}
    suggestions = (
        list(raw_intent["suggestions"])
        if isinstance(raw_intent.get("suggestions"), list)
        else ([t["question"] for t in (ranked_tail or [])[:3]] if ranked_tail else [])
    )
    # Run the static answer through `personalize` so any `{first_name}`
    # tokens (added to knowledge.json over time) resolve, and so the assistant
    # name substitution stays consistent. For pre-tokenised legacy answers
    # this is a near no-op.
    answer = personalize(entry["answer"], user_name=user_name)
    return {
        "id":              str(uuid.uuid4()),
        "answer":          answer,
        "matchedQuestion": entry["question"],
        "confidence":      confidence,
        "suggestions":     suggestions,
        "route":           raw_intent.get("route"),
        "intent":          entry["id"],
        "timestamp":       _now_iso(),
    }


def generate_chat_reply(message: str, user_name: str | None = None) -> dict[str, Any]:
    """Mirror of Node `generateChatReply(message, language)`. Language is
    accepted by the caller but the underlying matcher already returns the
    canonical answer; localisation happens in the knowledge file itself.

    `user_name` lets fallback / near-miss copy address the user by first
    name. Static intent answers are unchanged unless they contain a
    `{first_name}` token.
    """
    normalized = normalize_text(message)
    msg_tokens = set(normalized.split(" ")) - {""}

    # 1. Email-manager keyword shortcut.
    em = email_manager_prompt()
    if em and isinstance(em.get("trigger_keywords"), list):
        for keyword in em["trigger_keywords"]:
            nk = normalize_text(keyword)
            if nk and nk in normalized:
                return {
                    "id":              str(uuid.uuid4()),
                    "answer":          personalize(em.get("response", ""), user_name=user_name),
                    "matchedQuestion": "Email manager",
                    "confidence":      0.98,
                    "suggestions":     DEFAULT_SUGGESTIONS[:3],
                    "route":           None,
                    "intent":          "email_manager",
                    "timestamp":       _now_iso(),
                }

    if not INTENTS:
        return _fallback_reply(user_name)

    # 2. Phrase shortcuts — exact navigational phrases route deterministically
    #    instead of fighting the token-accumulation scorer.
    target_intent_id = _PHRASE_SHORTCUTS.get(normalized)
    if target_intent_id:
        for entry in INTENTS:
            if entry["id"] == target_intent_id:
                return _reply_from_intent(entry, confidence=0.99, user_name=user_name)

    # 3. Substring shortcuts — canonical commercial phrasings that lose
    #    token-count fights with high-keyword-count legacy intents.
    for phrase, intent_id in _SUBSTRING_SHORTCUTS:
        if phrase in normalized:
            for entry in INTENTS:
                if entry["id"] == intent_id:
                    return _reply_from_intent(entry, confidence=0.99, user_name=user_name)
            break

    ranked = sorted(
        ((entry, _score_entry(normalized, msg_tokens, entry)) for entry in INTENTS),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best, best_score = ranked[0]
    # Graded fallback: below 0.6 we have nothing, so show the main menu.
    # In the 0.6–1.5 band we have *some* signal — surface the top 3 candidates
    # as suggestions instead of forcing the user to re-navigate the menu.
    if best_score < 0.6:
        return _fallback_reply(user_name)
    if best_score < 1.5:
        return _near_miss_reply([p[0] for p in ranked[:3] if p[1] > 0], user_name=user_name)

    confidence = min(0.99, round(best_score / 16.0, 2))
    return _reply_from_intent(best, confidence, ranked_tail=[p[0] for p in ranked[1:4]], user_name=user_name)
