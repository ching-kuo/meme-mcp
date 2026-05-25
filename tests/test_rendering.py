from io import BytesIO

from PIL import Image

from meme_mcp.rendering import pipeline
from meme_mcp.rendering.image_store import FilesystemImageStore, S3ImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme
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


def test_s3_store_is_v15_stub() -> None:
    store = S3ImageStore()
    try:
        store.put(b"x", "png")
    except NotImplementedError as exc:
        assert "v1.5" in str(exc)
    else:
        raise AssertionError("S3ImageStore must remain a v1.5 stub")


def test_render_meme_returns_stable_hash_and_alt_text(tmp_path) -> None:
    store = FilesystemImageStore(tmp_path)
    spec = TemplateSpec(
        template_id="drake",
        image_bytes=template_bytes(),
        slots=[{"name": "top", "position": "top"}, {"name": "bottom", "position": "bottom"}],
    )
    result = render_meme(spec, ["raw sql", "an orm"], store)
    again = render_meme(spec, ["raw sql", "an orm"], store)
    assert result.hash == again.hash
    assert len(result.hash) == 16
    assert "raw sql" in result.alt_text
    assert "an orm" in result.alt_text
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
