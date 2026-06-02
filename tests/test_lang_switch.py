from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.web.csrf import DEFAULT_NEXT, safe_lang_return, safe_next
from tests.test_oauth_session import FakeGitHubOAuth, _extract_state
from tests.test_upload_flow import good_settings

# ---------------------------------------------------------------------------
# safe_lang_return (unit)
# ---------------------------------------------------------------------------


def test_lang_return_allows_landing() -> None:
    # The landing page "/" is on the switch allowlist (KTD7 gap), unlike the
    # login allowlist; it is the switch's default rather than /browse.
    assert safe_lang_return("/") == "/"


@pytest.mark.parametrize("raw", ["/browse", "/upload", "/account"])
def test_lang_return_allows_rendered_pages(raw: str) -> None:
    assert safe_lang_return(raw) == raw


def test_lang_return_preserves_query() -> None:
    assert safe_lang_return("/browse?q=cats") == "/browse?q=cats"
    assert safe_lang_return("/browse?q=cats&tag=ci") == "/browse?q=cats&tag=ci"


@pytest.mark.parametrize("raw", ["//evil.com", "https://evil.com", "/\\evil", "\x00/browse"])
def test_lang_return_rejects_open_redirect(raw: str) -> None:
    assert safe_lang_return(raw) == "/"


def test_lang_return_defaults_to_landing_for_unknown_path() -> None:
    # /auth/callback is intentionally NOT on the allowlist (KTD7): the restricted
    # page's switch falls back to "/" with no special-casing.
    assert safe_lang_return("/auth/callback?code=x&state=y") == "/"
    assert safe_lang_return("/nope") == "/"


def test_lang_return_accepts_account_even_though_session_gated() -> None:
    # The validator allows the path; the auth gate still applies at the route.
    assert safe_lang_return("/account") == "/account"


def test_query_borne_double_slash_stays_same_origin() -> None:
    # "//evil.com" inside the query of an allowlisted path is a same-origin query
    # value, not a redirect target -- preserved, not stripped.
    assert safe_lang_return("/browse?q=x&r=//evil.com") == "/browse?q=x&r=//evil.com"


def test_safe_next_default_allowlist_unchanged() -> None:
    # OAuth-flow regression guard: the refactor must not widen safe_next.
    assert safe_next("/browse") == "/browse"
    assert safe_next("/") == DEFAULT_NEXT  # landing not on the login allowlist
    assert safe_next("/browse?q=x") == "/browse"  # login flow still drops query


# ---------------------------------------------------------------------------
# /lang/{locale} route (integration)
# ---------------------------------------------------------------------------


def _set_cookie_header(response, name: str) -> str | None:
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(f"{name}="):
            return raw
    return None


def test_switch_sets_cookie_and_redirects(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/lang/zh-TW?next=/browse", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    cookie = _set_cookie_header(response, "lang")
    assert cookie is not None and "lang=zh-TW" in cookie


def test_cookie_then_render_is_chinese_regardless_of_header(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))
    client.get("/lang/zh-TW?next=/", follow_redirects=False)

    response = client.get("/", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert '<html lang="zh-TW">' in response.text


def test_unknown_locale_sets_no_cookie(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/lang/fr?next=/browse", follow_redirects=False)

    assert response.status_code == 303
    assert _set_cookie_header(response, "lang") is None


def test_switch_preserves_query_round_trip(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    # The switcher template emits a single percent-encoded next value; exercise
    # that exact form so the Starlette decode + safe_lang_return query
    # preservation are tested together, including a multi-param query that an
    # unencoded value would silently truncate.
    response = client.get(
        "/lang/zh-TW?next=%2Fbrowse%3Fq%3Dcats%26tag%3Dci", follow_redirects=False
    )

    assert response.headers["location"] == "/browse?q=cats&tag=ci"


def test_switch_open_redirect_falls_back(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    # Includes percent-encoded and scheme-bearing forms so the request-decoding
    # path (request.query_params decodes before safe_lang_return sees it) is
    # exercised, not just the already-decoded literals.
    attacker_forms = (
        "//evil.com",
        "https://evil.com",
        "%2F%2Fevil.com",  # decodes to //evil.com
        "%2f%2fevil.com",
        "/%5Cevil",  # decodes to /\evil (backslash trick)
        "data:text/html,evil",
        "https%3A%2F%2Fevil.com",
    )
    for bad in attacker_forms:
        response = client.get(f"/lang/en?next={bad}", follow_redirects=False)
        assert response.headers["location"] == "/", f"open redirect not blocked: {bad}"


def test_cookie_attributes_on_localhost(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/lang/en?next=/", follow_redirects=False)
    cookie = _set_cookie_header(response, "lang")

    assert cookie is not None
    assert "Path=/" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=31536000" in cookie
    # Secure is omitted on localhost (mirrors the session-cookie policy).
    assert "Secure" not in cookie


def test_cookie_secure_off_localhost(tmp_path) -> None:
    settings = good_settings(tmp_path).model_copy(
        update={"github_redirect_uri": "https://memes.example.test/auth/callback"}
    )
    client = TestClient(create_app(settings))

    response = client.get("/lang/en?next=/", follow_redirects=False)
    cookie = _set_cookie_header(response, "lang")

    assert cookie is not None and "Secure" in cookie


# ---------------------------------------------------------------------------
# Rendered switcher (template side)
# ---------------------------------------------------------------------------


def test_switcher_renders_on_anonymous_landing(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/", headers={"Accept-Language": "en"})

    assert response.status_code == 200
    assert "lang-switch" in response.text
    assert "English" in response.text
    assert "中文（繁體）" in response.text


def test_switcher_active_is_span_inactive_is_link(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/", headers={"Accept-Language": "en"})

    # Active (en) is a non-link span with aria-current; inactive (zh-TW) is a link.
    assert '<span class="lang-switch__option" aria-current="true">English</span>' in response.text
    assert 'href="/lang/zh-TW?next=' in response.text


def _login(client: TestClient) -> None:
    login = client.get("/auth/login", follow_redirects=False)
    callback = client.get(
        "/auth/callback?code=ok-code&state=" + _extract_state(login),
        follow_redirects=False,
    )
    assert callback.status_code == 303


def test_switcher_href_round_trips_query(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)

    response = client.get("/browse?q=cats&tag=ci", headers={"Accept-Language": "en"})

    assert response.status_code == 200
    # The next value is a single percent-encoded path+query; ? & = are encoded so
    # no param leaks to the top level of /lang/...
    assert "next=/browse%3Fq%3Dcats%26tag%3Dci" in response.text
    assert "next=/browse?q=cats&tag=ci" not in response.text
