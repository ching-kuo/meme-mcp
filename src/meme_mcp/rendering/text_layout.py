from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from PIL import Image, ImageDraw, ImageFont

from meme_mcp.rendering.emoji import (
    contains_emoji,
    emoji_advance,
    emoji_token_len,
    is_emoji_cluster,
    segment_runs,
)

MeasuredFont = ImageFont.ImageFont | ImageFont.FreeTypeFont

# A 1x1 scratch surface used only to ask PIL for the true rendered extent of a
# multi-line block (multiline_textbbox needs an ImageDraw); never drawn upon.
_SCRATCH = ImageDraw.Draw(Image.new("L", (1, 1)))

# Closing punctuation must never begin a line; opening punctuation must never end one
# (minimal kinsoku shori). Kept as frozensets so the rules are immutable.
_KINSOKU_NO_LINE_START: frozenset[str] = frozenset("」』。，！？、）")
_KINSOKU_NO_LINE_END: frozenset[str] = frozenset("「『（")


def _is_cjk_char(char: str) -> bool:
    """True for codepoints we treat as individually breakable CJK tokens.

    Covers CJK Unified Ideographs and Ext A, fullwidth/CJK punctuation, and the
    CJK Symbols block so kinsoku punctuation also counts as CJK width.
    """
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x3000 <= code <= 0x303F  # CJK Symbols and Punctuation
        or 0xFF00 <= code <= 0xFFEF  # Halfwidth and Fullwidth Forms
    )


def contains_cjk(text: str) -> bool:
    """Whether any character in ``text`` is treated as CJK."""
    return any(_is_cjk_char(char) for char in text)


def segment_tokens(text: str) -> list[str]:
    """Break text into wrap tokens: Latin runs stay word-atomic, each CJK char alone.

    Whitespace separates Latin words as usual; CJK characters always start a new
    token (and never merge with a neighbouring Latin run), so a line break may fall
    on any character boundary between Han glyphs. A renderable emoji cluster is its
    own token too, so a line may break either side of it.
    """
    tokens: list[str] = []
    latin_run = ""
    index = 0
    length = len(text)
    while index < length:
        span = emoji_token_len(text, index)
        if span:
            if latin_run:
                tokens.append(latin_run)
                latin_run = ""
            tokens.append(text[index : index + span])
            index += span
            continue
        char = text[index]
        if _is_cjk_char(char):
            if latin_run:
                tokens.append(latin_run)
                latin_run = ""
            tokens.append(char)
        elif char.isspace():
            if latin_run:
                tokens.append(latin_run)
                latin_run = ""
        else:
            latin_run += char
        index += 1
    if latin_run:
        tokens.append(latin_run)
    return tokens


def _join_tokens(tokens: Sequence[str]) -> str:
    """Reassemble a line: single space between Latin words, none around CJK or emoji.

    Emoji clusters carry their own horizontal tracking when drawn, so inserting a
    space beside one would both alter the caption text (e.g. CJK ``真😀香``) and
    double the gap; adjacent CJK chars are never spaced either.
    """
    parts: list[str] = []
    for token in tokens:
        if parts:
            both_cjk = _is_cjk_char(token[0]) and _is_cjk_char(parts[-1][-1])
            touches_emoji = is_emoji_cluster(token) or is_emoji_cluster(parts[-1])
            if not (both_cjk or touches_emoji):
                parts.append(" ")
        parts.append(token)
    return "".join(parts)


def _apply_kinsoku(lines: list[list[str]]) -> list[list[str]]:
    """Pull forbidden line-initial/final tokens back across the break."""
    for i in range(1, len(lines)):
        # No line may start with closing punctuation: pull it to the previous line.
        while lines[i] and lines[i][0] in _KINSOKU_NO_LINE_START and lines[i - 1]:
            lines[i - 1].append(lines[i].pop(0))
        # No line may end with opening punctuation: push it to the next line.
        while lines[i - 1] and lines[i - 1][-1] in _KINSOKU_NO_LINE_END and len(lines[i - 1]) > 1:
            lines[i].insert(0, lines[i - 1].pop())
    return [line for line in lines if line]


def _line_spacing(font: MeasuredFont) -> int:
    return int(font.size) // 6 if hasattr(font, "size") else 0


def run_advance(font: MeasuredFont, kind: str, run: str) -> float:
    """Advance width of one segment run from :func:`emoji.segment_runs`.

    The single source of truth for the emoji-vs-text width rule, shared by layout
    measurement and the manual mixed-font draw path so fit and draw never disagree.
    The text font reports a ``.notdef`` box for emoji codepoints, so emoji runs are
    measured at the size they are actually composited at (see emoji.emoji_advance).
    """
    if kind == "emoji":
        return emoji_advance(run, int(font.size) if hasattr(font, "size") else 0)
    return font.getlength(run)


def _line_width(font: MeasuredFont, line: str) -> float:
    return sum(run_advance(font, kind, run) for kind, run in segment_runs(line))


def emoji_block_metrics(font: ImageFont.FreeTypeFont, line_count: int) -> tuple[int, int, int]:
    """(line_height, spacing, block_height) for the manual mixed-font draw path.

    PIL cannot lay out mixed fonts via ``multiline_text``, so emoji captions stack
    lines by ascent+descent. The emoji branch of :func:`_measure_lines` reserves the
    same block_height this returns, keeping the measure/draw contract in one place.
    """
    spacing = _line_spacing(font)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    return line_height, spacing, line_count * line_height + spacing * max(0, line_count - 1)


def _measure_lines(font: MeasuredFont, lines: Sequence[str]) -> tuple[int, int]:
    """Measure width and stacked height as the renderer will actually draw them.

    Width is emoji-aware. Height matches PIL's real layout: emoji-free blocks use
    ``multiline_textbbox`` (the renderer's own metric, ~25-30% taller than a naive
    sum of per-line ink bounds), while emoji blocks use the ascent+descent line model
    that the manual mixed-font draw path stacks lines by, so fit and draw agree.
    """
    width = max((_line_width(font, line) for line in lines), default=0.0)
    height: float
    if any(contains_emoji(line) for line in lines) and hasattr(font, "getmetrics"):
        _, _, height = emoji_block_metrics(cast(ImageFont.FreeTypeFont, font), len(lines))
    else:
        box = _SCRATCH.multiline_textbbox(
            (0, 0), "\n".join(lines), font=font, align="center", spacing=_line_spacing(font)
        )
        height = box[3] - box[1]
    return int(width), int(height)


def _greedy_wrap_cjk(text: str, max_chars: int, target_lines: int | None) -> list[str]:
    """Token-aware greedy wrap for captions containing CJK characters."""
    tokens = segment_tokens(text)
    if not tokens:
        return [""]
    if target_lines == 1:
        return [_join_tokens(tokens)]
    lines: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        candidate = current + [token]
        can_add_line = target_lines is None or len(lines) < target_lines - 1
        if len(_join_tokens(candidate)) > max_chars and current and can_add_line:
            lines.append(current)
            current = [token]
        else:
            current = candidate
    if current:
        lines.append(current)
    lines = _apply_kinsoku(lines)
    return [_join_tokens(line) for line in lines] or [""]


def greedy_wrap(text: str, max_chars: int, target_lines: int | None = None) -> list[str]:
    max_chars = max(1, max_chars)
    if contains_cjk(text):
        return _greedy_wrap_cjk(text, max_chars, target_lines)
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
    stroke_ratio: int = 0,
) -> ImageFont.FreeTypeFont:
    """Return the largest font that fits text within the box with memegen-like margins.

    `text` may include newlines; multi-line input is measured per-line with the same
    line spacing the renderer will apply at draw time. ``stroke_ratio`` (font_size //
    stroke_ratio, the same heuristic the renderer uses) reserves room for the stroke
    outline on both sides so a bold CJK glyph + its outline never clips the box edge.
    """
    box_w, box_h = box_size
    usable_w = max(1, int(box_w - box_w / 35))
    usable_h = max(1, int(box_h - box_h / 10))
    start = max(min_size, max_size)
    lines = text.split("\n")
    for size in range(start, min_size - 1, -2):
        font = ImageFont.truetype(font_path, size)
        text_w, text_h = _measure_lines(font, lines)
        stroke = (max(1, size // stroke_ratio) if stroke_ratio else 0) * 2
        if text == "" or (text_w + stroke <= usable_w and text_h + stroke <= usable_h):
            return font
    return ImageFont.truetype(font_path, min_size)


def _max_chars_for(text: str, target_lines: int) -> int:
    """Per-line column budget for ``greedy_wrap``.

    The pixel measurement in ``fit_font``/``select_wrap`` is the source of truth; this
    heuristic only seeds how aggressively ``greedy_wrap`` splits. Pure-Latin text keeps
    the original ``len()``-based budget byte-for-byte (golden parity). When CJK is
    present, each CJK char counts as ~2 columns so a dense Han line breaks sooner.
    """
    if not contains_cjk(text):
        return max(1, (len(text) + target_lines - 1) // target_lines)
    columns = sum(2 if _is_cjk_char(char) else 1 for char in text if not char.isspace())
    return max(1, (columns + target_lines - 1) // target_lines)


def select_wrap(
    text: str,
    box_size: tuple[int, int],
    font_path: str,
    max_size: int,
    min_size: int = 12,
    stroke_ratio: int = 0,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Choose a 1/2/3-line layout that fills the box while maximizing font size."""
    normalized = " ".join(text.split())
    if not normalized:
        return [""], fit_font("", box_size, font_path, max_size, min_size, stroke_ratio)

    candidates: list[tuple[list[str], ImageFont.FreeTypeFont, bool, bool, int]] = []
    for target_lines in (1, 2, 3):
        max_chars = _max_chars_for(normalized, target_lines)
        lines = greedy_wrap(normalized, max_chars, target_lines)
        font = fit_font("\n".join(lines), box_size, font_path, max_size, min_size, stroke_ratio)
        width, height = _measure_lines(font, lines)
        fits = width <= box_size[0] - box_size[0] / 35 and height <= box_size[1] - box_size[1] / 10
        fills = width >= box_size[0] * 0.60
        candidates.append((lines, font, fits, fills, width))

    compliant = [candidate for candidate in candidates if candidate[2] and candidate[3]]
    fitting = [candidate for candidate in candidates if candidate[2]]
    pool = compliant or fitting or candidates
    best = max(pool, key=lambda candidate: (candidate[1].size, candidate[4]))
    return best[0], best[1]
