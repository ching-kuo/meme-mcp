from __future__ import annotations

import httpx
import pytest

from meme_mcp.app import GitHubOAuthHTTPClient


@pytest.mark.asyncio
async def test_github_oauth_client_exchanges_code_and_fetches_user() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-token"})
        if request.url.host == "api.github.com" and request.url.path == "/user":
            assert request.headers["authorization"] == "Bearer gh-token"
            return httpx.Response(200, json={"login": "friend"})
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    oauth = GitHubOAuthHTTPClient(
        client_id="cid",
        client_secret="secret-32-chars-value-for-tests",
        redirect_uri="http://localhost:8000/auth/callback",
        http_client=httpx.AsyncClient(transport=transport),
    )

    user = await oauth.fetch_user("ok-code", "verifier")

    assert user["login"] == "friend"
    assert {str(req.url) for req in requests} == {
        "https://github.com/login/oauth/access_token",
        "https://api.github.com/user",
    }
    token_request = next(
        req for req in requests if req.url.path == "/login/oauth/access_token"
    )
    assert token_request.headers["accept"] == "application/json"
    assert "code_verifier=verifier" in token_request.read().decode()


@pytest.mark.asyncio
async def test_github_oauth_client_rejects_missing_token() -> None:
    oauth = GitHubOAuthHTTPClient(
        client_id="cid",
        client_secret="secret-32-chars-value-for-tests",
        redirect_uri="http://localhost:8000/auth/callback",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"error": "bad_verification_code"})
            ),
        ),
    )

    with pytest.raises(ValueError, match="access_token"):
        await oauth.fetch_user("bad-code", "verifier")
