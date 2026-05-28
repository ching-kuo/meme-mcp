from __future__ import annotations

from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from tests.test_upload_flow import good_settings, png_bytes


def _client_with_token(tmp_path, *, capability: str) -> tuple[TestClient, str]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(
        app.state.pat_store,
        "friend",
        app.state.pat_hash_pepper_value,
        capability=capability,  # type: ignore[arg-type]
    )
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="single",
            slug="single",
            name="Single",
            source="friend",
            metadata={"name": "Single", "description": "d", "emotion": "e",
                      "usage_context": "u", "tags": [], "format": "static"},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    return TestClient(app), token


def test_read_scope_pat_can_call_mcp_find(tmp_path) -> None:
    client, token = _client_with_token(tmp_path, capability="read")
    response = client.post(
        "/api/mcp/find",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "single"},
    )
    assert response.status_code == 200


def test_read_scope_pat_rejected_from_mcp_generate(tmp_path) -> None:
    client, token = _client_with_token(tmp_path, capability="read")
    response = client.post(
        "/api/mcp/generate",
        headers={"Authorization": f"Bearer {token}"},
        json={"template_id": "single", "slot_fills": ["x"]},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert "capability" in str(body)


def test_readwrite_scope_pat_can_call_mcp_generate(tmp_path) -> None:
    client, token = _client_with_token(tmp_path, capability="readwrite")
    response = client.post(
        "/api/mcp/generate",
        headers={"Authorization": f"Bearer {token}"},
        json={"template_id": "single", "slot_fills": ["x"]},
    )
    assert response.status_code == 200


def test_read_scope_pat_rejected_from_uploads_analyze(tmp_path) -> None:
    client, token = _client_with_token(tmp_path, capability="read")
    import base64
    response = client.post(
        "/api/uploads/analyze",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filename": "x.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
        },
    )
    assert response.status_code == 401


def test_read_scope_pat_rejected_from_uploads_approve(tmp_path) -> None:
    client, token = _client_with_token(tmp_path, capability="read")
    response = client.post(
        "/api/uploads/some-id/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 401
