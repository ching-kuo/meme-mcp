from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat, verify_pat
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.web.csrf import CSRF_HEADER_NAME
from tests.test_upload_flow import good_settings
from tests.test_web_upload import CSRF_TOKEN, _session_client


class CaptureSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _make_app(tmp_path: Path):
    app = create_app(good_settings(tmp_path))
    app.state.allowlist.add("friend")
    app.state.audit_sink = CaptureSink()
    return app


def test_account_redirects_anonymous_to_login(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    response = TestClient(app).get("/account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/account"


def test_account_page_shows_status_without_plaintext(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    client = _session_client(app)

    response = client.get("/account")

    assert response.status_code == 200
    assert "active" in response.text
    assert "readwrite" in response.text
    assert token not in response.text


def test_account_page_offers_only_bounded_expiry(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)

    text = client.get("/account").text

    for days in (30, 90, 365):
        assert f'value="{days}"' in text
    # Never-expire (ttl_days=0) stays operator-CLI-only and must not be offered (R6).
    assert 'value="0"' not in text
    app = _make_app(tmp_path)
    old_token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    client = _session_client(app)

    response = client.post(
        "/account/token",
        headers={CSRF_HEADER_NAME: CSRF_TOKEN},
        json={"scope": "read", "ttl_days": 30},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    new_token = response.json()["data"]["token"]
    assert new_token
    assert verify_pat(app.state.pat_store, old_token, app.state.pat_hash_pepper_value) is None
    assert verify_pat(app.state.pat_store, new_token, app.state.pat_hash_pepper_value) == (
        "github:friend",
        "read",
    )
    assert app.state.audit_sink.events[0].event_type == "pat_issued"

    reloaded = client.get("/account")
    assert new_token not in reloaded.text


def test_post_revoke_requires_csrf_and_preserves_session(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    client = _session_client(app)

    missing_csrf = client.post("/account/token/revoke")
    assert missing_csrf.status_code == 403

    response = client.post("/account/token/revoke", headers={CSRF_HEADER_NAME: CSRF_TOKEN})

    assert response.status_code == 200
    assert response.json()["data"]["revoked"] is True
    assert verify_pat(app.state.pat_store, token, app.state.pat_hash_pepper_value) is None
    assert client.get("/account").status_code == 200


def test_pat_header_cannot_authenticate_post_routes(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    client = TestClient(app)

    for url, body in (
        ("/account/token", {"scope": "read", "ttl_days": 30}),
        ("/account/token/revoke", None),
    ):
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {token}", CSRF_HEADER_NAME: CSRF_TOKEN},
            json=body,
        )
        assert response.status_code == 401


def test_post_revoke_rejects_mismatched_csrf(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    client = _session_client(app)

    response = client.post("/account/token/revoke", headers={CSRF_HEADER_NAME: "wrong-token"})

    assert response.status_code == 403


def test_pat_admin_rate_limited(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app.state.pat_admin_limiter = WindowedRateLimiter(0, 3600)
    client = _session_client(app)

    response = client.post(
        "/account/token",
        headers={CSRF_HEADER_NAME: CSRF_TOKEN},
        json={"scope": "read", "ttl_days": 30},
    )

    assert response.status_code == 429


def test_browse_expiry_banner_links_to_account(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value, ttl_days=1)
    client = _session_client(app)

    response = client.get("/browse")

    assert response.status_code == 200
    assert 'href="/account"' in response.text
