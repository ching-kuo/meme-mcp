"""Renderer rotation tests that do not depend on the upstream memegen clone.

The visual-parity suite at tests/test_visual_parity_golden.py exercises the rotated
template end-to-end via the cmm reference image, but skips when /tmp/memegen-upstream
is absent. These tests validate the angle != 0 code path with a synthetic image so the
rotation contract is locked in for environments without the clone.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, _has_rotation, render_meme


def _solid_image(size: tuple[int, int] = (400, 300)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (30, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _slot(angle: float) -> dict[str, object]:
    return {
        "name": "stamp",
        "position": "center",
        "box": {
            "anchor_x": 0.25,
            "anchor_y": 0.4,
            "scale_x": 0.5,
            "scale_y": 0.2,
            "align": "center",
            "angle": angle,
        },
    }


def _render(angle: float, tmp_path: Path) -> bytes:
    spec = TemplateSpec(
        template_id="rot-fixture",
        image_bytes=_solid_image(),
        slots=[_slot(angle)],
    )
    store = FilesystemImageStore(tmp_path / f"renders-{int(angle)}")
    return render_meme(spec, ["change my mind"], store).bytes


def test_has_rotation_detects_nonzero_angle() -> None:
    assert _has_rotation([_slot(0.0)]) is False
    assert _has_rotation([_slot(15.0)]) is True
    assert _has_rotation([_slot(0.0), _slot(23.0)]) is True


def test_zero_angle_keeps_rgb_hot_path(tmp_path: Path) -> None:
    rendered = _render(0.0, tmp_path)
    with Image.open(BytesIO(rendered)) as img:
        assert img.mode == "RGB"


def test_nonzero_angle_switches_to_rgba_and_changes_output(tmp_path: Path) -> None:
    upright = _render(0.0, tmp_path)
    rotated = _render(23.0, tmp_path)
    # Bytes must differ — rotation actually transforms pixels.
    assert upright != rotated
    with Image.open(BytesIO(rotated)) as img:
        assert img.mode == "RGBA"


def test_multiple_rotations_dont_collide(tmp_path: Path) -> None:
    spec = TemplateSpec(
        template_id="rot-multi",
        image_bytes=_solid_image(),
        slots=[
            {
                "name": "a",
                "position": "top",
                "box": {
                    "anchor_x": 0.05,
                    "anchor_y": 0.05,
                    "scale_x": 0.4,
                    "scale_y": 0.2,
                    "align": "left",
                    "angle": 10.0,
                },
            },
            {
                "name": "b",
                "position": "bottom",
                "box": {
                    "anchor_x": 0.55,
                    "anchor_y": 0.75,
                    "scale_x": 0.4,
                    "scale_y": 0.2,
                    "align": "right",
                    "angle": -10.0,
                },
            },
        ],
    )
    store = FilesystemImageStore(tmp_path / "renders-multi")
    rendered = render_meme(spec, ["one", "two"], store).bytes
    with Image.open(BytesIO(rendered)) as img:
        assert img.mode == "RGBA"
        assert img.size == (400, 300)
