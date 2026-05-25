from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from pydantic import SecretStr

from meme_mcp.app import AppMCPBackend, create_app
from meme_mcp.config import Settings
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.errors import ErrorCode, MemeMCPError


def _settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite:///{tmp_path / 'meme.db'}",
        image_store_backend="filesystem",
        image_store_fs_path=str(tmp_path / "images"),
        github_client_id="cid",
        github_client_secret=SecretStr("secret-32-chars-value-for-tests"),
        github_redirect_uri="http://localhost:8000/auth/callback",
        github_allowlist_path=str(tmp_path / "allowlist.txt"),
        operator_github_login="operator",
        session_secret=SecretStr("session-secret-32-chars-value-tests"),
        pat_hash_pepper=SecretStr("pepper-secret-32-chars-value-tests"),
        vlm_base_url="https://example.test/v1",
        vlm_api_key=SecretStr("vlm-key"),
        vlm_model="vlm-model",
        embedding_base_url="https://example.test/v1",
        embedding_api_key=SecretStr("embedding-key"),
        rate_find_per_min=2,
        rate_generate_per_min=2,
        rate_upload_per_hour=2,
    )


def _png(tmp_path) -> str:
    image = Image.new("RGB", (50, 50), "white")
    buf = BytesIO()
    image.save(buf, format="PNG")
    path = "ab/cdef.png"
    full = tmp_path / "images" / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(buf.getvalue())
    return path


def test_mcp_find_is_rate_limited_per_actor(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    backend = AppMCPBackend(app)

    backend.find("a", None, "friend")
    backend.find("b", None, "friend")
    with pytest.raises(MemeMCPError) as info:
        backend.find("c", None, "friend")
    assert info.value.error_code is ErrorCode.RATE_LIMITED


def test_mcp_generate_is_rate_limited_per_actor(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    image_rel = _png(tmp_path)
    app.state.templates.upsert(
        TemplateCreate(
            template_id="t1",
            slug="t1",
            name="T",
            source="memegen",
            metadata={},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_rel,
            perceptual_hash="0" * 16,
            exact_hash="0" * 64,
        )
    )
    backend = AppMCPBackend(app)

    backend.generate("t1", ["hello"], True, "friend")
    backend.generate("t1", ["hello"], True, "friend")
    with pytest.raises(MemeMCPError) as info:
        backend.generate("t1", ["hello"], True, "friend")
    assert info.value.error_code is ErrorCode.RATE_LIMITED


def test_mcp_find_limiter_partitions_by_actor(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    backend = AppMCPBackend(app)

    for _ in range(2):
        backend.find("q", None, "alice")
    backend.find("q", None, "bob")  # different actor — fresh budget
