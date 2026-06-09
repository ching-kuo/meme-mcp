from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from pydantic import SecretStr

from meme_mcp.app import AppMCPBackend, create_app
from meme_mcp.config import Settings
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.errors import ErrorCode, MemeMCPError


def _settings(tmp_path, **overrides: object) -> Settings:
    data = {
        "storage_dir": str(tmp_path),
        "database_url": f"sqlite:///{tmp_path / 'meme.db'}",
        "image_store_backend": "filesystem",
        "image_store_fs_path": str(tmp_path / "images"),
        "github_client_id": "cid",
        "github_client_secret": SecretStr("secret-32-chars-value-for-tests"),
        "github_redirect_uri": "http://localhost:8000/auth/callback",
        "github_allowlist_path": str(tmp_path / "allowlist.txt"),
        "operator_github_login": "operator",
        "session_secret": SecretStr("session-secret-32-chars-value-tests"),
        "pat_hash_pepper": SecretStr("pepper-secret-32-chars-value-tests"),
        "vlm_base_url": "https://example.test/v1",
        "vlm_api_key": SecretStr("vlm-key"),
        "vlm_model": "vlm-model",
        "embedding_base_url": "https://example.test/v1",
        "embedding_api_key": SecretStr("embedding-key"),
        "rate_find_per_min": 20,
        "rate_generate_per_min": 20,
        "rate_upload_per_hour": 2,
    }
    data.update(overrides)
    return Settings(**data)


def _png(size: tuple[int, int] = (80, 40), color: str = "white") -> bytes:
    image = Image.new("RGB", size, color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.size


def _seed_template(app, image_bytes: bytes | None = None) -> None:
    content = image_bytes or _png()
    image_path = app.state.image_store.put(content, "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="t1",
            slug="t1",
            name="T",
            source="memegen",
            metadata={},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="0" * 64,
        )
    )


def _assert_not_found(exc: pytest.ExceptionInfo[MemeMCPError]) -> None:
    assert exc.value.error_code is ErrorCode.NOT_FOUND
    assert exc.value.errors == [{"field": "hash", "reason": "render_missing_or_expired"}]


def test_display_render_returns_owned_generated_png(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    _seed_template(app)
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "friend")

    data = backend.display_render(receipt["data"]["hash"], "friend")

    assert _dimensions(data) == (80, 40)


def test_display_render_missing_after_gc_raises_not_found(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    _seed_template(app)
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "friend")
    render_hash = receipt["data"]["hash"]
    app.state.image_store.delete(f"{render_hash[:2]}/{render_hash[2:]}.png")
    app.state.receipts.delete(render_hash)

    with pytest.raises(MemeMCPError) as info:
        backend.display_render(render_hash, "friend")
    _assert_not_found(info)


def test_display_render_other_owner_raises_not_found(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    _seed_template(app)
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "alice")

    with pytest.raises(MemeMCPError) as info:
        backend.display_render(receipt["data"]["hash"], "bob")
    _assert_not_found(info)


def test_display_render_unknown_or_malformed_hash_raises_not_found(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    backend = AppMCPBackend(app)

    for render_hash in ("0" * 16, "../not-a-render"):
        with pytest.raises(MemeMCPError) as info:
            backend.display_render(render_hash, "friend")
        _assert_not_found(info)


def test_display_render_blob_missing_with_live_receipt_raises_not_found(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    _seed_template(app)
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "friend")
    render_hash = receipt["data"]["hash"]
    app.state.image_store.delete(f"{render_hash[:2]}/{render_hash[2:]}.png")

    with pytest.raises(MemeMCPError) as info:
        backend.display_render(render_hash, "friend")
    _assert_not_found(info)


def test_display_render_uses_principal_ownership_for_oauth_split_identity(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    _seed_template(app)
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "google:sub-A")

    assert _dimensions(backend.display_render(receipt["data"]["hash"], "google:sub-A")) == (80, 40)

    with pytest.raises(MemeMCPError) as info:
        backend.display_render(receipt["data"]["hash"], "oauth-client-id")
    _assert_not_found(info)


def test_display_render_uses_portable_image_store_get(tmp_path) -> None:
    class GetOnlyStore:
        def __init__(self, path: str, data: bytes) -> None:
            self.path = path
            self.data = data
            self.seen_path: str | None = None

        def get(self, path: str) -> bytes:
            self.seen_path = path
            if path != self.path:
                raise FileNotFoundError(path)
            return self.data

    app = create_app(_settings(tmp_path))
    render_hash = "0123456789abcdef"
    path = "01/23456789abcdef.png"
    store = GetOnlyStore(path, _png())
    app.state.image_store = store
    app.state.receipts.record(render_hash, "t1", "friend")

    data = AppMCPBackend(app).display_render(render_hash, "friend")

    assert _dimensions(data) == (80, 40)
    assert store.seen_path == path


def test_display_render_downscales_to_inline_cap(tmp_path) -> None:
    app = create_app(_settings(tmp_path, inline_image_max_px=60))
    _seed_template(app, _png((200, 100)))
    backend = AppMCPBackend(app)
    receipt = backend.generate("t1", ["hello"], False, "friend")

    data = backend.display_render(receipt["data"]["hash"], "friend")

    assert max(_dimensions(data)) <= 60
