from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from io import BytesIO
from typing import Any, cast

from PIL import Image, ImageDraw, ImageFont

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.rendering.emoji import contains_emoji, emoji_tracking, render_emoji, segment_runs
from meme_mcp.rendering.fonts import bundled_font_path
from meme_mcp.rendering.image_store import ImageStore
from meme_mcp.rendering.text_layout import (
    contains_cjk,
    emoji_block_metrics,
    run_advance,
    select_wrap,
)


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    image_bytes: bytes
    slots: list[dict[str, Any]]


@dataclass(frozen=True)
class RenderResult:
    hash: str
    path: str
    rendered_url: str
    alt_text: str
    bytes: bytes


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return cast(ImageFont.ImageFont, ImageFont.truetype(_font_path(), size))
    except OSError:
        return cast(ImageFont.ImageFont, ImageFont.load_default())


@cache
def _font_path() -> str:
    return bundled_font_path("Anton-Regular.ttf")


@cache
def _cjk_font_path() -> str:
    # Noto Sans TC Black covers Latin too, so a mixed caption renders in one consistent
    # face. Selected per caption when any CJK codepoint is present (see _draw_slot_text).
    return bundled_font_path("NotoSansTC-Black.otf")


_LEGACY_BOX_VERTICAL: dict[str, tuple[float, float]] = {
    "bottom": (0.56, 0.44),
    "center": (0.28, 0.44),
}


def _legacy_box_from_position(position: str) -> dict[str, float | str]:
    anchor_y, scale_y = _LEGACY_BOX_VERTICAL.get(position, (0.0, 0.36))
    return {
        "anchor_x": 0.0,
        "anchor_y": anchor_y,
        "scale_x": 1.0,
        "scale_y": scale_y,
        "align": "center",
        "angle": 0.0,
    }


def _slot_anchor(
    slot: dict[str, Any], image_size: tuple[int, int]
) -> tuple[int, int, str, tuple[int, int]]:
    width, height = image_size
    raw_box = slot.get("box")
    box = (
        raw_box
        if isinstance(raw_box, dict)
        else _legacy_box_from_position(str(slot.get("position", "top")))
    )
    anchor_x = float(box.get("anchor_x", 0.0))
    anchor_y = float(box.get("anchor_y", 0.0))
    scale_x = float(box.get("scale_x", 1.0))
    scale_y = float(box.get("scale_y", 0.2))
    align = str(box.get("align", "center"))

    box_w = max(1, int(scale_x * width))
    box_h = max(1, int(scale_y * height))
    if align == "left":
        x = int(anchor_x * width)
        pil_anchor = "lm"
    elif align == "right":
        x = int((anchor_x + scale_x) * width)
        pil_anchor = "rm"
    else:
        x = int((anchor_x + scale_x / 2) * width)
        pil_anchor = "mm"
    y = int((anchor_y + scale_y / 2) * height)
    return x, y, pil_anchor, (box_w, box_h)


def _slot_xy(width: int, height: int, position: str) -> tuple[int, int, str]:
    x, y, anchor, _box_size = _slot_anchor({"position": position}, (width, height))
    return x, y, anchor


def _slot_angle(slot: dict[str, Any]) -> float:
    raw_box = slot.get("box")
    if isinstance(raw_box, dict):
        return float(raw_box.get("angle", 0.0))
    return 0.0


_STROKE_DIVISOR = 18


def _draw_mixed_text(
    target: Image.Image,
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    anchor: str,
    stroke_width: int,
) -> None:
    """Draw a caption that mixes text and emoji, one run at a time.

    PIL cannot mix fonts in a single call, so each line is laid out manually:
    text runs render with the caption font (and stroke), emoji clusters are
    composited from the color-emoji font. Lines stack by the ascent+descent
    model that ``text_layout`` measures against, and each line is centered within
    the block's widest line before the block is placed per ``anchor`` -- matching
    the emoji-free path's ``align="center"`` + ``anchor`` semantics so layout and
    draw stay consistent. A line's trailing emoji tracking is excluded from its
    visible width so a glyph at the end of a line lands on the intended anchor.
    """
    px = int(font.size)
    line_height, spacing, block_height = emoji_block_metrics(font, len(lines))
    top = y - block_height / 2

    laid_out = []
    for line in lines:
        runs = segment_runs(line)
        advances = [run_advance(font, kind, run) for kind, run in runs]
        visible_width = sum(advances)
        if runs and runs[-1][0] == "emoji":
            visible_width -= emoji_tracking(px)
        laid_out.append((runs, advances, visible_width))

    block_width = max((visible for _, _, visible in laid_out), default=0.0)
    if anchor == "lm":
        block_left = float(x)
    elif anchor == "rm":
        block_left = x - block_width
    else:  # "mm" -> centered
        block_left = x - block_width / 2

    for index, (runs, advances, visible_width) in enumerate(laid_out):
        center_y = top + index * (line_height + spacing) + line_height / 2
        cursor = block_left + (block_width - visible_width) / 2
        for (kind, run), advance in zip(runs, advances, strict=True):
            if kind == "emoji":
                glyph = render_emoji(run, px)
                target.paste(glyph, (round(cursor), round(center_y - glyph.height / 2)), glyph)
            else:
                draw.text(
                    (cursor, center_y),
                    run,
                    font=font,
                    fill="white",
                    anchor="lm",
                    stroke_width=stroke_width,
                    stroke_fill="black",
                )
            cursor += advance


def _draw_slot_text(
    target: Image.Image,
    fill: str,
    x: int,
    y: int,
    anchor: str,
    box_size: tuple[int, int],
) -> None:
    # .upper() is a no-op on CJK, so it stays in the shared path. A caption with any
    # CJK codepoint uses the Noto face (it covers Latin too); both faces draw a stroke,
    # so layout always reserves stroke room and the bold outline never clips.
    font_path = _cjk_font_path() if contains_cjk(fill) else _font_path()
    max_size = max(12, int(box_size[1] / 1.4))
    lines, font = select_wrap(
        fill.upper(), box_size, font_path, max_size, stroke_ratio=_STROKE_DIVISOR
    )
    font_size = int(getattr(font, "size", max_size))
    stroke_width = max(1, font_size // _STROKE_DIVISOR)
    draw = ImageDraw.Draw(target)
    if contains_emoji(fill):
        _draw_mixed_text(target, draw, lines, font, x, y, anchor, stroke_width)
    elif len(lines) > 1:
        draw.multiline_text(
            (x, y),
            "\n".join(lines),
            font=font,
            fill="white",
            anchor=anchor,
            align="center",
            spacing=font_size // 6,
            stroke_width=stroke_width,
            stroke_fill="black",
        )
    else:
        draw.text(
            (x, y),
            lines[0],
            font=font,
            fill="white",
            anchor=anchor,
            stroke_width=stroke_width,
            stroke_fill="black",
        )


def _draw_slots(image: Image.Image, slots: list[dict[str, Any]], slot_fills: list[str]) -> None:
    width, height = image.size
    for fill, slot in zip(slot_fills, slots, strict=True):
        x, y, anchor, box_size = _slot_anchor(slot, (width, height))
        angle = _slot_angle(slot)
        if angle == 0.0:
            _draw_slot_text(image, fill, x, y, anchor, box_size)
            continue
        # Each rotated slot draws into its own transparent layer so neighbours and the
        # base image don't smear together when we rotate around the slot anchor.
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_slot_text(layer, fill, x, y, anchor, box_size)
        rotated = layer.rotate(angle, resample=Image.Resampling.BICUBIC, center=(x, y))
        image.alpha_composite(rotated)


def _has_rotation(slots: list[dict[str, Any]]) -> bool:
    return any(_slot_angle(slot) != 0.0 for slot in slots)


def _render_png_bytes(spec: TemplateSpec, slot_fills: list[str]) -> bytes:
    if len(slot_fills) != len(spec.slots):
        raise MemeMCPError(
            ErrorCode.SLOT_MISMATCH,
            [{"field": "slot_fills", "reason": "must match template slot count"}],
        )
    target_mode = "RGBA" if _has_rotation(spec.slots) else "RGB"
    with Image.open(BytesIO(spec.image_bytes)) as source:
        image = source.convert(target_mode)
    _draw_slots(image, spec.slots, slot_fills)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def render_meme(
    spec: TemplateSpec, slot_fills: list[str], image_store: ImageStore, base_url: str
) -> RenderResult:
    # base_url is the externally visible origin (e.g. https://meme.igene.tw); it is
    # prepended so rendered_url is absolute and an MCP client can fetch the image
    # without knowing the server host out of band. Callers pass the same public
    # origin used for OAuth metadata, so render and auth URLs cannot drift.
    content = _render_png_bytes(spec, slot_fills)
    digest = hashlib.sha256(content).hexdigest()[:16]
    path = image_store.put(content, "png")
    return RenderResult(
        hash=digest,
        path=path,
        rendered_url=f"{base_url.rstrip('/')}/renders/{path}",
        alt_text=f"Meme {spec.template_id}: " + " / ".join(slot_fills),
        bytes=content,
    )


def preview_transient(spec: TemplateSpec, slot_fills: list[str]) -> bytes:
    return _render_png_bytes(spec, slot_fills)
