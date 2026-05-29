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


def _analyze_for_approve(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    return client.post(
        "/api/uploads/analyze",
        headers=headers,
        json={
            "filename": "deploy.png",
            "mime": "image/png",
            "content_base64": base64.b64encode(png_bytes()).decode(),
            "title_hint": "Deploy Face",
        },
    ).json()["data"]


def test_upload_approval_rejects_blank_or_placeholder_name(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)
    headers = auth_headers(client)
    analyzed = _analyze_for_approve(client, headers)

    for bad_name in ["", "   ", "Uploaded Meme"]:
        metadata = dict(analyzed["metadata"])
        metadata["name"] = bad_name
        response = client.post(
            f"/api/uploads/{analyzed['pending_upload_id']}/approve",
            headers=headers,
            json={"metadata": metadata},
        )
        assert response.status_code == 400, bad_name
        body = response.json()
        assert body["error_code"] == "INVALID_INPUT"
        assert body["errors"] == [{"field": "name", "reason": "name_required"}]

    # No template was upserted for any of the rejected attempts.
    assert app.state.templates.list_rows() == []


def test_vlm_unavailable_placeholder_name_fails_even_with_ack(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = TimeoutVLMClient()
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
    # VLM-unavailable falls back to the placeholder name and a vlm_* suspect flag.
    assert analyzed["metadata"]["name"] == "Uploaded Meme"
    assert analyzed["suspect_flags"] == ["vlm_timeout"]

    # Even acknowledging the suspect flag, the placeholder name still fails the
    # independent name-required check (KTD7).
    response = client.post(
        f"/api/uploads/{analyzed['pending_upload_id']}/approve",
        headers=headers,
        json={"metadata": analyzed["metadata"], "ack_suspect": True},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "INVALID_INPUT"
    assert body["errors"] == [{"field": "name", "reason": "name_required"}]
    assert app.state.templates.list_rows() == []


def test_vlm_unavailable_with_real_name_and_ack_promotes(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = TimeoutVLMClient()
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

    metadata = dict(analyzed["metadata"])
    metadata["name"] = "Real Manual Name"
    response = client.post(
        f"/api/uploads/{analyzed['pending_upload_id']}/approve",
        headers=headers,
        json={"metadata": metadata, "ack_suspect": True},
    )
    assert response.status_code == 200
    template = app.state.templates.get(response.json()["data"]["template_id"])
    assert template.name == "Real Manual Name"
    assert template.source == "friend"
    # Pending row was deleted after approve.
    import pytest

    with pytest.raises(KeyError):
        app.state.pending_uploads.get(analyzed["pending_upload_id"], "friend")


def test_upload_rejects_mime_mismatch_through_service(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze",
        headers=auth_headers(client),
        json={
            "filename": "deploy.jpg",
            "mime": "image/jpeg",
            "content_base64": base64.b64encode(png_bytes()).decode(),
        },
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "UPLOAD_REJECTED"
    assert response.json()["errors"] == [{"field": "file", "reason": "mime_mismatch"}]


def test_upload_base64_garbage_charges_limiter_and_returns_invalid_input(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)
    headers = auth_headers(client)

    response = client.post(
        "/api/uploads/analyze",
        headers=headers,
        json={"filename": "x.png", "mime": "image/png", "content_base64": "not base64!!"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "INVALID_INPUT"
    assert body["errors"] == [{"field": "content_base64", "reason": "base64"}]
    # The shared service charges the limiter before decoding (KTD2 ordering): a
    # window now exists for the friend even though the request was rejected.
    _start, count = app.state.upload_limiter._windows["friend"]
    assert count == 1


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


def _ok_metadata(name: str = "Real Name") -> dict[str, Any]:
    return {
        "name": name,
        "description": "d",
        "emotion": "e",
        "usage_context": "u",
        "tags": ["x"],
        "format": "static",
        "slot_definitions": [{"name": "top", "position": "top"}],
    }


def test_validated_metadata_suspect_gate_precedes_name_check() -> None:
    import pytest

    from meme_mcp.errors import ErrorCode, MemeMCPError
    from meme_mcp.upload.service import _validated_metadata

    # With a suspect flag and no ack, the suspect gate fires first even though
    # the name is also blank, so callers learn about the ack requirement first.
    with pytest.raises(MemeMCPError) as exc_info:
        _validated_metadata(_ok_metadata(name=""), ["markup"], ack_suspect=False)
    assert exc_info.value.error_code == ErrorCode.VLM_OUTPUT_SUSPECT


def test_validated_metadata_name_check_runs_after_ack_passes() -> None:
    import pytest

    from meme_mcp.errors import ErrorCode, MemeMCPError
    from meme_mcp.upload.service import _validated_metadata

    # Acknowledging the suspect flag passes the ack gate, but the independent
    # name-required check then rejects the blank name (KTD7).
    with pytest.raises(MemeMCPError) as exc_info:
        _validated_metadata(_ok_metadata(name="   "), ["markup"], ack_suspect=True)
    assert exc_info.value.error_code == ErrorCode.INVALID_INPUT
    assert exc_info.value.errors == [{"field": "name", "reason": "name_required"}]


def test_validated_metadata_accepts_real_name() -> None:
    from meme_mcp.upload.service import _validated_metadata

    cleaned = _validated_metadata(_ok_metadata(name="Deploy Face"), [], ack_suspect=False)
    assert cleaned["name"] == "Deploy Face"
    assert cleaned["format"] == "static"
