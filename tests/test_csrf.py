from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.web.csrf import (
    CSRF_HEADER_NAME,
    CSRF_SESSION_KEY,
    DEFAULT_NEXT,
    ensure_csrf_token,
    require_csrf,
    safe_next,
)
from tests.test_oauth_session import FakeGitHubOAuth, _extract_state
from tests.test_upload_flow import good_settings


class _FakeRequest:
    """Minimal Request stand-in exposing ``session`` and ``headers``."""

    def __init__(self, session: dict[str, object], headers: dict[str, str]) -> None:
        self.session = session
        self.headers = headers


def _request(session: dict[str, object], headers: dict[str, str] | None = None) -> _FakeRequest:
    return _FakeRequest(session, headers or {})


# ---------------------------------------------------------------------------
# ensure_csrf_token
# ---------------------------------------------------------------------------


def test_ensure_csrf_token_mints_when_absent() -> None:
    session: dict[str, object] = {}

    token = ensure_csrf_token(session)

    assert isinstance(token, str)
    assert token
    assert session[CSRF_SESSION_KEY] == token


def test_ensure_csrf_token_is_stable_within_session() -> None:
    session: dict[str, object] = {}

    first = ensure_csrf_token(session)
    second = ensure_csrf_token(session)

    assert first == second


def test_ensure_csrf_token_replaces_blank_value() -> None:
    session: dict[str, object] = {CSRF_SESSION_KEY: ""}

    token = ensure_csrf_token(session)

    assert token
    assert session[CSRF_SESSION_KEY] == token


def test_ensure_csrf_token_regenerated_across_fresh_session() -> None:
    first_session: dict[str, object] = {}
    first = ensure_csrf_token(first_session)

    fresh_session: dict[str, object] = {}
    second = ensure_csrf_token(fresh_session)

    assert first != second


# ---------------------------------------------------------------------------
# require_csrf
# ---------------------------------------------------------------------------


def test_require_csrf_passes_with_matching_header() -> None:
    session: dict[str, object] = {}
    token = ensure_csrf_token(session)
    request = _request(session, {CSRF_HEADER_NAME: token})

    require_csrf(request)  # type: ignore[arg-type]


def test_require_csrf_rejects_missing_header() -> None:
    session: dict[str, object] = {}
    ensure_csrf_token(session)
    request = _request(session, {})

    with pytest.raises(MemeMCPError) as exc:
        require_csrf(request)  # type: ignore[arg-type]

    assert exc.value.error_code is ErrorCode.FORBIDDEN
    assert exc.value.errors[0] == {"field": "csrf", "reason": "missing"}


def test_require_csrf_rejects_when_session_token_absent() -> None:
    request = _request({}, {CSRF_HEADER_NAME: "some-token"})

    with pytest.raises(MemeMCPError) as exc:
        require_csrf(request)  # type: ignore[arg-type]

    assert exc.value.error_code is ErrorCode.FORBIDDEN
    assert exc.value.errors[0] == {"field": "csrf", "reason": "missing"}


def test_require_csrf_rejects_mismatched_header() -> None:
    session: dict[str, object] = {}
    ensure_csrf_token(session)
    request = _request(session, {CSRF_HEADER_NAME: "wrong-token"})

    with pytest.raises(MemeMCPError) as exc:
        require_csrf(request)  # type: ignore[arg-type]

    assert exc.value.error_code is ErrorCode.FORBIDDEN
    assert exc.value.errors[0] == {"field": "csrf", "reason": "mismatch"}


def test_require_csrf_rejects_blank_session_token() -> None:
    session: dict[str, object] = {CSRF_SESSION_KEY: ""}
    request = _request(session, {CSRF_HEADER_NAME: ""})

    with pytest.raises(MemeMCPError) as exc:
        require_csrf(request)  # type: ignore[arg-type]

    assert exc.value.error_code is ErrorCode.FORBIDDEN


# ---------------------------------------------------------------------------
# safe_next
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["/upload", "/browse"])
def test_safe_next_allows_allowlisted_paths(raw: str) -> None:
    assert safe_next(raw) == raw


@pytest.mark.parametrize(
    "raw",
    ["/templates/deploy-face", "/templates/uploaded-meme-ab12cd34"],
)
def test_safe_next_allows_template_detail_paths(raw: str) -> None:
    # Detail pages are shareable, so login returns the friend to the exact one.
    assert safe_next(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "/templates/deploy-face/image",  # binary sub-route, not the detail page
        "/templates/",  # empty id
        "/templates/..",  # normalizes to a parent path
        "/templates/.",
    ],
)
def test_safe_next_rejects_non_detail_template_paths(raw: str) -> None:
    assert safe_next(raw) == DEFAULT_NEXT


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.com",
        "https://evil.com",
        "http://evil.com/upload",
        "/\\evil",
        "\x00/upload",
        " /upload",
        "\t/upload",
        "/upload\n",
        "",
        "   ",
        "/admin",
        "/upload/../browse",
        "javascript:alert(1)",
        "upload",
        None,
        123,
    ],
)
def test_safe_next_falls_back_to_default(raw: object) -> None:
    assert safe_next(raw) == DEFAULT_NEXT


def test_safe_next_rejects_path_with_query_outside_allowlist() -> None:
    # "/upload?x=1" splits to path "/upload"; query is dropped, path matches.
    assert safe_next("/upload?next=/browse") == "/upload"


# ---------------------------------------------------------------------------
# logout enforces CSRF (integration)
# ---------------------------------------------------------------------------


def _login(client: TestClient) -> str:
    login = client.get("/auth/login", follow_redirects=False)
    callback = client.get(
        "/auth/callback?code=ok-code&state=" + _extract_state(login),
        follow_redirects=False,
    )
    # The callback now redirects to a validated return path (U6) instead of
    # returning a JSON envelope; the session is established alongside it.
    assert callback.status_code == 303
    return "friend"


def test_logout_rejected_without_csrf_header(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)

    response = client.post("/auth/logout")

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    # Session still valid: an authed page is reachable.
    assert client.get("/browse").status_code == 200


def test_logout_rejected_with_wrong_csrf_header(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)
    # Seed a session CSRF token through a request that mints one would require
    # the /upload GET (U8); here we assert that an arbitrary header without a
    # matching session token is rejected.

    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: "not-the-token"})

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert client.get("/browse").status_code == 200


def _csrf_from_nav(html: str) -> str:
    match = re.search(r'data-logout data-csrf="([^"]+)"', html)
    assert match, "logout button with a CSRF token should render in the signed-in nav"
    return match.group(1)


def test_browse_nav_renders_logout_button_with_token(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)

    html = client.get("/browse").text

    # The button carries the per-session token the JS handler sends as the
    # X-CSRF-Token header (the route has no form-field fallback).
    token = _csrf_from_nav(html)
    assert token


def test_landing_nav_logout_button_has_working_token(tmp_path) -> None:
    # Regression: a signed-in visitor sees the nav on "/" too. If the landing
    # context omits csrf_token, data-csrf renders empty and logout 403s.
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)

    token = _csrf_from_nav(client.get("/").text)

    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: token})
    assert response.status_code == 200


def test_logout_with_csrf_clears_session(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)
    token = _csrf_from_nav(client.get("/browse").text)

    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: token})

    assert response.status_code == 200
    assert response.json()["data"]["logged_out"] is True
    # Session cleared: /browse now bounces an unauthenticated browser to login.
    after = client.get("/browse", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"] == "/auth/login?next=/browse"
