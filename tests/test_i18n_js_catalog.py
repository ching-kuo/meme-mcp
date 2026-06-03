from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from meme_mcp import app as app_module
from meme_mcp.app import create_app
from meme_mcp.web.i18n import core
from meme_mcp.web.i18n.core import SUPPORTED, js_catalog
from tests.test_oauth_session import FakeGitHubOAuth, _extract_state
from tests.test_upload_flow import good_settings

STATIC_DIR = Path(__file__).resolve().parents[1] / "src/meme_mcp/web/static"
BLOB_RE = re.compile(
    r'<script type="application/json" id="i18n-catalog">(.*?)</script>', re.DOTALL
)


# ---------------------------------------------------------------------------
# js_catalog shape
# ---------------------------------------------------------------------------


def test_js_catalog_only_js_keys_and_complete() -> None:
    for locale in SUPPORTED:
        catalog = js_catalog(locale)
        assert catalog, "js catalog is empty"
        assert all(key.startswith("js.") for key in catalog)
        assert all(value for value in catalog.values()), "empty js value"


def test_js_catalog_excludes_server_only_keys() -> None:
    catalog = js_catalog("en")
    assert "nav.browse" not in catalog
    assert not any(key.startswith("nav.") or key.startswith("upload.field.") for key in catalog)


# ---------------------------------------------------------------------------
# Script-context safety (KTD6)
# ---------------------------------------------------------------------------


def test_js_catalog_json_escapes_script_breakout(monkeypatch) -> None:
    monkeypatch.setattr(core, "MESSAGES", {"js.x": {"en": "</script><!--", "zh-TW": "x"}})

    rendered = app_module._js_catalog_json("en")

    assert "</script>" not in rendered
    assert "<!--" not in rendered
    assert "\\u003c" in rendered
    # Still valid JSON that restores the original value.
    assert json.loads(rendered)["js.x"] == "</script><!--"


def test_blob_cannot_break_out_in_rendered_page(tmp_path, monkeypatch) -> None:
    # Render-level guard: a mixed-case </ScRiPt> payload (HTML tag matching is
    # case-insensitive) must not produce a second closing tag in the actual page.
    monkeypatch.setattr(
        core, "MESSAGES", {"js.x": {"en": "</ScRiPt><sCript>alert(1)", "zh-TW": "x"}}
    )
    client = TestClient(create_app(good_settings(tmp_path)))

    text = client.get("/", headers={"Accept-Language": "en"}).text

    blobs = BLOB_RE.findall(text)
    assert len(blobs) == 1, "catalog value broke out into a second script element"
    assert "</ScRiPt>" not in blobs[0]
    assert "\\u003c" in blobs[0]
    assert json.loads(blobs[0])["js.x"] == "</ScRiPt><sCript>alert(1)"


# ---------------------------------------------------------------------------
# Rendered blob + script ordering
# ---------------------------------------------------------------------------


def _authed_client(tmp_path) -> TestClient:
    app = create_app(good_settings(tmp_path))
    app.state.github_oauth = FakeGitHubOAuth("friend")
    (tmp_path / "allowlist.txt").write_text("friend\n", encoding="utf-8")
    client = TestClient(app)
    login = client.get("/auth/login", follow_redirects=False)
    client.get(
        "/auth/callback?code=ok-code&state=" + _extract_state(login),
        follow_redirects=False,
    )
    return client


def test_blob_carries_active_locale_strings(tmp_path) -> None:
    client = _authed_client(tmp_path)

    zh = client.get("/upload", headers={"Accept-Language": "zh-TW"})
    en = client.get("/upload", headers={"Accept-Language": "en"})

    zh_blob = json.loads(BLOB_RE.search(zh.text).group(1))
    en_blob = json.loads(BLOB_RE.search(en.text).group(1))

    assert zh_blob["js.copy.done"] == "已複製"
    assert en_blob["js.copy.done"] == "Copied"
    # The blob round-trips as valid JSON and excludes server-only keys.
    assert "nav.browse" not in zh_blob


def test_blob_and_bootstrap_precede_page_scripts(tmp_path) -> None:
    client = _authed_client(tmp_path)

    upload = client.get("/upload", headers={"Accept-Language": "en"})
    account = client.get("/account", headers={"Accept-Language": "en"})

    # The t() bootstrap lives in external /static/base.js (deferred), not inline,
    # so the gateway's strict CSP does not block it. Deferred scripts run in
    # document order, so base.js's tag must precede the page script's tag for
    # window.t to be defined when account.js/upload.js run.
    for page, script in ((upload, "/static/upload.js"), (account, "/static/account.js")):
        blob_at = page.text.index('id="i18n-catalog"')
        boot_at = page.text.index("/static/base.js")
        script_at = page.text.index(script)
        assert blob_at < script_at, "catalog blob must precede the page script"
        assert boot_at < script_at, "base.js bootstrap must precede the page script"


# Matches a <script> ... </script> with neither a src= attribute nor a
# type="application/json" data marker -- i.e. an inline executable script, which
# the gateway CSP ("default-src 'self'", no 'unsafe-inline') blocks at runtime.
_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", re.IGNORECASE
)


def test_rendered_pages_have_no_inline_scripts(tmp_path) -> None:
    # CSP guard: every executable script must be an external /static file. An
    # inline <script> renders fine in tests but is silently blocked in production
    # behind the gateway CSP (this is how the logout button and the i18n
    # bootstrap first broke). The JSON catalog data block is allowed (not executed).
    client = _authed_client(tmp_path)

    for path in ("/", "/browse", "/upload", "/account"):
        text = client.get(path, headers={"Accept-Language": "en"}).text
        offenders = _INLINE_SCRIPT_RE.findall(text)
        assert not offenders, f"{path} has a CSP-blocked inline <script>: {offenders}"


# ---------------------------------------------------------------------------
# Literal-removal guard + referenced-keys
# ---------------------------------------------------------------------------

JS_LITERALS = {
    "account.js": [
        "Regenerate this token",
        "Revoke the active token",
        '"Copied"',
        '"Generate"',
    ],
    "upload.js": [
        "This image was rejected",
        "already exists as template",
        "uploaded too many images",
        "Looking up this meme online",
        "Could not reach the server",
        "Session expired",
        "No slots proposed",
        "is now searchable",
    ],
}


def test_no_unconverted_js_literals() -> None:
    for filename, phrases in JS_LITERALS.items():
        text = (STATIC_DIR / filename).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase not in text, f"{filename}: un-converted literal {phrase!r}"


def test_referenced_js_keys_exist_in_catalog() -> None:
    # Every complete t("js....") literal referenced by the JS must be in the
    # shipped catalog (a missing key would silently render the bare key id).
    keys = set(js_catalog("en"))
    ref_re = re.compile(r't\(\s*"(js\.[a-z0-9_.]*[a-z0-9_])"', re.IGNORECASE)
    for filename in ("account.js", "upload.js"):
        text = (STATIC_DIR / filename).read_text(encoding="utf-8")
        for match in ref_re.finditer(text):
            key = match.group(1)
            # Skip concatenation prefixes like "js.token.state." (trailing dot).
            if key.endswith("."):
                continue
            assert key in keys, f"{filename} references missing js key: {key}"


def test_dynamic_status_prefixes_are_populated() -> None:
    # account.js builds keys as "js.token.state." + value; assert the enum keys exist.
    keys = set(js_catalog("en"))
    for state in ("none", "active", "expired", "revoked"):
        assert f"js.token.state.{state}" in keys
    for scope in ("read", "readwrite"):
        assert f"js.token.scope.{scope}" in keys
    assert {"js.token.none", "js.token.never"} <= keys
