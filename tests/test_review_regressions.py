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
from meme_mcp.config import ConfigError, Settings
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.errors import MemeMCPError
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme


def settings(tmp_path, **overrides: object) -> Settings:
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
        "embedding_api_key": SecretStr("embedding-key"),
        # TestClient sends Host: testserver; allow it so MCP transport requests
        # reach the handler instead of being rejected by the rebinding guard.
        "mcp_allowed_hosts": ["testserver"],
        "mcp_allowed_origins": ["http://testserver"],
    }
    data.update(overrides)
    return Settings(**data)


def image_bytes() -> bytes:
    image = Image.new("RGB", (320, 180), "navy")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_app_wires_web_mcp_ready_and_authenticated_renders(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    image_path = app.state.image_store.put(image_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="drake",
            slug="drake",
            name="Drake",
            source="friend",
            metadata={
                "name": "Drake",
                "description": "reaction meme",
                "emotion": "dismissive",
                "usage_context": "comparison",
                "tags": ["reaction"],
                "format": "static",
            },
            slot_definitions=[{"position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )

    spec = TemplateSpec("drake", image_bytes(), [{"position": "top"}])
    result = render_meme(spec, ["hello"], app.state.image_store)
    app.state.receipts.record(result.hash, "drake", "alice")

    client = TestClient(app)
    assert client.get("/readyz").json() == {"ok": True}
    assert PIL.Image.MAX_IMAGE_PIXELS == 40 * 1024 * 1024
    assert client.get("/browse", follow_redirects=False).status_code == 303
    assert client.get("/api/mcp/tools").status_code == 401
    authed = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/mcp/tools", headers=authed).json()["data"]["tools"] == [
        "find",
        "generate",
        "record_outcome",
    ]
    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)
    assert client.post("/api/mcp/find", headers=authed, json={"query": "drake"}).status_code == 200
    dry_run = client.post(
        "/api/mcp/generate",
        headers=authed,
        json={"template_id": "drake", "slot_fills": ["x"], "dry_run": True},
    )
    assert dry_run.json()["data"]["rendered_url"] is None
    assert client.get(result.rendered_url, headers=authed).status_code == 200
    assert client.get(result.rendered_url).status_code == 401


def test_bare_mcp_path_is_not_a_307_redirect(tmp_path) -> None:
    # The MCP transport is mounted at /mcp with its inner route at /, so its
    # real endpoint is /mcp/. A bare /mcp must be served in-process (not
    # 307-redirected to /mcp/), since mcp-remote cannot replay a POST body
    # across the redirect and fails with code 307.
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    # The context-manager form runs the app lifespan, which starts the MCP
    # session manager's task group; without it the transport raises "Task group
    # is not initialized" on first request.
    with TestClient(app) as client:
        bare = client.post("/mcp", headers=headers, json=initialize, follow_redirects=False)
        # 200 proves the authenticated initialize reached the Streamable HTTP
        # handler: bare /mcp was not 307-redirected (mcp-remote can't replay a
        # POST across it) AND the session-manager task group was started by the
        # app lifespan (otherwise the handler raises -> 500).
        assert bare.status_code == 200
        slashed = client.post("/mcp/", headers=headers, json=initialize, follow_redirects=False)
        assert slashed.status_code == 200


def test_mcp_transport_rejects_disallowed_host(tmp_path) -> None:
    # The DNS-rebinding guard must 421 a Host that is not on mcp_allowed_hosts,
    # even for an authenticated request. settings() only allowlists "testserver".
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    with TestClient(app) as client:
        rejected = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Host": "evil.example.com",
            },
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            follow_redirects=False,
        )
        assert rejected.status_code == 421


def test_mcp_public_oauth_metadata_uses_public_host_and_root_well_known(tmp_path) -> None:
    app = create_app(settings(tmp_path, github_redirect_uri="https://meme.igene.tw/auth/callback"))
    initialize = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    with TestClient(app) as client:
        unauthorized = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=initialize,
            follow_redirects=False,
        )
        assert unauthorized.status_code == 401
        assert (
            unauthorized.headers["www-authenticate"]
            == 'Bearer error="invalid_token", error_description="Authentication required", '
            'resource_metadata="https://meme.igene.tw/.well-known/oauth-protected-resource/mcp"'
        )

        metadata = client.get("/.well-known/oauth-protected-resource/mcp", follow_redirects=False)
        assert metadata.status_code == 200
        assert metadata.headers["content-type"] == "application/json"
        assert metadata.json() == {
            "resource": "https://meme.igene.tw/mcp",
            "authorization_servers": ["https://meme.igene.tw/"],
            "scopes_supported": ["meme:read"],
            "bearer_methods_supported": ["header"],
        }


def test_malformed_github_redirect_uri_fails_fast(tmp_path) -> None:
    # A redirect URI that does not end in /auth/callback would make the base-URL
    # derivation a silent no-op and bake a broken path into the OAuth metadata;
    # create_app must reject it at startup instead.
    with pytest.raises(ConfigError, match="GITHUB_REDIRECT_URI"):
        create_app(settings(tmp_path, github_redirect_uri="https://meme.igene.tw/oauth/return"))


def test_pat_auth_uses_file_allowlist_written_by_operator_cli(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    (tmp_path / "allowlist.txt").write_text("alice\n", encoding="utf-8")

    response = TestClient(app).get("/api/mcp/tools", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_route_rate_limits_apply_before_find_work(tmp_path) -> None:
    app = create_app(settings(tmp_path, rate_find_per_min=1))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/mcp/find", headers=headers, json={"query": "x"}).status_code == 200
    limited = client.post("/api/mcp/find", headers=headers, json={"query": "x"})

    assert limited.status_code == 429


def test_dry_run_validates_template_and_slot_count(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    image_path = app.state.image_store.put(image_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="two-slot",
            slug="two-slot",
            name="Two Slot",
            source="friend",
            metadata={"name": "Two Slot", "tags": [], "format": "static"},
            slot_definitions=[{"position": "top"}, {"position": "bottom"}],
            image_path=image_path,
            perceptual_hash="1" * 16,
            exact_hash="b" * 64,
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    missing = client.post(
        "/api/mcp/generate",
        headers=headers,
        json={"template_id": "missing", "slot_fills": ["x"], "dry_run": True},
    )
    mismatch = client.post(
        "/api/mcp/generate",
        headers=headers,
        json={"template_id": "two-slot", "slot_fills": ["x"], "dry_run": True},
    )

    assert missing.status_code == 404
    assert mismatch.status_code == 400


def test_pat_store_persists_and_enforces_allowlist(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    token = issue_pat(store, "alice", "pepper")
    reopened = SQLitePatStore(tmp_path / "auth.db")
    assert verify_pat(reopened, token, "pepper") == ("alice", "readwrite")
    friend = require_pat(f"Bearer {token}", reopened, {"alice"}, "pepper")
    assert friend.github_login == "alice"
    assert friend.capability == "readwrite"
    with pytest.raises(MemeMCPError):
        require_pat(f"Bearer {token}", reopened, set(), "pepper")


def test_render_route_rejects_path_traversal(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("alice")
    secret = tmp_path / "secret.png"
    secret.write_bytes(image_bytes())
    fake_hash = "..secret"
    app.state.receipts.record(fake_hash, "any", "alice")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/renders/../secret.png", headers=headers)

    assert response.status_code in (404, 400)


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
