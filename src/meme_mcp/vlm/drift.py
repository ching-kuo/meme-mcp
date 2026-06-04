from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAINLAND_VOCAB_DENYLIST = {
    "視頻": "影片",
    "视频": "影片",
    "質量": "品質",
    "质量": "品質",
    "软件": "軟體",
    "軟件": "軟體",
    "网络": "網路",
    "信息": "資訊",
}
# Per-character fallback when hanzidentifier is unavailable. Chars overlapping
# MAINLAND_VOCAB_DENYLIST terms are intentional defense-in-depth: the denylist
# catches whole mainland words, this catches the simplified chars on their own.
SIMPLIFIED_ONLY = set("视频质量软件网络信息后台数据库程序开发")


@dataclass(frozen=True)
class DriftResult:
    passed: bool
    reasons: tuple[str, ...]


def check_drift(text: str) -> DriftResult:
    if not text:
        return DriftResult(True, ())
    reasons: list[str] = []
    if _hanzidentifier_rejects(text):
        reasons.append("simplified_or_mixed")
    elif any(char in SIMPLIFIED_ONLY for char in text):
        reasons.append("simplified_character")
    for term, preferred in MAINLAND_VOCAB_DENYLIST.items():
        if term in text:
            reasons.append(f"mainland_vocab:{term}->{preferred}")
    return DriftResult(not reasons, tuple(reasons))


def check_metadata_drift(metadata: dict[str, Any], locale: str = "zh-TW") -> DriftResult:
    locales = metadata.get("locales")
    if not isinstance(locales, dict):
        return DriftResult(True, ())
    block = locales.get(locale)
    if not isinstance(block, dict):
        return DriftResult(True, ())
    reasons: list[str] = []
    for key, value in block.items():
        if key.startswith("_"):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            # Non-string items (malformed pre-sanitize shapes) are skipped, not
            # repr-coerced: str() on a dict would drift-check the repr text.
            if not isinstance(item, str):
                continue
            result = check_drift(item)
            reasons.extend(f"{key}:{reason}" for reason in result.reasons)
    return DriftResult(not reasons, tuple(reasons))


def _hanzidentifier_rejects(text: str) -> bool:
    try:
        import hanzidentifier  # type: ignore[import-untyped]
    except Exception:
        return False
    simplified = getattr(hanzidentifier, "SIMPLIFIED", None)
    mixed = getattr(hanzidentifier, "MIXED", None)
    try:
        return hanzidentifier.identify(text) in {simplified, mixed}
    except Exception:
        return False
