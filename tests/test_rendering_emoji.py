from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image

from meme_mcp.rendering.emoji import (
    can_render,
    contains_emoji,
    emoji_advance,
    render_emoji,
    segment_runs,
)
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient
from meme_mcp.rendering.text_layout import greedy_wrap

BOX = "\U0001f4e6"  # package
SALUTE = "\U0001fae1"  # saluting face (the screenshot's emoji)
ROCKET = "\U0001f680"
HEART = "❤️"  # red heart + VS16 (BMP base, emoji presentation)
TW_FLAG = "\U0001f1f9\U0001f1fc"  # regional-indicator pair
FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # ZWJ sequence


def _gray_template(width: int = 500, height: int = 200) -> bytes:
    # Mid-gray so white text, black stroke, and saturated emoji are all distinguishable.
    image = Image.new("RGB", (width, height), (128, 128, 128))
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _render(fill: str, box: dict | None = None) -> Image.Image:
    slot: dict = {"name": "top", "position": "center"}
    if box is not None:
        slot["box"] = box
    spec = TemplateSpec(template_id="emoji-test", image_bytes=_gray_template(), slots=[slot])
    return Image.open(BytesIO(preview_transient(spec, [fill]))).convert("RGB")


def _colorful_pixel_count(image: Image.Image) -> int:
    """Pixels with a saturated hue: emoji glyphs, never white/black/gray scaffolding."""
    arr = np.asarray(image).astype(int)
    spread = arr.max(axis=2) - arr.min(axis=2)
    return int((spread > 40).sum())


# ---------------------------------------------------------------------------
# Detection + segmentation
# ---------------------------------------------------------------------------


def test_segment_runs_splits_text_and_emoji() -> None:
    assert segment_runs("hi " + BOX) == (("text", "hi "), ("emoji", BOX))


def test_segment_runs_keeps_zwj_sequence_intact() -> None:
    runs = segment_runs("a " + FAMILY + " b")
    assert ("emoji", FAMILY) in runs


def test_segment_runs_keeps_flag_pair_intact() -> None:
    runs = segment_runs(TW_FLAG)
    assert runs == (("emoji", TW_FLAG),)


def test_bare_keycap_without_vs16_is_one_emoji_cluster() -> None:
    # Unicode permits the FE0F-less keycap form; both must segment as one emoji.
    assert segment_runs("1⃣") == (("emoji", "1⃣"),)
    assert segment_runs("1️⃣") == (("emoji", "1️⃣"),)


def test_vs15_forces_text_presentation() -> None:
    # A base + U+FE0E (text selector) is an explicit "render as text" request.
    assert not contains_emoji("☎︎")  # phone + VS15 -> text
    assert contains_emoji("☎️")  # phone + VS16 -> emoji


def test_text_default_symbol_is_not_auto_emoji() -> None:
    # U+2122 trade-mark defaults to text; it must not be diverted to the color path.
    assert not contains_emoji("™")


def test_cjk_emoji_caption_keeps_no_spurious_spaces() -> None:
    # Regression: the CJK wrapper re-joined tokens with spaces around emoji,
    # rewriting 真😀香 as "真 😀 香" and changing the caption text + width.
    assert greedy_wrap("真" + BOX + "香", 10) == ["真" + BOX + "香"]


def test_contains_emoji_ignores_plain_text() -> None:
    assert not contains_emoji("YAML configs")
    assert not contains_emoji("他說 GG")
    assert contains_emoji("ship it " + ROCKET)


def test_plain_letter_is_not_treated_as_emoji() -> None:
    assert can_render(BOX)
    assert not can_render("A")


# ---------------------------------------------------------------------------
# Glyph rendering
# ---------------------------------------------------------------------------


def test_render_emoji_is_color_and_target_height() -> None:
    glyph = render_emoji(BOX, 64)
    assert glyph.mode == "RGBA"
    assert glyph.height == 64
    assert _colorful_pixel_count(glyph.convert("RGB")) > 50


def test_emoji_advance_tracks_glyph_width() -> None:
    assert emoji_advance(BOX, 64) >= render_emoji(BOX, 64).width


# ---------------------------------------------------------------------------
# End-to-end: emoji renders in color, not as a .notdef box
# ---------------------------------------------------------------------------


def test_emoji_caption_renders_color_not_tofu() -> None:
    colorful = _colorful_pixel_count(_render("picked up " + SALUTE))
    assert colorful > 100, "emoji should composite saturated color pixels, not a hollow box"


def test_plain_caption_has_no_color_pixels() -> None:
    # Control: a pure-text caption stays white/black on gray -- no saturated pixels.
    assert _colorful_pixel_count(_render("PLAIN TEXT")) == 0


def test_emoji_caption_stays_within_box_width() -> None:
    # A narrow edge-anchored box: emoji-aware measurement must keep ink off the edges.
    box = {"anchor_x": 0.0, "anchor_y": 0.0, "scale_x": 1.0, "scale_y": 0.4, "align": "center"}
    image = _render("ship the rocket " + ROCKET + " today", box)
    arr = np.asarray(image).astype(int)
    spread = arr.max(axis=2) - arr.min(axis=2)
    bright = arr.min(axis=2) > 200
    ink = (spread > 40) | bright  # emoji color OR white caption fill
    assert ink[:, 0].sum() == 0 and ink[:, -1].sum() == 0, "caption must not touch side edges"
