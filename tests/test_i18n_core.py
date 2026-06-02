from __future__ import annotations

import pytest

from meme_mcp.web.i18n import core
from meme_mcp.web.i18n.core import (
    DEFAULT,
    check_completeness,
    js_catalog,
    lint_placeholders,
    negotiate_accept_language,
    plural,
    resolve_locale,
    t,
)


class _FakeRequest:
    """Minimal Request stand-in exposing ``cookies`` and ``headers``."""

    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.cookies = cookies or {}
        # resolve_locale reads the lowercase header name.
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


# ---------------------------------------------------------------------------
# resolve_locale: precedence
# ---------------------------------------------------------------------------


def test_cookie_wins_over_accept_language() -> None:
    request = _FakeRequest(cookies={"lang": "zh-TW"}, headers={"Accept-Language": "en"})

    assert resolve_locale(request) == "zh-TW"  # type: ignore[arg-type]


def test_invalid_cookie_falls_through_to_header() -> None:
    for bad in ("fr", "../x", "", "zh", "EN"):
        request = _FakeRequest(cookies={"lang": bad}, headers={"Accept-Language": "zh-TW"})
        assert resolve_locale(request) == "zh-TW"  # type: ignore[arg-type]


def test_no_signals_defaults_to_en() -> None:
    assert resolve_locale(_FakeRequest()) == DEFAULT  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# negotiate_accept_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("zh-Hant-TW,zh;q=0.9", "zh-TW"),
        ("zh-HK", "zh-TW"),
        ("zh", "zh-TW"),
        ("en-US,en;q=0.9", "en"),
        ("en", "en"),
        # q-weights establish ordering: en outranks zh here.
        ("zh;q=0.3,en;q=0.9", "en"),
        ("fr-FR", None),
        ("fr-FR,de;q=0.8", None),
    ],
)
def test_negotiation(header: str, expected: str | None) -> None:
    assert negotiate_accept_language(header) == expected


@pytest.mark.parametrize("header", [None, "", ";;;", "q=,", "\x00\x01\x02", ",,,"])
def test_negotiation_malformed_never_raises(header: str | None) -> None:
    # None means no match -> caller falls back to DEFAULT.
    assert negotiate_accept_language(header) is None


# ---------------------------------------------------------------------------
# t(): lookup, fallback, interpolation
# ---------------------------------------------------------------------------


def test_t_missing_key_returns_key() -> None:
    assert t("does.not.exist", "en") == "does.not.exist"


def test_t_falls_back_to_en_when_locale_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "MESSAGES", {"x": {"en": "only english"}})

    assert t("x", "zh-TW") == "only english"


def test_t_interpolates_named_placeholder() -> None:
    assert t("browse.match.one", "en", count=1, query="ci") == "1 match for “ci”"


def test_t_robust_to_missing_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "MESSAGES", {"x": {"en": "Hi {name}", "zh-TW": "嗨 {name}"}})

    # No 'name' kwarg supplied -> degrades to the unformatted string, no raise.
    assert t("x", "en", other="z") == "Hi {name}"


def test_t_robust_to_extra_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "MESSAGES", {"x": {"en": "no placeholders", "zh-TW": "無"}})

    assert t("x", "en", name="Ada") == "no placeholders"


# ---------------------------------------------------------------------------
# plural()
# ---------------------------------------------------------------------------


def test_plural_en_selects_one_and_other() -> None:
    assert plural(1, "browse.match", "en", query="ci") == "1 match for “ci”"
    assert plural(0, "browse.match", "en", query="ci") == "0 matches for “ci”"
    assert plural(3, "browse.match", "en", query="ci") == "3 matches for “ci”"


def test_plural_zh_always_single_form() -> None:
    # zh-TW has no plural inflection: n=1 and n=5 use the same .other form,
    # differing only in the interpolated {count}.
    assert plural(1, "browse.match", "zh-TW", query="ci") == "1 筆符合「ci」"
    assert plural(5, "browse.match", "zh-TW", query="ci") == "5 筆符合「ci」"


# ---------------------------------------------------------------------------
# js_catalog()
# ---------------------------------------------------------------------------


def test_js_catalog_returns_only_js_keys() -> None:
    catalog = js_catalog("en")

    assert all(key.startswith("js.") for key in catalog)
    assert "js.copy" in catalog
    assert "nav.browse" not in catalog
    assert catalog["js.copy.done"] == "Copied"


def test_js_catalog_uses_requested_locale() -> None:
    assert js_catalog("zh-TW")["js.copy.done"] == "已複製"


# ---------------------------------------------------------------------------
# lint_placeholders() / check_completeness()
# ---------------------------------------------------------------------------


def test_lint_rejects_non_named_placeholders() -> None:
    bad = {
        "attr": {"en": "{obj.attr}", "zh-TW": "{obj.attr}"},
        "index": {"en": "{0[k]}", "zh-TW": "{0[k]}"},
        "positional": {"en": "{0}", "zh-TW": "{0}"},
    }

    offenders = lint_placeholders(bad)

    assert set(offenders) == {"attr", "index", "positional"}


def test_lint_accepts_named_placeholders() -> None:
    ok = {"x": {"en": "{count} of {total}", "zh-TW": "{total} 之 {count}"}}

    assert lint_placeholders(ok) == []


def test_real_catalog_passes_placeholder_lint() -> None:
    assert lint_placeholders() == []


def test_real_catalog_is_complete() -> None:
    assert check_completeness() == []


def test_completeness_flags_missing_locale_and_unpaired_plural() -> None:
    broken = {
        "a": {"en": "x"},  # missing zh-TW
        "b.one": {"en": "x", "zh-TW": "y"},  # missing b.other
    }

    problems = check_completeness(broken)

    assert any("a: missing zh-TW" in p for p in problems)
    assert any("b.one: missing matching .other" in p for p in problems)
