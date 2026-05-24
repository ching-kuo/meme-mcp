from __future__ import annotations

import time
from io import BytesIO

import PIL.Image
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from meme_mcp.app import create_app
from meme_mcp.auth.depends import require_pat
from meme_mcp.auth.pat import SQLitePatStore, issue_pat, verify_pat
from meme_mcp.config import Settings
from meme_mcp.errors import MemeMCPError
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme


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


def image_bytes() -> bytes:
    image = Image.new("RGB", (320, 180), "navy")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_app_wires_web_mcp_ready_and_authenticated_renders(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")

    spec = TemplateSpec("drake", image_bytes(), [{"position": "top"}])
    result = render_meme(spec, ["hello"], app.state.image_store)
    app.state.receipts.record(result.hash, "drake", "alice")

    client = TestClient(app)
    assert client.get("/readyz").json() == {"ok": True}
    assert PIL.Image.MAX_IMAGE_PIXELS == 40 * 1024 * 1024
    assert client.get("/browse").status_code == 401
    assert client.get("/mcp/tools").status_code == 401
    authed = {"Authorization": f"Bearer {token}"}
    assert client.get("/mcp/tools", headers=authed).json()["data"]["tools"] == ["find", "generate"]
    assert client.post("/mcp/find", headers=authed, json={"query": "drake"}).status_code == 200
    dry_run = client.post(
        "/mcp/generate",
        headers=authed,
        json={"template_id": "drake", "slot_fills": ["x"], "dry_run": True},
    )
    assert dry_run.json()["data"]["rendered_url"] is None
    assert client.get(result.rendered_url, headers=authed).status_code == 200
    assert client.get(result.rendered_url).status_code == 401


def test_pat_store_persists_and_enforces_allowlist(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    token = issue_pat(store, "alice", "pepper")
    reopened = SQLitePatStore(tmp_path / "auth.db")
    assert verify_pat(reopened, token, "pepper") == "alice"
    friend = require_pat(f"Bearer {token}", reopened, {"alice"}, "pepper")
    assert friend.github_login == "alice"
    with pytest.raises(MemeMCPError):
        require_pat(f"Bearer {token}", reopened, set(), "pepper")


def test_preview_transient_does_not_write_files(tmp_path) -> None:
    spec = TemplateSpec("sample", image_bytes(), [{"position": "top"}])
    before = set(tmp_path.rglob("*"))
    rendered = preview_transient(spec, ["preview"])
    after = set(tmp_path.rglob("*"))
    assert rendered.startswith(b"\x89PNG")
    assert after == before


def test_windowed_rate_limiter_resets_after_window() -> None:
    limiter = WindowedRateLimiter(limit=2, window_seconds=1, clock=time.monotonic)
    limiter.hit("alice")
    limiter.hit("alice")
    with pytest.raises(MemeMCPError):
        limiter.hit("alice")
    time.sleep(1.05)
    limiter.hit("alice")


def test_deployment_manifests_include_planned_files() -> None:
    required = [
        "deploy/k8s/ingress.yaml",
        "deploy/k8s/configmap.yaml",
        "deploy/k8s/pvc.yaml",
        "deploy/k8s/cnpg-cluster.example.yaml",
    ]
    for path in required:
        assert __import__("pathlib").Path(path).exists(), path
