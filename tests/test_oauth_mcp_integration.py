"""Integration tests for AS-mode wiring (U5): origin-root route mirroring, metadata
correctness, DCR + authorize reachability, the flag-off contrast, and the bearer
401. Per-request token-validation semantics (PAT fallback, de-allowlist, scope,
resource binding) are covered at the provider layer in test_oauth_provider.py."""

from __future__ import annotations

import base64
import hashlib

from fastapi.testclient import TestClient
from pydantic import SecretStr

from meme_mcp.app import create_app
from tests.test_upload_flow import good_settings

# Host that satisfies the /mcp transport DNS-rebinding allowlist under TestClient.
BASE_URL = "http://localhost:8000"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _settings(tmp_path, enabled: bool):
    base = good_settings(tmp_path)
    if not enabled:
        return base
    return base.model_copy(
        update={
            "oauth_as_enabled": True,
            "oauth_token_pepper": SecretStr("oauth-token-pepper-32-chars-value-test"),
            "oauth_secret_enc_key": SecretStr("oauth-secret-enc-key-32-chars-value-test"),
        }
    )


def _client(tmp_path, enabled: bool) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, enabled)), base_url=BASE_URL)


def _challenge() -> str:
    verifier = "verifier-0123456789-0123456789-0123456789"
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


# -- flag off: no authorization server ----------------------------------------


def test_flag_off_has_no_authorization_server_routes(tmp_path) -> None:
    client = _client(tmp_path, enabled=False)
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert client.get("/authorize").status_code == 404
    # The RFC 9728 protected-resource doc is unchanged (resource-server-only mode).
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


# -- flag on: metadata + endpoints at the origin root -------------------------


def test_as_metadata_served_at_origin_root(tmp_path) -> None:
    client = _client(tmp_path, enabled=True)
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    meta = resp.json()
    # AnyHttpUrl serializes a bare origin with a trailing slash; the path
    # endpoints below do not (build_metadata rstrips before appending).
    assert meta["issuer"].rstrip("/") == BASE_URL
    assert meta["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert meta["token_endpoint"] == f"{BASE_URL}/token"
    assert meta["registration_endpoint"] == f"{BASE_URL}/register"
    assert meta["revocation_endpoint"] == f"{BASE_URL}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    # Public PKCE clients are accepted, so the advertised methods must include none.
    assert "none" in meta["token_endpoint_auth_methods_supported"]


def test_protected_resource_points_at_live_issuer(tmp_path) -> None:
    client = _client(tmp_path, enabled=True)
    doc = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert doc["resource"] == f"{BASE_URL}/mcp"
    assert [s.rstrip("/") for s in doc["authorization_servers"]] == [BASE_URL]


def test_register_and_authorize_resolve_at_origin_root(tmp_path) -> None:
    client = _client(tmp_path, enabled=True)
    reg = client.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"},
    )
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    assert reg.json()["token_endpoint_auth_method"] == "none"

    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
            "resource": f"{BASE_URL}/mcp",
        },
        follow_redirects=False,
    )
    # authorize() parks the request by nonce and redirects to the consent route
    # (not a 404 -- the functional endpoint is mirrored, not just the metadata).
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/oauth/consent/")


# -- bearer auth wiring -------------------------------------------------------


def test_unauthenticated_mcp_returns_401_with_resource_metadata(tmp_path) -> None:
    client = _client(tmp_path, enabled=True)
    resp = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "")


# -- per-IP rate limiting on the pre-auth endpoints (U6) ----------------------


def _rate_limited_client(tmp_path) -> TestClient:
    settings = _settings(tmp_path, enabled=True).model_copy(update={"rate_oauth_per_min": 2})
    return TestClient(create_app(settings), base_url=BASE_URL)


def test_register_rate_limited(tmp_path) -> None:
    client = _rate_limited_client(tmp_path)
    body = {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
    assert client.post("/register", json=body).status_code == 201
    assert client.post("/register", json=body).status_code == 201
    assert client.post("/register", json=body).status_code == 429  # third trips the limit


def test_token_rate_limited_per_ip(tmp_path) -> None:
    client = _rate_limited_client(tmp_path)
    bad = {"grant_type": "authorization_code", "code": "x", "client_id": "y", "code_verifier": "z"}
    # The first two reach the handler (rejected on their own merits, not 429);
    # the third is blocked by the per-IP limiter before the handler runs.
    for _ in range(2):
        assert client.post("/token", data=bad).status_code != 429
    assert client.post("/token", data=bad).status_code == 429
