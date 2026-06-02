from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.web.i18n.catalog import MESSAGES
from tests.test_oauth_session import FakeGitHubOAuth, _extract_state
from tests.test_upload_flow import good_settings

EN_TAGLINE = "A private meme studio for friends"
ZH_TAGLINE = "專為朋友打造的迷因工作室"

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src/meme_mcp/web/templates"


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


# ---------------------------------------------------------------------------
# U4: per-page translation, plural/interpolation, and static guards
# ---------------------------------------------------------------------------

ZH = {"Accept-Language": "zh-TW"}
EN = {"Accept-Language": "en"}


def _authed_client(tmp_path, login: str = "friend") -> tuple[TestClient, object]:
    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth(login)
    (tmp_path / "allowlist.txt").write_text(f"{login}\n", encoding="utf-8")
    client = TestClient(app)
    _login(client)
    return client, app


def _seed_template(app) -> None:
    app.state.templates.upsert(
        TemplateCreate(
            template_id="ci-party",
            slug="ci-party",
            name="CI Party",
            source="friend",
            metadata={
                "description": "celebrate a clean CI run",
                "emotion": "celebration",
                "usage_context": "build passed",
                "tags": ["ci"],
                "format": "static",
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path="ab/example.png",
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )


def _assert_no_raw_keys(text: str) -> None:
    # A t() fallback to the literal key would leak the dotted id into the page.
    for key in MESSAGES:
        assert key not in text, f"raw catalog key leaked into render: {key}"


def test_landing_anonymous_zh(tmp_path) -> None:
    client = TestClient(create_app(good_settings(tmp_path)))

    response = client.get("/", headers=ZH)

    assert "使用 GitHub 登入" in response.text
    assert "Sign in with GitHub" not in response.text
    _assert_no_raw_keys(response.text)


def test_restricted_page_translated(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("stranger")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    login = client.get("/auth/login", follow_redirects=False)
    response = client.get(
        "/auth/callback?code=ok-code&state=" + _extract_state(login),
        headers=ZH,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "存取受限" in response.text
    assert "Access restricted" not in response.text
    # Interpolation through a real render: the operator login sits in <code>
    # between the translated prefix/suffix.
    assert "若要申請存取權" in response.text
    assert "operator" in response.text
    _assert_no_raw_keys(response.text)


def test_browse_empty_translated(tmp_path) -> None:
    client, _ = _authed_client(tmp_path)

    response = client.get("/browse", headers=ZH)

    assert "範本庫" in response.text
    assert "Template library" not in response.text
    assert "目前還沒有範本。" in response.text
    _assert_no_raw_keys(response.text)


def test_browse_plural_count(tmp_path) -> None:
    client, app = _authed_client(tmp_path)
    _seed_template(app)

    zh = client.get("/browse", headers=ZH)
    en = client.get("/browse", headers=EN)

    assert "1 個範本可供使用" in zh.text
    assert "1 template ready to render" in en.text


def test_account_status_localized(tmp_path) -> None:
    client, _ = _authed_client(tmp_path)

    response = client.get("/account", headers=ZH)

    assert "MCP 存取權杖" in response.text
    assert "MCP access token" not in response.text
    # No PAT yet -> state "none" badge and "none"/"never" placeholders localized.
    assert "無" in response.text
    assert "從未" in response.text
    _assert_no_raw_keys(response.text)


def test_upload_page_translated(tmp_path) -> None:
    client, _ = _authed_client(tmp_path)

    response = client.get("/upload", headers=ZH)

    assert "建議的描述資料" in response.text  # review heading
    assert "Proposed metadata" not in response.text
    _assert_no_raw_keys(response.text)


def test_detail_page_translated(tmp_path) -> None:
    client, app = _authed_client(tmp_path)
    _seed_template(app)

    response = client.get("/templates/ci-party", headers=ZH)

    assert response.status_code == 200
    assert "屬性" in response.text  # Attributes
    assert "Attributes" not in response.text
    assert "指紋" in response.text  # Fingerprint
    _assert_no_raw_keys(response.text)


def test_pat_expiry_banner_plural_and_interpolation(tmp_path) -> None:
    client, app = _authed_client(tmp_path)
    issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value, ttl_days=3)

    zh = client.get("/browse", headers=ZH)
    en = client.get("/browse", headers=EN)

    assert "天後過期" in zh.text  # plural + interpolation rendered
    assert "已以 friend 登入。" in zh.text  # common.signed_in_as interpolation
    assert "Your PAT expires in" in en.text


# --- static guards ---------------------------------------------------------


def _all_template_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in TEMPLATES_DIR.glob("*.html"))


def test_orphan_key_check() -> None:
    # Every non-js catalog key must be referenced by at least one template.
    text = _all_template_text()

    def _quoted(token: str) -> bool:
        # t() calls appear with either quote style in the templates.
        return f'"{token}"' in text or f"'{token}'" in text

    referenced: set[str] = set()
    for key in MESSAGES:
        if key.startswith("js."):
            continue
        if _quoted(key):
            referenced.add(key)
        # Plural keys are reached via plural(n, "base"); the base is the literal.
        if key.endswith((".one", ".other")) and _quoted(key.rsplit(".", 1)[0]):
            referenced.add(key)

    orphans = sorted(k for k in MESSAGES if not k.startswith("js.") and k not in referenced)
    assert orphans == []


# Distinctive pre-existing English phrases that must no longer appear as bare
# text in the templates (they now live in the catalog). Catches a literal
# silently left hardcoded, which the no-raw-key and completeness tests miss.
UNCONVERTED_LITERALS = {
    "base.html": ["Renew it in account settings", "Signed in as"],
    "landing.html": ["For MCP clients", "Sign in with GitHub", "Browse templates"],
    "restricted.html": ["Access restricted", "Back to browse"],
    "browse.html": ["Template library", "ready to render", "Search templates"],
    "detail.html": ["Attributes", "Fingerprint", "Perceptual hash", "Exact hash"],
    "account.html": ["MCP access token", "Current token", "Regenerating kills"],
    "upload.html": [
        "Upload a template",
        "Proposed metadata",
        "Stored image differs",
        "Looking at your image",
    ],
}


@pytest.mark.parametrize("filename", sorted(UNCONVERTED_LITERALS))
def test_no_unconverted_english_literals(filename: str) -> None:
    text = (TEMPLATES_DIR / filename).read_text(encoding="utf-8")
    # Strip Jinja and HTML comments: copy that legitimately mentions a literal in
    # a developer comment (not rendered) must not trip the guard.
    text = re.sub(r"{#.*?#}", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    for phrase in UNCONVERTED_LITERALS[filename]:
        assert phrase not in text, f"{filename}: un-converted literal {phrase!r}"
