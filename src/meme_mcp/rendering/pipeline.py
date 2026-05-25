from __future__ import annotations

import atexit
import hashlib
from contextlib import ExitStack
from dataclasses import dataclass
from functools import cache
from importlib import resources
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageDraw, ImageFont

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.text_layout import select_wrap


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
    source_path = Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Anton-Regular.ttf"
    if source_path.is_file():
        return str(source_path)
    # importlib.resources.as_file materializes a real filesystem path even when the
    # package is loaded from a zip; the ExitStack keeps any temp file alive for the
    # process lifetime so PIL can reopen it on every render.
    ref = resources.files("meme_mcp").joinpath("assets/fonts/Anton-Regular.ttf")
    stack = ExitStack()
    atexit.register(stack.close)
    return str(stack.enter_context(resources.as_file(ref)))


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


def _draw_slots(image: Image.Image, slots: list[dict[str, Any]], slot_fills: list[str]) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font_path = _font_path()
    for fill, slot in zip(slot_fills, slots, strict=True):
        x, y, anchor, box_size = _slot_anchor(slot, (width, height))
        max_size = max(12, int(box_size[1] / 1.4))
        lines, font = select_wrap(fill.upper(), box_size, font_path, max_size)
        font_size = int(getattr(font, "size", max_size))
        stroke_width = max(1, font_size // 18)
        if len(lines) > 1:
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


def _render_png_bytes(spec: TemplateSpec, slot_fills: list[str]) -> bytes:
    if len(slot_fills) != len(spec.slots):
        raise MemeMCPError(
            ErrorCode.SLOT_MISMATCH,
            [{"field": "slot_fills", "reason": "must match template slot count"}],
        )
    with Image.open(BytesIO(spec.image_bytes)) as source:
        image = source.convert("RGB")
    _draw_slots(image, spec.slots, slot_fills)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def render_meme(
    spec: TemplateSpec, slot_fills: list[str], image_store: FilesystemImageStore
) -> RenderResult:
    content = _render_png_bytes(spec, slot_fills)
    digest = hashlib.sha256(content).hexdigest()[:16]
    path = image_store.put(content, "png")
    return RenderResult(
        hash=digest,
        path=path,
        rendered_url=f"/renders/{path}",
        alt_text=f"Meme {spec.template_id}: " + " / ".join(slot_fills),
        bytes=content,
    )


def preview_transient(spec: TemplateSpec, slot_fills: list[str]) -> bytes:
    return _render_png_bytes(spec, slot_fills)
