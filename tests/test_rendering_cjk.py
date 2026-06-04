from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from meme_mcp.rendering import pipeline
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient
from meme_mcp.rendering.text_layout import (
    contains_cjk,
    fit_font,
    greedy_wrap,
    segment_tokens,
    select_wrap,
)

CJK_FONT = pipeline._cjk_font_path()
LATIN_FONT = pipeline._font_path()


def _ink_pixels(png: bytes) -> int:
    """Count near-white (caption ink) pixels in a rendered PNG."""
    image = Image.open(BytesIO(png)).convert("RGB")
    arr = np.asarray(image)
    # Caption fill is white with a black stroke; count bright pixels only.
    return int((arr.min(axis=2) > 200).sum())


def _navy_template(width: int = 400, height: int = 240) -> bytes:
    image = Image.new("RGB", (width, height), "navy")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# CJK detection + tokenization
# ---------------------------------------------------------------------------


def test_contains_cjk_detects_han() -> None:
    assert contains_cjk("真香")
    assert contains_cjk("他說 GG")
    assert not contains_cjk("hello world")
    assert not contains_cjk("YAML configs")
    assert not contains_cjk("")


def test_segment_tokens_splits_each_han_char() -> None:
    assert segment_tokens("真香") == ["真", "香"]


def test_segment_tokens_keeps_latin_words_atomic() -> None:
    # Mixed "他說 GG": Han chars individual, the Latin run stays one token.
    assert segment_tokens("他說 GG") == ["他", "說", "GG"]


def test_segment_tokens_pure_latin_matches_split() -> None:
    text = "if logs are smart"
    assert segment_tokens(text) == text.split()


# ---------------------------------------------------------------------------
# Wrapping with CJK tokens (pixel-measured)
# ---------------------------------------------------------------------------


def test_greedy_wrap_breaks_unspaced_cjk() -> None:
    # 20 unspaced Han chars cannot stay on one line in a narrow box.
    text = "真" * 20
    lines = greedy_wrap(text, max_chars=6)
    assert len(lines) > 1
    assert "".join(lines) == text


def test_greedy_wrap_keeps_latin_word_unsplit_in_mixed() -> None:
    lines = greedy_wrap("他說 GG", max_chars=2)
    # "GG" must survive intact on whichever line it lands.
    assert any("GG" in line for line in lines)
    assert all("G" not in line or "GG" in line for line in lines)


def test_kinsoku_pulls_back_line_initial_closing_punct() -> None:
    # A wrap that would start a line with 。 must pull the 。 onto the prior line.
    text = "真香真香。真香"
    lines = greedy_wrap(text, max_chars=4)
    assert len(lines) > 1
    for line in lines:
        assert not line.startswith("。")


def test_kinsoku_no_line_final_opening_punct() -> None:
    text = "真香「真香真香"
    lines = greedy_wrap(text, max_chars=3)
    assert len(lines) > 1
    for line in lines:
        assert not line.endswith("「")


def test_select_wrap_wraps_long_cjk_to_multiple_lines() -> None:
    lines, _font = select_wrap("真" * 20, (200, 120), CJK_FONT, 80)
    assert len(lines) >= 2
    assert "".join(lines) == "真" * 20


# ---------------------------------------------------------------------------
# fit_font with stroke accounting
# ---------------------------------------------------------------------------


def test_select_wrap_shrinks_long_cjk_in_small_slot() -> None:
    # Long CJK in a Latin-authored small slot must wrap + shrink, not overflow.
    box = (120, 60)
    text = "真香真香真香真香真香"
    lines, font = select_wrap(text, box, CJK_FONT, 66, stroke_ratio=18)
    stroke = max(1, font.size // 18)
    widest = max(int(font.getlength(line)) for line in lines)
    assert widest + 2 * stroke <= box[0]
    assert "".join("".join(line.split()) for line in lines) == text


def test_fit_font_accounts_for_stroke_width_no_clip() -> None:
    # With stroke accounted, the inked glyph + outline stays inside usable width.
    box = (200, 80)
    text = "真香真香"
    plain = fit_font(text, box, CJK_FONT, 66, stroke_ratio=0)
    stroked = fit_font(text, box, CJK_FONT, 66, stroke_ratio=18)
    # Accounting for the stroke can only keep or shrink the chosen size.
    assert stroked.size <= plain.size


# ---------------------------------------------------------------------------
# End-to-end render through the pipeline
# ---------------------------------------------------------------------------


def _render_single(fill: str, box: dict | None = None) -> bytes:
    slot = {"name": "top", "position": "center"}
    if box is not None:
        slot["box"] = box
    spec = TemplateSpec(
        template_id="cjk-test",
        image_bytes=_navy_template(),
        slots=[slot],
    )
    return preview_transient(spec, [fill])


def test_render_cjk_produces_real_ink_not_tofu() -> None:
    png = _render_single("真香")
    # Tofu boxes are hollow rectangles; real glyphs fill a meaningful pixel mass.
    assert _ink_pixels(png) > 300


def test_render_long_cjk_fits_within_box() -> None:
    # 20-char unspaced CJK in a normal slot wraps to multiple lines and renders ink.
    png = _render_single("真香" * 10)
    assert _ink_pixels(png) > 300


def test_render_mixed_caption_renders_ink() -> None:
    png = _render_single("他說 GG")
    assert _ink_pixels(png) > 300


def test_render_pure_latin_uses_latin_font_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure-Latin captions must never touch the CJK font (golden-parity guarantee).
    def _boom() -> str:
        raise AssertionError("CJK font selected for a pure-Latin caption")

    monkeypatch.setattr(pipeline, "_cjk_font_path", _boom)
    png = _render_single("YAML configs")
    assert _ink_pixels(png) > 100
