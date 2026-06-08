"""Color-emoji support: detection, grapheme-cluster segmentation, and rendering.

PIL renders exactly one font per draw call and performs no font fallback, so a
caption that mixes text and emoji cannot be drawn with a single ``draw.text``.
This module is the leaf that the layout (width measurement) and the pipeline
(mixed-font drawing) build on: it splits a string into text/emoji runs and
rasterizes each emoji cluster from the bundled Noto Color Emoji font.

Noto Color Emoji is a CBDT bitmap font with a single ~109px strike, so glyphs
are rendered at that native size and downscaled to the caption's font size.
"""

from __future__ import annotations

from functools import cache, lru_cache

from PIL import Image, ImageDraw, ImageFont

from meme_mcp.rendering.fonts import bundled_font_path

_EMOJI_FONT_FILENAME = "NotoColorEmoji.ttf"

# Cluster-grammar combining marks (a subset of UAX #29 / UTS #51 sufficient for
# real captions): variation selectors, skin-tone modifiers, the keycap combiner,
# and ZWJ that glues multi-person / profession sequences together.
_ZWJ = 0x200D
_VS15 = 0xFE0E  # text-presentation selector (explicit "render as text" request)
_VS16 = 0xFE0F  # emoji-presentation selector
_KEYCAP = 0x20E3
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)  # regional indicators -> flags, used in pairs
_TAG_RANGE = range(0xE0020, 0xE0080)  # tag chars for subdivision flags (e.g. England)
_KEYCAP_BASES = frozenset("0123456789#*")  # digit/#/* + optional VS16 + U+20E3
# VS15 stays out of _MODIFIERS so a base + FE0E is rejected as a text request, not consumed.
_MODIFIERS = frozenset({_VS16, _KEYCAP, *_SKIN_TONES})


def _is_emoji_codepoint(cp: int) -> bool:
    """Whether ``cp`` defaults to emoji presentation (no VS16 required).

    Covers the supplementary emoji planes plus the common BMP symbol/dingbat
    blocks that Noto Color Emoji renders. Text-default symbols (e.g. U+2122 trade
    mark, U+2139 information) are deliberately excluded: they only reach the color
    path when the caption explicitly requests emoji presentation with U+FE0F. The
    block ranges are intentionally broad for a meme generator (rendering a color
    glyph beats the text fonts' .notdef box) and are always gated by ``can_render``.
    """
    return (
        0x1F000 <= cp <= 0x1FAFF  # emoticons, pictographs, transport, symbols, ext-A
        or cp in _REGIONAL
        or 0x2600 <= cp <= 0x27BF  # misc symbols + dingbats
        or 0x2B00 <= cp <= 0x2BFF  # stars, squares, arrows (e.g. star, white-square)
        or 0x2300 <= cp <= 0x23FF  # technical: watch, hourglass, keyboard, eject
        or cp == 0x2615  # hot beverage (emoji-default BMP coffee)
    )


def _keycap_len(text: str, start: int) -> int:
    """Length of a keycap sequence (digit/#/* [+ FE0F] + U+20E3) at ``start``, else 0.

    Handles the FE0F-less form Unicode permits, which the emoji-codepoint gate misses.
    """
    length = len(text)
    if text[start] not in _KEYCAP_BASES:
        return 0
    pos = start + 1
    if pos < length and ord(text[pos]) == _VS16:
        pos += 1
    if pos < length and ord(text[pos]) == _KEYCAP:
        return pos - start + 1
    return 0


def _cluster_len(text: str, start: int) -> int:
    """Length of the emoji grapheme cluster beginning at ``start``, else 0.

    Recognizes flag pairs (two regional indicators), keycap and skin-tone
    modifiers, variation selectors, subdivision tag sequences, and ZWJ chains.
    A base immediately followed by U+FE0E (text selector) is rejected so the
    caption renders as text. Does not consult the font; callers gate on
    :func:`can_render`.
    """
    length = len(text)
    first = ord(text[start])

    if first in _REGIONAL:
        # A flag is exactly two regional indicators; a lone one is still emoji.
        if start + 1 < length and ord(text[start + 1]) in _REGIONAL:
            return 2
        return 1

    keycap = _keycap_len(text, start)
    if keycap:
        return keycap

    next_cp = ord(text[start + 1]) if start + 1 < length else None
    if next_cp == _VS15:  # explicit text presentation -> not an emoji cluster
        return 0
    if not (_is_emoji_codepoint(first) or next_cp == _VS16):
        return 0

    pos = start + 1
    while pos < length:
        code = ord(text[pos])
        if code in _MODIFIERS or code in _TAG_RANGE:
            pos += 1
            continue
        if code == _ZWJ and pos + 1 < length:
            pos += 2  # consume ZWJ + the next cluster's base codepoint
            while pos < length and (ord(text[pos]) in _MODIFIERS or ord(text[pos]) in _TAG_RANGE):
                pos += 1
            continue
        break
    return pos - start


@cache
def emoji_font_path() -> str:
    return bundled_font_path(_EMOJI_FONT_FILENAME)


@cache
def _native_strike() -> int:
    """The bitmap strike size the CBDT font exposes (PIL rejects other sizes)."""
    path = emoji_font_path()
    for size in (109, 128, 136, 160, 96, 64, 48, 32):
        try:
            ImageFont.truetype(path, size)
            return size
        except OSError:
            continue
    raise OSError("no usable bitmap strike found in emoji font")


@cache
def _emoji_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(emoji_font_path(), _native_strike())


@lru_cache(maxsize=1024)
def _native_glyph(cluster: str) -> Image.Image | None:
    """Rasterize ``cluster`` at the font's native strike, cropped to ink; None if absent.

    Internal cache shared by detection, advance, and rendering so a cluster is
    rasterized at most once regardless of how many font sizes the fit loop tries.
    Callers only read or resize the result (resize returns a fresh image); they
    must never mutate it in place, or the cache would be corrupted.
    """
    native = _native_strike()
    canvas = Image.new("RGBA", (native * 3, native * 3), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((native, native), cluster, font=_emoji_font(), embedded_color=True)
    box = canvas.getbbox()
    if box is None or (box[2] - box[0]) <= 1 or (box[3] - box[1]) <= 1:
        return None
    return canvas.crop(box)


def can_render(cluster: str) -> bool:
    """Whether the emoji font produces a glyph for ``cluster``.

    Guards detection: a codepoint in an emoji range that the bundled font lacks
    must fall back to the text path rather than rasterize an empty box.
    """
    return _native_glyph(cluster) is not None


def _scaled_glyph_width(glyph: Image.Image, px: int) -> int:
    return max(1, round(glyph.width * px / glyph.height))


@lru_cache(maxsize=256)
def segment_runs(text: str) -> tuple[tuple[str, str], ...]:
    """Split ``text`` into ordered ("text"|"emoji", value) runs.

    Consecutive non-emoji characters merge into one text run; each renderable
    emoji cluster is its own run so callers can measure and draw it separately.
    Cached and immutable: this runs once per font size in the fit loop and again
    at draw time over the same wrapped lines, so memoizing the pure split avoids
    re-scanning identical strings.
    """
    runs: list[tuple[str, str]] = []
    buffer = ""
    index = 0
    length = len(text)
    while index < length:
        span = _cluster_len(text, index)
        cluster = text[index : index + span] if span else ""
        if span and can_render(cluster):
            if buffer:
                runs.append(("text", buffer))
                buffer = ""
            runs.append(("emoji", cluster))
            index += span
        else:
            buffer += text[index]
            index += 1
    if buffer:
        runs.append(("text", buffer))
    return tuple(runs)


def contains_emoji(text: str) -> bool:
    return any(kind == "emoji" for kind, _ in segment_runs(text))


def emoji_token_len(text: str, start: int) -> int:
    """Cluster length at ``start`` only if renderable, for the wrap tokenizer."""
    span = _cluster_len(text, start)
    if span and can_render(text[start : start + span]):
        return span
    return 0


def render_emoji(cluster: str, px: int) -> Image.Image:
    """Rasterize ``cluster`` as a fresh RGBA image whose height is ``px`` pixels.

    Returns a new image on every call (a resize of the cached native glyph), so the
    caller may composite or transform it freely without touching the shared cache.
    """
    px = max(1, px)
    glyph = _native_glyph(cluster)
    if glyph is None:
        return Image.new("RGBA", (px, px), (0, 0, 0, 0))
    return glyph.resize((_scaled_glyph_width(glyph, px), px), Image.Resampling.LANCZOS)


# Horizontal breathing room placed after each emoji, proportional to its size.
_TRACKING_DIVISOR = 8


def emoji_tracking(px: int) -> int:
    """Trailing breathing room after an emoji at height ``px`` (not visible ink).

    Exposed so the draw path can exclude a line's trailing emoji tracking when
    centering/right-anchoring, keeping the visible glyph on the intended anchor.
    """
    return max(1, max(1, px) // _TRACKING_DIVISOR)


def is_emoji_cluster(token: str) -> bool:
    """Whether ``token`` is exactly one renderable emoji cluster (a wrap/join token)."""
    return _cluster_len(token, 0) == len(token) > 0 and can_render(token)


def emoji_advance(cluster: str, px: int) -> int:
    """Horizontal advance for ``cluster`` at height ``px`` (glyph width + tracking).

    Computed arithmetically from the cached native glyph so the fit loop can probe
    many font sizes without re-rasterizing; matches ``render_emoji(cluster, px).width``.
    """
    px = max(1, px)
    glyph = _native_glyph(cluster)
    width = _scaled_glyph_width(glyph, px) if glyph is not None else px
    return width + emoji_tracking(px)
