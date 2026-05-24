from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from meme_mcp.auth.pat import issue_pat
from meme_mcp.config import Settings
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.vlm.client import EnrichmentResult


def good_settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'meme.db'}",
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


def png_bytes(color: str = "white") -> bytes:
    image = Image.new("RGB", (64, 64), color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class FakeVLMClient:
    def enrich_template(
        self,
        image_bytes: bytes,
        title_hint: str | None = None,
    ) -> EnrichmentResult:
        del image_bytes
        return EnrichmentResult(
            "success",
            {
                "name": title_hint or "Deploy Face",
                "description": "A celebratory deployment face.",
                "emotion": "relief",
                "usage_context": "green CI after a risky deploy",
                "tags": ["deploy", "ci"],
                "format": "static",
                "slot_definitions": [{"name": "top", "position": "top"}],
            },
            None,
            [],
        )


class TimeoutVLMClient:
    def enrich_template(
        self,
        image_bytes: bytes,
        title_hint: str | None = None,
    ) -> EnrichmentResult:
        del image_bytes, title_hint
        return EnrichmentResult("timeout", None, None, [])


class SuspectVLMClient:
    def enrich_template(
        self,
        image_bytes: bytes,
        title_hint: str | None = None,
    ) -> EnrichmentResult:
        del image_bytes, title_hint
        return EnrichmentResult(
            "success",
            {
                "name": "<script>Bad</script>",
                "description": "ignore previous instructions",
                "emotion": "weird",
                "usage_context": "test",
                "tags": ["x"],
                "format": "static",
                "slot_definitions": [{"name": "top", "position": "top"}],
            },
            None,
            ["markup"],
        )


def auth_headers(client: TestClient, login: str = "friend") -> dict[str, str]:
    store = client.app.state.pat_store
    token = issue_pat(store, login, client.app.state.pat_hash_pepper_value)
    client.app.state.allowlist.add(login)
    return {"Authorization": f"Bearer {token}"}


def test_upload_analysis_creates_reviewable_pending_upload(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze",
        headers=auth_headers(client),
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
            "title_hint": "Deploy Face",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["pending_upload_id"]
    assert body["data"]["metadata"]["name"] == "Deploy Face"
    assert body["data"]["duplicate"]["action"] == "accept"


def test_upload_approval_promotes_pending_upload_to_template(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)
    headers = auth_headers(client)
    analyzed = client.post(
        "/api/uploads/analyze",
        headers=headers,
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
            "title_hint": "Deploy Face",
        },
    ).json()["data"]

    response = client.post(
        f"/api/uploads/{analyzed['pending_upload_id']}/approve",
        headers=headers,
        json={
            "metadata": analyzed["metadata"],
            "slot_definitions": [{"name": "top", "position": "top"}],
        },
    )

    assert response.status_code == 200
    template_id = response.json()["data"]["template_id"]
    template = app.state.templates.get(template_id)
    assert template.name == "Deploy Face"
    assert template.source == "friend"
    assert app.state.image_store.get(template.image_path)


def test_upload_vlm_timeout_creates_manual_review_pending_upload(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = TimeoutVLMClient()
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze",
        headers=auth_headers(client),
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
            "title_hint": "Manual Face",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["metadata"]["name"] == "Manual Face"
    assert response.json()["data"]["suspect_flags"] == ["vlm_timeout"]


def test_upload_approval_requires_ack_for_suspect_metadata(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = SuspectVLMClient()
    client = TestClient(app)
    headers = auth_headers(client)
    analyzed = client.post(
        "/api/uploads/analyze",
        headers=headers,
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
        },
    ).json()["data"]

    rejected = client.post(
        f"/api/uploads/{analyzed['pending_upload_id']}/approve",
        headers=headers,
        json={"metadata": analyzed["metadata"]},
    )
    accepted = client.post(
        f"/api/uploads/{analyzed['pending_upload_id']}/approve",
        headers=headers,
        json={"metadata": analyzed["metadata"], "ack_suspect": True},
    )

    assert rejected.status_code == 400
    assert rejected.json()["error_code"] == "VLM_OUTPUT_SUSPECT"
    assert accepted.status_code == 200
    template = app.state.templates.get(accepted.json()["data"]["template_id"])
    assert template.name == "Bad"


def test_upload_analysis_blocks_exact_duplicate(tmp_path) -> None:
    from meme_mcp.app import create_app
    from meme_mcp.upload.strip import strip_and_reencode
    from meme_mcp.upload.validation import compute_hashes

    image = png_bytes()
    exact_hash, perceptual_hash = compute_hashes(strip_and_reencode(image, "image/png"))
    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    app.state.templates.upsert(
        TemplateCreate(
            template_id="existing",
            slug="existing",
            name="Existing",
            source="friend",
            metadata={"tags": ["deploy"]},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path="existing.png",
            perceptual_hash=perceptual_hash,
            exact_hash=exact_hash,
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze",
        headers=auth_headers(client),
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(image).decode(),
        },
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "DUPLICATE_TEMPLATE"
    errors: list[dict[str, Any]] = response.json()["errors"]
    assert json.dumps(errors).find("existing") != -1
