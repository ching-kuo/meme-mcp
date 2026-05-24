from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from tests.test_upload_flow import good_settings, png_bytes


def authed_client(tmp_path) -> tuple[TestClient, dict[str, str]]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="deploy-face",
            slug="deploy-face",
            name="Deploy Face",
            source="friend",
            metadata={
                "name": "Deploy Face",
                "description": "deploy relief",
                "emotion": "relief",
                "usage_context": "green CI",
                "tags": ["deploy", "ci"],
                "format": "static",
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_browse_renders_authenticated_template_list(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Deploy Face" in response.text


def test_template_api_searches_and_previews_without_persistence(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    listed = client.get("/api/templates?q=deploy", headers=headers)
    preview = client.post(
        "/api/templates/deploy-face/preview",
        headers=headers,
        json={"slot_fills": ["ship it"]},
    )

    assert listed.status_code == 200
    assert listed.json()["data"]["templates"][0]["template_id"] == "deploy-face"
    assert preview.status_code == 200
    data_url = preview.json()["data"]["data_url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]).startswith(b"\x89PNG")
