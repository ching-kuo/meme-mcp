"""Bilingual message catalog: the single source of truth for UI copy.

``MESSAGES`` maps a dotted message id to its ``en`` and ``zh-TW`` strings. Keys
prefixed ``js.`` are shipped to the browser in the ``window.I18N`` blob (KTD6);
all other keys are server-render only. Values use **named placeholders only**
(``{count}``, ``{login}``) -- see ``core.lint_placeholders``.

This file is seeded here and fully populated in U4 (server templates) and U5
(client JS). The completeness test (``core.check_completeness``) keeps both
locales in lockstep.
"""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # --- nav (server-only) ---------------------------------------------------
    "nav.browse": {"en": "Browse", "zh-TW": "瀏覽"},
    "nav.upload": {"en": "Upload", "zh-TW": "上傳"},
    "nav.account": {"en": "Account", "zh-TW": "帳號"},
    # --- landing -------------------------------------------------------------
    "landing.tagline": {
        "en": "A private meme studio for friends — find the right template, "
        "fill it in, and ship it.",
        "zh-TW": "專為朋友打造的迷因工作室 — 找到合適的範本、填好內容，立刻發出去。",
    },
    # --- browse (plural example) ---------------------------------------------
    "browse.match.one": {
        "en": "{count} match for “{query}”",
        "zh-TW": "{count} 筆符合「{query}」",
    },
    "browse.match.other": {
        "en": "{count} matches for “{query}”",
        "zh-TW": "{count} 筆符合「{query}」",
    },
    # --- client JS (shipped in window.I18N) ----------------------------------
    "js.copy": {"en": "Copy", "zh-TW": "複製"},
    "js.copy.done": {"en": "Copied", "zh-TW": "已複製"},
}
