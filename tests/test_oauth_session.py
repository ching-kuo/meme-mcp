from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_upload_flow import good_settings


class FakeGitHubOAuth:
    def __init__(self, login: str) -> None:
        self.login = login

    async def fetch_user(self, code: str, code_verifier: str | None = None) -> dict[str, str]:
        assert code_verifier
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


def test_stale_callback_fails_after_newer_login(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)

    first_login = client.get("/auth/login", follow_redirects=False)
    stale_state = _extract_state(first_login)

    second_login = client.get("/auth/login", follow_redirects=False)
    fresh_state = _extract_state(second_login)
    assert stale_state != fresh_state

    stale_callback = client.get(f"/auth/callback?code=ok-code&state={stale_state}")
    assert stale_callback.status_code == 401
    assert stale_callback.json()["errors"][0] == {"field": "state", "reason": "mismatch"}

    fresh_callback = client.get(f"/auth/callback?code=ok-code&state={fresh_state}")
    assert fresh_callback.status_code == 200


def test_concurrent_login_tabs_invalidate_earlier_state(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    tab_one = TestClient(app)

    login_one = tab_one.get("/auth/login", follow_redirects=False)
    state_one = _extract_state(login_one)
    session_after_one = _current_session_cookie(tab_one)

    tab_two = TestClient(app)
    tab_two.cookies.set("session", session_after_one, domain="testserver.local")
    login_two = tab_two.get("/auth/login", follow_redirects=False)
    state_two = _extract_state(login_two)
    assert state_one != state_two
    session_after_two = _current_session_cookie(tab_two)
    assert session_after_two != session_after_one

    tab_one.cookies.clear()
    tab_one.cookies.set("session", session_after_two, domain="testserver.local")
    older_callback = tab_one.get(f"/auth/callback?code=ok-code&state={state_one}")
    assert older_callback.status_code == 401
    assert older_callback.json()["errors"][0] == {"field": "state", "reason": "mismatch"}


def _current_session_cookie(client: TestClient) -> str:
    values = [c.value for c in client.cookies.jar if c.name == "session"]
    return values[-1]


def _extract_state(response) -> str:
    location = response.headers["location"]
    return location.split("state=", 1)[1].split("&", 1)[0]
