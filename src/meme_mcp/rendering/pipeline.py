from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

from PIL import Image, ImageDraw, ImageFont

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.rendering.image_store import FilesystemImageStore


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
        return cast(ImageFont.ImageFont, ImageFont.truetype("Arial.ttf", size))
    except OSError:
        return cast(ImageFont.ImageFont, ImageFont.load_default())


def _slot_xy(width: int, height: int, position: str) -> tuple[int, int, str]:
    if position == "bottom":
        return width // 2, int(height * 0.78), "mm"
    if position == "center":
        return width // 2, height // 2, "mm"
    return width // 2, int(height * 0.18), "mm"


def render_meme(
    spec: TemplateSpec, slot_fills: list[str], image_store: FilesystemImageStore
) -> RenderResult:
    if len(slot_fills) != len(spec.slots):
        raise MemeMCPError(
            ErrorCode.SLOT_MISMATCH,
            [{"field": "slot_fills", "reason": "must match template slot count"}],
        )
    with Image.open(BytesIO(spec.image_bytes)) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = _font(max(18, width // 12))
    for fill, slot in zip(slot_fills, spec.slots, strict=True):
        x, y, anchor = _slot_xy(width, height, str(slot.get("position", "top")))
        draw.text(
            (x, y),
            fill.upper(),
            font=font,
            fill="white",
            anchor=anchor,
            stroke_width=3,
            stroke_fill="black",
        )
    out = BytesIO()
    image.save(out, format="PNG")
    content = out.getvalue()
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
    if len(slot_fills) != len(spec.slots):
        raise MemeMCPError(
            ErrorCode.SLOT_MISMATCH,
            [{"field": "slot_fills", "reason": "must match template slot count"}],
        )
    with Image.open(BytesIO(spec.image_bytes)) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = _font(max(18, width // 12))
    for fill, slot in zip(slot_fills, spec.slots, strict=True):
        x, y, anchor = _slot_xy(width, height, str(slot.get("position", "top")))
        draw.text(
            (x, y),
            fill.upper(),
            font=font,
            fill="white",
            anchor=anchor,
            stroke_width=3,
            stroke_fill="black",
        )
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
