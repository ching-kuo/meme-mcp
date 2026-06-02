"""Bilingual (en / zh-TW) i18n package for the web UI.

Re-exports the engine (``core``) and the catalog (``catalog``) so callers import
from one place: ``from meme_mcp.web.i18n import t, resolve_locale, ...``.
"""

from __future__ import annotations

from meme_mcp.web.i18n.catalog import MESSAGES
from meme_mcp.web.i18n.core import (
    COOKIE_NAME,
    DEFAULT,
    SUPPORTED,
    check_completeness,
    js_catalog,
    lint_placeholders,
    negotiate_accept_language,
    plural,
    resolve_locale,
    t,
)

__all__ = [
    "COOKIE_NAME",
    "DEFAULT",
    "MESSAGES",
    "SUPPORTED",
    "check_completeness",
    "js_catalog",
    "lint_placeholders",
    "negotiate_accept_language",
    "plural",
    "resolve_locale",
    "t",
]
