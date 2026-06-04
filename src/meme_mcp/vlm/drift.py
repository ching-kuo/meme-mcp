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
    # Mainland terms spelled entirely with characters that are ALSO valid
    # Traditional (后 台 程 序). hanzidentifier classifies them as BOTH, so neither
    # the Simplified/Mixed check nor the charset fallback catches them -- the
    # denylist is the only precise instrument (a charset scan on these chars would
    # false-positive on legitimate Traditional like 皇后 / 工程 / 順序).
    "后台": "後台",
    "程序": "程式",
}
# Per-character fallback used ONLY when hanzidentifier is unavailable (see
# check_drift). It must contain genuinely Simplified-only characters: shared
# characters that are also valid Traditional (e.g. 量 件 信 息 后 台 程 序) would
# false-positive on legitimate zh-TW prose, so they are deliberately excluded.
# The denylist below still catches whole mainland words regardless of this set.
SIMPLIFIED_ONLY = set("视频质软网络数据库开发")


@dataclass(frozen=True)
class DriftResult:
    passed: bool
    reasons: tuple[str, ...]


def check_drift(text: str) -> DriftResult:
    if not text:
        return DriftResult(True, ())
    reasons: list[str] = []
    verdict = _hanzidentifier_verdict(text)
    if verdict is True:
        reasons.append("simplified_or_mixed")
    elif verdict is None and any(char in SIMPLIFIED_ONLY for char in text):
        # hanzidentifier is unavailable; fall back to the conservative charset
        # scan. When it IS available its identification governs -- the charset
        # scan never runs, so shared Traditional characters are not misflagged.
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


def _hanzidentifier_verdict(text: str) -> bool | None:
    """Tri-state hanzidentifier check.

    Returns True when the text identifies as Simplified or Mixed (reject),
    False when it identifies as clean Traditional/ambiguous (accept), and None
    when hanzidentifier cannot produce a verdict so the caller falls back to the
    conservative per-character scan. The unavailable cases -- the library is not
    installed OR identify() raised -- are deliberately unified: both mean "no
    authoritative verdict", and the only safety net in either case is the
    charset + denylist fallback. We never accept-on-error here, since that would
    let a Simplified string slip through whenever identify() happened to raise.
    """
    try:
        import hanzidentifier  # type: ignore[import-untyped]

        simplified = getattr(hanzidentifier, "SIMPLIFIED", None)
        mixed = getattr(hanzidentifier, "MIXED", None)
        return hanzidentifier.identify(text) in {simplified, mixed}
    except Exception:
        return None
