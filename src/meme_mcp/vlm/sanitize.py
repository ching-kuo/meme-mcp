from __future__ import annotations

import re
import unicodedata
from typing import Any

MARKUP_RE = re.compile(r"<[^>]+>")
ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u202e", "\u202d"}
IMPERATIVE_MARKERS = ("ignore previous", "system:", "user:", "</")
FIELD_CAPS = {"description": 512, "name": 128, "emotion": 64, "usage_context": 256}


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
            cleaned[key] = _clean_string(value, FIELD_CAPS.get(key, 128))
        elif isinstance(value, list):
            cleaned[key] = [_clean_string(str(item), 32) for item in value]
        elif isinstance(value, dict):
            cleaned[key] = hard_sanitize_metadata(value)
        else:
            cleaned[key] = value
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

