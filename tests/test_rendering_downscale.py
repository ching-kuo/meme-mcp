from __future__ import annotations

from io import BytesIO

from PIL import Image

from meme_mcp.rendering.downscale import downscale_png


def _png(size: tuple[int, int], color: str = "white") -> bytes:
    image = Image.new("RGB", size, color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.size


def test_downscale_caps_long_edge_and_returns_valid_png() -> None:
    result = downscale_png(_png((200, 100)), 80)

    assert max(_size(result)) <= 80
    assert _size(result) == (80, 40)


def test_under_cap_image_is_byte_stable() -> None:
    data = _png((20, 10))

    assert downscale_png(data, 80) == data


def test_downscale_preserves_aspect_ratio_with_rounding() -> None:
    result = downscale_png(_png((300, 100)), 128)
    width, height = _size(result)

    assert width == 128
    assert abs((width / height) - 3.0) < 0.1


def test_larger_cap_does_not_upscale() -> None:
    data = _png((20, 10))

    assert downscale_png(data, 200) == data
