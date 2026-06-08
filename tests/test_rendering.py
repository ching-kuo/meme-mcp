from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from meme_mcp.rendering import pipeline
from meme_mcp.rendering.image_store import FilesystemImageStore, S3ImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme
from meme_mcp.rendering.text_layout import fit_font, select_wrap


def template_bytes() -> bytes:
    image = Image.new("RGB", (400, 240), "navy")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_filesystem_store_is_content_addressed(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path)
    first = store.put(b"same", "png")
    second = store.put(b"same", "png")
    assert first == second
    assert (tmp_path / first).exists()


def test_filesystem_path_for_matches_put(tmp_path) -> None:
    # analyze records path_for() into the pending row BEFORE put() writes the blob;
    # the two must agree or the row would reference a path the blob never lands at.
    store = FilesystemImageStore(tmp_path)
    content = b"hello content addressing"
    assert store.path_for(content, "png") == store.put(content, "png")


def test_filesystem_delete_removes_blob_and_get_then_raises(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path)
    path = store.put(b"to be deleted", "png")
    assert store.delete(path) is True
    with pytest.raises(FileNotFoundError):
        store.get(path)


def test_filesystem_delete_absent_path_returns_false(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path)
    assert store.delete("ab/cdef0123456789.png") is False


def test_filesystem_delete_rejects_traversal(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path / "images")
    # A sibling file outside the store root must never be unlinked.
    outside = tmp_path / "secret.txt"
    outside.write_text("keep me")
    assert store.delete("../secret.txt") is False
    assert store.delete("../../etc/passwd") is False
    assert outside.exists()


def test_s3_image_store_requires_construction_kwargs() -> None:
    """S3ImageStore is live as of U15; this regression check ensures it cannot be
    instantiated without the connection config, surfacing a TypeError at construction
    rather than NoSuchKey from boto3 mid-request."""
    with pytest.raises(TypeError):
        S3ImageStore()  # type: ignore[call-arg]


def test_render_meme_returns_stable_hash_and_alt_text(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path)
    spec = TemplateSpec(
        template_id="drake",
        image_bytes=template_bytes(),
        slots=[{"name": "top", "position": "top"}, {"name": "bottom", "position": "bottom"}],
    )
    result = render_meme(spec, ["raw sql", "an orm"], store, "http://localhost:8000")
    again = render_meme(spec, ["raw sql", "an orm"], store, "http://localhost:8000")
    assert result.hash == again.hash
    assert len(result.hash) == 16
    assert "raw sql" in result.alt_text
    assert "an orm" in result.alt_text
    assert result.rendered_url == f"http://localhost:8000/renders/{result.path}"
    assert (tmp_path / result.path).exists()


def test_slot_anchor_uses_box_geometry() -> None:
    slot = {
        "position": "top",
        "box": {
            "anchor_x": 0.0,
            "anchor_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 0.2,
            "align": "center",
            "angle": 0.0,
        },
    }
    assert pipeline._slot_anchor(slot, (400, 240)) == (200, 24, "mm", (400, 48))


def test_slot_anchor_honors_left_alignment() -> None:
    slot = {
        "position": "middle-left",
        "box": {
            "anchor_x": 0.12,
            "anchor_y": 0.7,
            "scale_x": 0.325,
            "scale_y": 0.1,
            "align": "left",
            "angle": 0.0,
        },
    }
    assert pipeline._slot_anchor(slot, (1000, 500)) == (120, 375, "lm", (325, 50))


def test_legacy_slot_xy_preserves_old_coordinates() -> None:
    assert pipeline._slot_xy(400, 240, "top") == (200, 43, "mm")
    assert pipeline._slot_xy(400, 240, "bottom") == (200, 187, "mm")


def test_fit_font_scales_to_box() -> None:
    font = fit_font("HI", (400, 80), pipeline._font_path(), 66)
    assert font.size >= 60


def test_fit_font_shrinks_long_text() -> None:
    font = fit_font("X" * 30, (400, 80), pipeline._font_path(), 66)
    assert 12 <= font.size < 60


def test_select_wrap_keeps_short_text_single_line() -> None:
    lines, _font = select_wrap("yes", (400, 80), pipeline._font_path(), 66)
    assert lines == ["yes"]


def test_select_wrap_uses_multiple_lines_for_long_text() -> None:
    lines, _font = select_wrap(
        "if logs are so smart why cant they find bugs", (160, 80), pipeline._font_path(), 66
    )
    assert len(lines) >= 2


def _navy_template(width: int, height: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), "navy").save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize(
    "anchor_y",
    [0.0, 0.8],  # boxes flush to the top and bottom image edges
)
def test_long_caption_in_edge_anchored_box_is_not_clipped(anchor_y: float) -> None:
    # Regression: layout under-measured rendered height (summed tight glyph bounds
    # instead of PIL's real line metrics), so a long caption in a short edge-anchored
    # box (scale_y 0.2) overflowed past the image edge and the renderer clipped it.
    box = {
        "anchor_x": 0.0,
        "anchor_y": anchor_y,
        "scale_x": 1.0,
        "scale_y": 0.2,
        "align": "center",
    }
    spec = TemplateSpec(
        template_id="clip-test",
        image_bytes=_navy_template(500, 500),
        slots=[{"name": "slot", "position": "top", "box": box}],
    )
    image = Image.open(BytesIO(preview_transient(spec, ["X" * 80]))).convert("RGB")
    arr = np.asarray(image)
    caption_ink = arr.min(axis=2) > 200  # white fill of the caption
    # No caption pixels may bleed into the extreme edge rows -> nothing was clipped.
    assert caption_ink[0].sum() == 0
    assert caption_ink[-1].sum() == 0
