from io import BytesIO

from PIL import Image

from meme_mcp.rendering.image_store import FilesystemImageStore, S3ImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme


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

