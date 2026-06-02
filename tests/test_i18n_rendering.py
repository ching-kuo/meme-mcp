from __future__ import annotations

from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from tests.test_oauth_session import FakeGitHubOAuth, _extract_state
from tests.test_upload_flow import good_settings

EN_TAGLINE = "A private meme studio for friends"
ZH_TAGLINE = "專為朋友打造的迷因工作室"


# ---------------------------------------------------------------------------
# Accept-Language negotiation end-to-end (settings-less app is enough: the
# context processor is attached to the templates instance, not app.state).
# ---------------------------------------------------------------------------


def test_landing_renders_chinese_for_zh_tw_header() -> None:
    client = TestClient(create_app())

    response = client.get("/", headers={"Accept-Language": "zh-Hant-TW,zh;q=0.9"})

    assert response.status_code == 200
    assert ZH_TAGLINE in response.text
    assert EN_TAGLINE not in response.text
    assert '<html lang="zh-TW">' in response.text


def test_landing_renders_english_for_en_header() -> None:
    client = TestClient(create_app())

    response = client.get("/", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert response.status_code == 200
    assert EN_TAGLINE in response.text
    assert ZH_TAGLINE not in response.text
    assert '<html lang="en">' in response.text


def test_cookie_precedence_over_accept_language() -> None:
    client = TestClient(create_app())
    client.cookies.set("lang", "en")

    response = client.get("/", headers={"Accept-Language": "zh-TW"})

    assert EN_TAGLINE in response.text
    assert '<html lang="en">' in response.text


def test_templated_response_carries_vary() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    vary = {v.strip().lower() for v in response.headers.get("vary", "").split(",")}
    assert "cookie" in vary
    assert "accept-language" in vary


# ---------------------------------------------------------------------------
# pat_routes.py renders through the same templates instance, so it must also
# receive the injected locale (R7 -- signed-in surface). No per-route plumbing.
# ---------------------------------------------------------------------------


def _login(client: TestClient) -> None:
    login = client.get("/auth/login", follow_redirects=False)
    callback = client.get(
        "/auth/callback?code=ok-code&state=" + _extract_state(login),
        follow_redirects=False,
    )
    assert callback.status_code == 303


def test_account_page_receives_injected_locale(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)

    response = client.get("/account", headers={"Accept-Language": "zh-TW"})

    assert response.status_code == 200
    # account.html copy is translated in U4; here we prove the context processor
    # reached a pat_routes render (no per-route plumbing regression).
    assert '<html lang="zh-TW">' in response.text
