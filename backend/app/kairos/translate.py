"""
Async translation pipeline. Port of `KAIROS-BE/backend/utils/translate.js`.

Strategy: Lingva (multiple instances, free) → MyMemory (free) fallback.
No API keys required.

Local language detection is simplified vs the Node version: we rely on
script/Unicode-block patterns (which catch the cases that matter most —
Hindi, Arabic, CJK, Cyrillic, Hebrew, Thai, etc.) and defer to the API's
detected-language label for Latin-script content. The original Node module
shipped a 500-line Latin-keyword table; that level of fidelity isn't
required for parity with the support-email flow.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_LINGVA_INSTANCES = [
    s.strip()
    for s in (
        os.getenv(
            "LINGVA_INSTANCES",
            "https://lingva.ml,https://translate.igna.wtf,https://translate.plausibility.cloud",
        ).split(",")
    )
    if s.strip()
]
_MYMEMORY_EMAIL = os.getenv("MYMEMORY_EMAIL") or os.getenv("SMTP_USER") or ""
_TIMEOUT_S = 5.0
_MAX_CHUNK = 900


# ── Language code ↔ name maps (from translate.js) ─────────────────────────

LANG_NAME_TO_CODE: dict[str, str] = {
    "afrikaans": "af", "albanian": "sq", "amharic": "am", "arabic": "ar",
    "armenian": "hy", "azerbaijani": "az", "basque": "eu", "belarusian": "be",
    "bengali": "bn", "bosnian": "bs", "bulgarian": "bg", "catalan": "ca",
    "chinese (simplified)": "zh-cn", "chinese (traditional)": "zh-tw",
    "chinese": "zh", "croatian": "hr", "czech": "cs", "danish": "da",
    "dutch": "nl", "english": "en", "estonian": "et", "finnish": "fi",
    "french": "fr", "galician": "gl", "georgian": "ka", "german": "de",
    "greek": "el", "gujarati": "gu", "haitian creole": "ht", "hausa": "ha",
    "hebrew": "he", "hindi": "hi", "hungarian": "hu", "icelandic": "is",
    "igbo": "ig", "indonesian": "id", "irish": "ga", "italian": "it",
    "japanese": "ja", "kannada": "kn", "kazakh": "kk", "khmer": "km",
    "korean": "ko", "kurdish": "ku", "kyrgyz": "ky", "lao": "lo",
    "latvian": "lv", "lithuanian": "lt", "luxembourgish": "lb",
    "macedonian": "mk", "malagasy": "mg", "malay": "ms", "malayalam": "ml",
    "maltese": "mt", "maori": "mi", "marathi": "mr", "mongolian": "mn",
    "myanmar (burmese)": "my", "burmese": "my", "nepali": "ne",
    "norwegian": "no", "pashto": "ps", "persian": "fa", "polish": "pl",
    "portuguese": "pt", "punjabi": "pa", "romanian": "ro", "russian": "ru",
    "samoan": "sm", "serbian": "sr", "sinhala": "si", "slovak": "sk",
    "slovenian": "sl", "somali": "so", "spanish": "es", "sundanese": "su",
    "swahili": "sw", "swedish": "sv", "tagalog (filipino)": "tl",
    "tagalog": "tl", "filipino": "tl", "tajik": "tg", "tamil": "ta",
    "tatar": "tt", "telugu": "te", "thai": "th", "turkish": "tr",
    "turkmen": "tk", "ukrainian": "uk", "urdu": "ur", "uyghur": "ug",
    "uzbek": "uz", "vietnamese": "vi", "welsh": "cy", "xhosa": "xh",
    "yiddish": "yi", "yoruba": "yo", "zulu": "zu",
}

_CODE_TO_NAME: dict[str, str] = {v: k.title() for k, v in LANG_NAME_TO_CODE.items()}


def get_lang_code(lang_name: str) -> str:
    return LANG_NAME_TO_CODE.get((lang_name or "").lower(), "unknown")


def normalize_language_label(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "Unknown"
    lowered = raw.lower()
    if lowered in {"unknown", "auto"}:
        return "Unknown"
    code = LANG_NAME_TO_CODE.get(lowered)
    if code:
        return _CODE_TO_NAME.get(code, raw.title())
    if lowered in _CODE_TO_NAME:
        return _CODE_TO_NAME[lowered]
    return raw.title()


# ── Local script/Unicode detection ────────────────────────────────────────

_SCRIPT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Korean",            re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")),
    ("Japanese",          re.compile(r"[぀-ヿ]")),
    ("Chinese",           re.compile(r"[一-鿿]")),
    ("Thai",              re.compile(r"[฀-๿]")),
    ("Lao",               re.compile(r"[຀-໿]")),
    ("Khmer",             re.compile(r"[ក-៿]")),
    ("Myanmar (Burmese)", re.compile(r"[က-႟]")),
    ("Sinhala",           re.compile(r"[඀-෿]")),
    ("Georgian",          re.compile(r"[Ⴀ-ჿ]")),
    ("Armenian",          re.compile(r"[԰-֏]")),
    ("Amharic",           re.compile(r"[ሀ-፿]")),
    ("Hebrew",            re.compile(r"[א-׿]")),
    ("Arabic",            re.compile(r"[؀-ۿ]")),
    ("Telugu",            re.compile(r"[ఀ-౿]")),
    ("Tamil",             re.compile(r"[஀-௿]")),
    ("Kannada",           re.compile(r"[ಀ-೿]")),
    ("Malayalam",         re.compile(r"[ഀ-ൿ]")),
    ("Bengali",           re.compile(r"[ঀ-৿]")),
    ("Gujarati",          re.compile(r"[઀-૿]")),
    ("Punjabi",           re.compile(r"[਀-੿]")),
    ("Hindi",             re.compile(r"[ऀ-ॿ]")),
    ("Russian",           re.compile(r"[Ѐ-ӿ]")),  # Cyrillic catch-all
]


def detect_language_from_text(value: str) -> str:
    """Best-effort script-based detection. Returns 'Unknown' for Latin
    scripts (caller should defer to the translation API's label)."""
    if not value:
        return "Unknown"
    for name, pattern in _SCRIPT_PATTERNS:
        if pattern.search(value):
            return name
    return "Unknown"


def _is_likely_english(value: str) -> bool:
    clean = (value or "").strip()
    if not clean:
        return False
    non_ascii = sum(1 for ch in clean if ord(ch) > 127)
    return (1 - non_ascii / len(clean)) > 0.95


def _normalize_for_compare(value: str) -> str:
    out = re.sub(r"[^\w\s]", " ", (value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", out).strip()


def _resolve_language_name(raw_lang: str, original: str, translated: str) -> str:
    guessed = detect_language_from_text(original)
    if guessed.lower() != "unknown":
        return guessed
    normalized = normalize_language_label(raw_lang or "")
    if normalized.lower() != "unknown":
        return normalized
    if (
        _normalize_for_compare(original) == _normalize_for_compare(translated)
        and _is_likely_english(original)
    ):
        return "English"
    return "Unknown"


# ── Chunking ──────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    sentences = re.findall(r"[^.!?\n]+[.!?\n]*", text) or [text]
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(current) + len(s) > max_len and current:
            chunks.append(current.strip())
            current = s
        else:
            current += s
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ── Provider calls ────────────────────────────────────────────────────────

async def _lingva(client: httpx.AsyncClient, text: str) -> dict[str, str]:
    encoded = quote(text, safe="")
    last_err: Exception | None = None
    for base in _LINGVA_INSTANCES:
        try:
            resp = await client.get(f"{base}/api/v1/auto/en/{encoded}", timeout=_TIMEOUT_S)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("error"):
                continue
            return {
                "translated": data.get("translation", text),
                "lang": (data.get("info", {}).get("detectedLanguage", {}) or {}).get("name", "unknown"),
            }
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"All Lingva instances failed: {last_err}")


async def _mymemory(client: httpx.AsyncClient, text: str) -> dict[str, str]:
    url = (
        "https://api.mymemory.translated.net/get"
        f"?q={quote(text, safe='')}&langpair=autodetect|en&de={quote(_MYMEMORY_EMAIL)}"
    )
    resp = await client.get(url, timeout=_TIMEOUT_S)
    data = resp.json()
    if data.get("responseStatus") != 200:
        raise RuntimeError(data.get("responseDetails") or "MyMemory failed")
    detected = (
        (data.get("responseData") or {}).get("detectedLanguage")
        or next(
            (m.get("source") for m in (data.get("matches") or []) if m and m.get("segment")),
            None,
        )
        or "unknown"
    )
    return {
        "translated": (data.get("responseData") or {}).get("translatedText") or text,
        "lang": detected,
    }


def _build_result(results: list[dict[str, str]], original: str, source: str) -> dict[str, Any]:
    combined = " ".join(r["translated"] for r in results).strip()
    raw_lang = results[0]["lang"] if results else "unknown"
    detected = _resolve_language_name(raw_lang, original, combined)
    code = get_lang_code(detected)
    skipped = (
        detected.lower() == "english"
        or _normalize_for_compare(combined) == _normalize_for_compare(original)
    )
    return {
        "translated": combined or original,
        "lang":       detected,
        "langCode":   code,
        "source":     source,
        "skipped":    skipped,
    }


async def translate_to_english(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {"translated": text or "", "lang": "Unknown", "langCode": "unknown"}
    clean = text.strip()
    chunks = _split_into_chunks(clean, _MAX_CHUNK)
    async with httpx.AsyncClient() as client:
        try:
            results = [await _lingva(client, c) for c in chunks]
            return _build_result(results, clean, "lingva")
        except Exception as lingva_err:
            try:
                results = [await _mymemory(client, c) for c in chunks]
                return _build_result(results, clean, "mymemory")
            except Exception as mm_err:
                logger.warning("Both translation providers failed: lingva=%s mymemory=%s", lingva_err, mm_err)
                return {
                    "translated": clean,
                    "lang":       "Unknown",
                    "langCode":   "unknown",
                    "error":      str(mm_err),
                }


async def process_support_email(raw_body: str) -> dict[str, Any]:
    result = await translate_to_english(raw_body or "")
    return {
        "originalBody":   raw_body,
        "englishBody":    result.get("translated"),
        "sourceLang":     result.get("lang"),
        "sourceLangCode": result.get("langCode"),
        "wasTranslated":  not result.get("skipped"),
    }


async def normalize_prompt(raw_prompt: str) -> dict[str, Any]:
    result = await translate_to_english(raw_prompt)
    return {
        "originalText":   raw_prompt,
        "normalizedText": (result.get("translated") or "").strip().lower(),
        "originalLang":   result.get("lang"),
    }


async def translate_reply_to_user(english_reply: str, target_code: str) -> dict[str, Any]:
    if not target_code or target_code in {"unknown", "en"}:
        return {"translated": english_reply, "lang": "English", "langCode": "en"}
    encoded = quote(english_reply, safe="")
    async with httpx.AsyncClient() as client:
        for base in _LINGVA_INSTANCES:
            try:
                resp = await client.get(
                    f"{base}/api/v1/en/{target_code}/{encoded}",
                    timeout=_TIMEOUT_S,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if data.get("error"):
                    continue
                return {
                    "translated": data.get("translation", english_reply),
                    "lang":       (data.get("info", {}).get("detectedLanguage", {}) or {}).get("name") or target_code,
                    "langCode":   target_code,
                    "source":     "lingva",
                }
            except Exception:
                continue
        try:
            url = (
                "https://api.mymemory.translated.net/get"
                f"?q={encoded}&langpair=en|{target_code}&de={quote(_MYMEMORY_EMAIL)}"
            )
            resp = await client.get(url, timeout=_TIMEOUT_S)
            data = resp.json()
            if data.get("responseStatus") == 200:
                return {
                    "translated": (data.get("responseData") or {}).get("translatedText") or english_reply,
                    "lang":       target_code,
                    "langCode":   target_code,
                    "source":     "mymemory",
                }
        except Exception as exc:
            logger.warning("translate_reply_to_user mymemory failed: %s", exc)
    return {
        "translated": english_reply,
        "lang":       "English",
        "langCode":   "en",
        "error":      "translation failed",
    }


async def translate_transcript_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            out.append({
                **message,
                "translatedContent": message.get("content") or "",
                "detectedLanguage":  "English",
                "detectedLangCode":  "en",
                "wasTranslated":     False,
            })
            continue
        translation = await process_support_email(message.get("content") or "")
        out.append({
            **message,
            "translatedContent": translation.get("englishBody") or message.get("content") or "",
            "detectedLanguage":  translation.get("sourceLang") or "Unknown",
            "detectedLangCode":  translation.get("sourceLangCode") or "unknown",
            "wasTranslated":     bool(translation.get("wasTranslated")),
        })
    return out
