from __future__ import annotations

import httpx
import pytest

from meme_mcp.app import GitHubOAuthHTTPClient


@pytest.mark.asyncio
async def test_github_oauth_client_exchanges_code_and_fetches_user() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-token"})
        if request.url.path == "/user":
            assert request.headers["authorization"] == "Bearer gh-token"
            return httpx.Response(200, json={"login": "friend"})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://github.com",
    )
    oauth = GitHubOAuthHTTPClient(
        client_id="cid",
        client_secret="secret-32-chars-value-for-tests",
        redirect_uri="http://localhost:8000/auth/callback",
        http_client=client,
    )

    user = await oauth.fetch_user("ok-code", "verifier")

    assert user["login"] == "friend"
    token_request = requests[0]
    assert token_request.headers["accept"] == "application/json"
    assert token_request.read().decode().find("code_verifier=verifier") != -1


@pytest.mark.asyncio
async def test_github_oauth_client_rejects_missing_token() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"error": "bad_verification_code"})
        ),
        base_url="https://github.com",
    )
    oauth = GitHubOAuthHTTPClient(
        client_id="cid",
        client_secret="secret-32-chars-value-for-tests",
        redirect_uri="http://localhost:8000/auth/callback",
        http_client=client,
    )

    with pytest.raises(ValueError, match="access_token"):
        await oauth.fetch_user("bad-code", "verifier")
