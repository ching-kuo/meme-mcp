from __future__ import annotations

from collections.abc import Sequence

from PIL import ImageFont

MeasuredFont = ImageFont.ImageFont | ImageFont.FreeTypeFont


def _text_bbox_size(font: MeasuredFont, text: str) -> tuple[int, int]:
    left, top, right, bottom = font.getbbox(text)
    return int(right - left), int(bottom - top)


def _measure_lines(font: MeasuredFont, lines: Sequence[str]) -> tuple[int, int]:
    """Measure width and stacked height of one or more lines (matches multiline render)."""
    spacing = int(font.size) // 6 if hasattr(font, "size") else 0
    sizes = [_text_bbox_size(font, line) for line in lines]
    width = max((w for w, _ in sizes), default=0)
    height = sum(h for _, h in sizes) + spacing * max(0, len(sizes) - 1)
    return width, height


def greedy_wrap(text: str, max_chars: int, target_lines: int | None = None) -> list[str]:
    max_chars = max(1, max_chars)
    words = text.split()
    if target_lines == 1:
        return [" ".join(words)] if words else [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        can_add_line = target_lines is None or len(lines) < target_lines - 1
        if len(candidate) > max_chars and current and can_add_line:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def fit_font(
    text: str,
    box_size: tuple[int, int],
    font_path: str,
    max_size: int,
    min_size: int = 12,
) -> ImageFont.FreeTypeFont:
    """Return the largest font that fits text within the box with memegen-like margins.

    `text` may include newlines; multi-line input is measured per-line with the same
    line spacing the renderer will apply at draw time.
    """
    box_w, box_h = box_size
    usable_w = max(1, int(box_w - box_w / 35))
    usable_h = max(1, int(box_h - box_h / 10))
    start = max(min_size, max_size)
    lines = text.split("\n")
    for size in range(start, min_size - 1, -2):
        font = ImageFont.truetype(font_path, size)
        text_w, text_h = _measure_lines(font, lines)
        if text == "" or (text_w <= usable_w and text_h <= usable_h):
            return font
    return ImageFont.truetype(font_path, min_size)


def select_wrap(
    text: str,
    box_size: tuple[int, int],
    font_path: str,
    max_size: int,
    min_size: int = 12,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Choose a 1/2/3-line layout that fills the box while maximizing font size."""
    normalized = " ".join(text.split())
    if not normalized:
        return [""], fit_font("", box_size, font_path, max_size, min_size)

    candidates: list[tuple[list[str], ImageFont.FreeTypeFont, bool, bool, int]] = []
    for target_lines in (1, 2, 3):
        max_chars = max(1, (len(normalized) + target_lines - 1) // target_lines)
        lines = greedy_wrap(normalized, max_chars, target_lines)
        font = fit_font("\n".join(lines), box_size, font_path, max_size, min_size)
        width, height = _measure_lines(font, lines)
        fits = width <= box_size[0] - box_size[0] / 35 and height <= box_size[1] - box_size[1] / 10
        fills = width >= box_size[0] * 0.60
        candidates.append((lines, font, fits, fills, width))

    compliant = [candidate for candidate in candidates if candidate[2] and candidate[3]]
    fitting = [candidate for candidate in candidates if candidate[2]]
    pool = compliant or fitting or candidates
    best = max(pool, key=lambda candidate: (candidate[1].size, candidate[4]))
    return best[0], best[1]
