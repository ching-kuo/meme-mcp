from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

MARKUP_RE = re.compile(r"<[^>]+>")
ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u202e", "\u202d"}
IMPERATIVE_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard previous",
    "disregard prior",
    "new instructions",
    "system:",
    "user:",
    "assistant:",
    "</",
)
# source_url is capped by URL_MAX_LEN in sanitize_url, not _clean_string; the
# entry documents the inner origin keys the recursion sees (KTD6/KTD9).
FIELD_CAPS = {
    "description": 512,
    "name": 128,
    "emotion": 64,
    "usage_context": 256,
    "source_url": 2048,
}

# Fields validated as URLs (https allowlist) rather than markup-cleaned as prose.
# Declared once so both sanitization dispatch sites agree on which keys are URLs.
URL_FIELDS = {"source_url"}

# https-only source links capped well under the metadata blob size. A URL longer
# than this is rejected (returned empty), not truncated -- a truncated URL is a
# broken/dangerous link.
URL_MAX_LEN = 2048


def flag_anomalies(metadata: dict[str, Any]) -> list[str]:
    flags: set[str] = set()
    for value in _walk_strings(metadata):
        lower = value.lower()
        if MARKUP_RE.search(value) or "${" in value or "`" in value:
            flags.add("markup")
        if any(char in value for char in ZERO_WIDTH):
            flags.add("zero_width_unicode")
        if any(marker in lower for marker in IMPERATIVE_MARKERS):
            flags.add("imperative_prompt")
        if len(value) > 512:
            flags.add("length_overflow")
    return sorted(flags)


def hard_sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, str):
            # URL fields are https-validated, not markup-mangled: MARKUP_RE.sub
            # corrupts URLs (KTD6). This canonical branch also covers the friend's
            # edited source_url arriving via approve -> _validated_metadata.
            if key in URL_FIELDS:
                cleaned[key] = sanitize_url(value)
            else:
                cleaned[key] = _clean_string(value, FIELD_CAPS.get(key, 128))
        elif isinstance(value, list):
            cleaned[key] = [_clean_string(str(item), 32) for item in value]
        elif isinstance(value, dict):
            # The origin block carries the clean-data invariant: a still-flagged
            # field is hard-dropped to empty so stored origin -- and find/MCP
            # output -- stays clean even when the friend edits it on approve (KTD6).
            if key == "origin":
                cleaned[key] = _sanitize_origin_block(value)
            else:
                cleaned[key] = hard_sanitize_metadata(value)
        else:
            cleaned[key] = value
    return cleaned


def _sanitize_origin_block(origin: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in origin.items():
        if isinstance(value, str):
            cleaned[key] = clean_origin_value(key, value)
        else:
            cleaned[key] = value
    return cleaned


def sanitize_url(value: str) -> str:
    """Return value if it is a well-formed https URL within the length cap, else "".

    An https-only scheme allowlist is the structural defense against a stored
    ``javascript:``/``data:`` ``source_url`` becoming a live link (KTD6); display
    layers add autoescape on top. Over-long URLs are rejected, not truncated.
    """
    normalized = unicodedata.normalize("NFKC", value).strip()
    for char in ZERO_WIDTH:
        normalized = normalized.replace(char, "")
    if not normalized or len(normalized) > URL_MAX_LEN:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    # Reject userinfo (e.g. https://trusted.com@evil.example/x): the visible
    # prefix impersonates a trusted host while the real host is the part after
    # '@'. urlsplit parses that into username/password, so guard on both.
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return ""
    return normalized


def sanitize_web_results(
    best_guess: str,
    entities: Sequence[str],
    page_titles: Sequence[str],
) -> str:
    """Assemble UNTRUSTED web-recovered text into a clean grounding block.

    Each piece is markup/zero-width-stripped and dropped if it still trips
    ``flag_anomalies`` (the clean-data invariant). The result is presented to the
    VLM as isolated data with explicit framing (U4); this sanitization is
    defense-in-depth, not the primary injection control (KTD2).
    """
    lines: list[str] = []
    name = _clean_and_guard(best_guess, FIELD_CAPS["name"])
    if name:
        lines.append(f"Likely meme identity: {name}")
    clean_entities = [e for e in (_clean_and_guard(x, 64) for x in entities) if e]
    if clean_entities:
        lines.append("Related concepts: " + ", ".join(clean_entities[:8]))
    clean_titles = [t for t in (_clean_and_guard(x, 128) for x in page_titles) if t]
    if clean_titles:
        lines.append("Web page titles: " + " | ".join(clean_titles[:5]))
    return "\n".join(lines)


def clean_origin_value(key: str, value: str) -> str:
    """Sanitize one origin field and enforce the clean-data invariant (KTD6).

    ``source_url`` is https-validated; other keys are markup/length-cleaned. Any
    result that still trips ``flag_anomalies`` is hard-dropped to empty so the
    stored origin -- and therefore ``find``/MCP output to agents -- is guaranteed
    clean without a read-time pass.
    """
    if not value:
        return ""
    if key in URL_FIELDS:
        # sanitize_url is the complete validator for URLs (https allowlist +
        # length cap). Do NOT run flag_anomalies here: its hardcoded 512-char
        # length flag would silently drop valid long https URLs (query strings)
        # that sanitize_url's 2048 cap accepts.
        return sanitize_url(value)
    # _clean_and_guard already drops a still-flagged value to empty, so no
    # second flag_anomalies pass is needed here.
    return _clean_and_guard(value, FIELD_CAPS.get(key, 128))


def _clean_and_guard(value: str, cap: int) -> str:
    cleaned = _clean_string(value, cap)
    if cleaned and flag_anomalies({"value": cleaned}):
        return ""
    return cleaned


def _clean_string(value: str, cap: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    for char in ZERO_WIDTH:
        normalized = normalized.replace(char, "")
    without_markup = MARKUP_RE.sub("", normalized)
    return without_markup[:cap]


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _walk_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _walk_strings(child)]
    return []

