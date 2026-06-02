from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.config import Settings
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate


def settings(tmp_path) -> Settings:
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
        embedding_api_key=SecretStr("embedding-key"),
    )


def png() -> bytes:
    image = Image.new("RGB", (320, 180), "navy")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def template_create(image_path: str) -> TemplateCreate:
    return TemplateCreate(
        template_id="ci-party",
        slug="ci-party",
        name="CI Party",
        source="friend",
        metadata={
            "description": "celebrate a clean CI run",
            "emotion": "celebration",
            "usage_context": "Rust build passed in continuous integration",
            "tags": ["rust", "ci-pass"],
            "format": "static",
            "engineering_context": {"language": "rust"},
        },
        slot_definitions=[{"name": "top", "position": "top"}],
        image_path=image_path,
        perceptual_hash="0" * 16,
        exact_hash="a" * 64,
    )


def test_template_repository_persists_and_searches(tmp_path) -> None:
    repo = SQLiteTemplateRepository(tmp_path / "templates.db")
    image_path = "ab/example.png"
    repo.upsert(template_create(image_path))
    reopened = SQLiteTemplateRepository(tmp_path / "templates.db")
    assert reopened.get("ci-party").name == "CI Party"
    results = reopened.search("celebrate rust", {"engineering_context.language": "rust"})
    assert [result.template_id for result in results] == ["ci-party"]


def test_app_mcp_find_and_generate_use_persisted_templates(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    image_path = app.state.image_store.put(png(), "png")
    app.state.templates.upsert(template_create(image_path))

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    found = client.post(
        "/api/mcp/find",
        headers=headers,
        json={"query": "clean rust ci", "filters": {"engineering_context.language": "rust"}},
    )
    assert found.status_code == 200
    assert found.json()["data"]["candidates"][0]["template_id"] == "ci-party"

    rendered = client.post(
        "/api/mcp/generate",
        headers=headers,
        json={"template_id": "ci-party", "slot_fills": ["ship it"]},
    )
    assert rendered.status_code == 200
    rendered_url = rendered.json()["data"]["rendered_url"]
    assert "sig=" in rendered_url and "exp=" in rendered_url
    # The signed receipt URL is fetchable with auth AND, crucially, without any
    # credential -- an image client cannot replay the caller's Bearer PAT.
    assert client.get(rendered_url, headers=headers).status_code == 200
    assert client.get(rendered_url).status_code == 200
    # A bad signature drops back to auth: rejected without a credential, but an
    # authenticated owner can still fetch (an expired URL must not lock them out).
    tampered = rendered_url.replace("sig=", "sig=x")
    assert client.get(tampered).status_code == 401
    assert client.get(tampered, headers=headers).status_code == 200
