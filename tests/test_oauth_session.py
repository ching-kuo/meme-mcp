from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_upload_flow import good_settings


class FakeGitHubOAuth:
    def __init__(self, login: str) -> None:
        self.login = login

    async def fetch_user(self, code: str) -> dict[str, str]:
        assert code == "ok-code"
        return {"login": self.login}


def test_oauth_callback_creates_allowlisted_session(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)

    login = client.get("/auth/login", follow_redirects=False)
    state = client.cookies.get("session")
    assert login.status_code == 307
    assert "github.com/login/oauth/authorize" in login.headers["location"]
    assert state

    callback = client.get("/auth/callback?code=ok-code&state=" + _extract_state(login))

    assert callback.status_code == 200
    assert callback.json()["data"]["github_login"] == "friend"
    assert client.get("/browse").status_code == 200


def test_oauth_callback_rejects_non_allowlisted_user(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("stranger")
    client = TestClient(app)
    login = client.get("/auth/login", follow_redirects=False)

    response = client.get("/auth/callback?code=ok-code&state=" + _extract_state(login))

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN_NOT_ALLOWLISTED"


def test_oauth_callback_rejects_state_mismatch(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    client = TestClient(app)
    client.get("/auth/login", follow_redirects=False)

    response = client.get("/auth/callback?code=ok-code&state=wrong")

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


def _extract_state(response) -> str:
    location = response.headers["location"]
    return location.split("state=", 1)[1].split("&", 1)[0]
