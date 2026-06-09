from __future__ import annotations

from io import BytesIO

from PIL import Image


def downscale_png(data: bytes, max_long_edge: int) -> bytes:
    """Return PNG bytes whose longest edge is at most ``max_long_edge``.

    When the input is already within the cap it is returned unchanged, byte for
    byte (no re-encode, never upscaled); only an over-cap image is resampled with
    LANCZOS, preserving aspect ratio, and re-encoded (so its bytes differ).
    """
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if max(width, height) <= max_long_edge:
            return data
        image.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
