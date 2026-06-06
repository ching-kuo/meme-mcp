"""Authorize-bridge + consent-screen tests (U4): login bounce, per-client consent,
allowlist-at-issuance, confused-deputy mitigation, CSRF, and single-use nonces."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import itsdangerous
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import meme_mcp.app as app_module
from meme_mcp.oauth.consent import register_consent_routes
from meme_mcp.oauth.provider import MemeAuthProvider
from meme_mcp.oauth.store import SQLiteOAuthStore
from meme_mcp.web.csrf import safe_next
from tests.test_oauth_provider import (
    ENC_KEY,
    PAT_PEPPER,
    PEPPER,
    REDIRECT,
    RESOURCE,
    FakeAllowlist,
)

SESSION_SECRET = "test-session-secret-32-chars-xxxxxxxx"
CSRF = "known-csrf-token-value"


def _register_error_handler(app: FastAPI) -> None:
    # Mirror create_app's MemeMCPError -> JSON handler so require_csrf_form's
    # FORBIDDEN surfaces as a 403 (as it does in the real parent app).
    from fastapi.responses import JSONResponse

    from meme_mcp.envelope import make_error
    from meme_mcp.errors import MemeMCPError, status_for_error

    @app.exception_handler(MemeMCPError)
    async def _handler(_request: object, exc: MemeMCPError) -> JSONResponse:
        return JSONResponse(
            make_error(exc.error_code, exc.errors), status_code=status_for_error(exc.error_code)
        )


def _session_cookie(**data: str) -> str:
    signer = itsdangerous.TimestampSigner(SESSION_SECRET)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


def build(
    tmp_path: Path, allowed: set[str] | None = None
) -> tuple[FastAPI, MemeAuthProvider, FakeAllowlist]:
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False
    )
    _register_error_handler(app)
    web_dir = Path(app_module.__file__).parent / "web"
    templates = Jinja2Templates(
        directory=web_dir / "templates", context_processors=[app_module._i18n_context]
    )
    store = SQLiteOAuthStore(tmp_path / "oauth.db", token_pepper=PEPPER, secret_enc_key=ENC_KEY)
    from meme_mcp.auth.pat import SQLitePatStore

    allow = FakeAllowlist(allowed if allowed is not None else {"alice"})
    provider = MemeAuthProvider(
        store=store,
        allowlist=allow,
        pat_store=SQLitePatStore(tmp_path / "pats.db"),
        pat_pepper=PAT_PEPPER,
        resource_url=RESOURCE,
    )
    app.state.web_allowlist = allow
    app.state.allowlist = allow
    app.state.pin_store = None
    app.state.web_templates = templates
    app.state.settings = SimpleNamespace(operator_github_login="operator")
    register_consent_routes(app, provider=provider, templates=templates)
    return app, provider, allow


def _pending(provider: MemeAuthProvider, scopes: list[str] | None = None) -> str:
    return provider.store.create_pending_request(
        client_id="c-pub",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        code_challenge="chal",
        scopes=scopes if scopes is not None else ["meme:read"],
        resource=RESOURCE,
        state="xyz",
    )


def _client(app: FastAPI, **session: str) -> TestClient:
    client = TestClient(app)
    if session:
        client.cookies.set("session", _session_cookie(**session))
    return client


# -- safe_next admits the consent return, rejects external origins ------------


def test_safe_next_admits_consent_rejects_external() -> None:
    assert safe_next("/oauth/consent/abc123_-") == "/oauth/consent/abc123_-"
    assert safe_next("https://evil.example.com/oauth/consent/x") == "/browse"
    assert safe_next("/oauth/consent/a/b") == "/browse"  # embedded slash rejected


# -- login bounce -------------------------------------------------------------


def test_anonymous_consent_redirects_to_login_preserving_nonce(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path)
    rid = _pending(provider)
    resp = _client(app).get(f"/oauth/consent/{rid}", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/auth/login" in location
    assert f"/oauth/consent/{rid}" in unquote(location)


# -- consent screen -----------------------------------------------------------


def test_first_time_client_shows_consent_screen(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path)
    rid = _pending(provider, scopes=["meme:read", "meme:write"])
    resp = _client(app, github_login="alice").get(f"/oauth/consent/{rid}", follow_redirects=False)
    assert resp.status_code == 200
    assert "c-pub" in resp.text
    assert "meme:read" in resp.text and "meme:write" in resp.text
    assert REDIRECT in resp.text
    assert resp.headers.get("x-frame-options") == "DENY"


# -- approval issues a code ---------------------------------------------------


def test_approve_records_approval_and_redirects_with_code(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path)
    rid = _pending(provider)
    resp = _client(app, github_login="alice", csrf_token=CSRF).post(
        f"/oauth/consent/{rid}",
        data={"decision": "approve", "csrf_token": CSRF},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(REDIRECT)
    assert "code=" in location and "state=xyz" in location
    assert provider.store.has_approval("github:alice", "c-pub", ["meme:read"]) is True


def test_non_allowlisted_denied_at_issuance_even_after_approving(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path, allowed=set())
    rid = _pending(provider)
    resp = _client(app, github_login="alice", csrf_token=CSRF).post(
        f"/oauth/consent/{rid}",
        data={"decision": "approve", "csrf_token": CSRF},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "code=" not in resp.headers.get("location", "")


def test_approved_client_skips_screen_but_rechecks_allowlist(tmp_path: Path) -> None:
    app, provider, allow = build(tmp_path)
    provider.store.record_approval("github:alice", "c-pub", ["meme:read"])
    rid = _pending(provider)  # requests ["meme:read"] -> covered by the approval
    # Pre-approved: GET auto-issues (no screen) and redirects with a code.
    resp = _client(app, github_login="alice").get(f"/oauth/consent/{rid}", follow_redirects=False)
    assert resp.status_code == 303 and "code=" in resp.headers["location"]
    # De-allowlist, new request: pre-approval does not bypass the live check.
    allow.logins.clear()
    rid2 = _pending(provider)
    resp2 = _client(app, github_login="alice").get(f"/oauth/consent/{rid2}", follow_redirects=False)
    assert resp2.status_code == 403


# -- CSRF & confused-deputy ---------------------------------------------------


def test_consent_post_without_csrf_rejected(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path)
    rid = _pending(provider)
    resp = _client(app, github_login="alice", csrf_token=CSRF).post(
        f"/oauth/consent/{rid}",
        data={"decision": "approve", "csrf_token": "WRONG"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    # CSRF failed before any consent logic: the pending request is untouched.
    assert provider.store.load_pending_request(rid) is not None


def test_prior_read_approval_does_not_auto_grant_write(tmp_path: Path) -> None:
    # Scope escalation guard: a client previously approved for meme:read must NOT
    # be auto-issued a code for a later meme:write request -- the consent screen
    # is re-shown instead.
    app, provider, _ = build(tmp_path)
    provider.store.record_approval("github:alice", "c-pub", ["meme:read"])
    rid = _pending(provider, scopes=["meme:read", "meme:write"])
    resp = _client(app, github_login="alice").get(f"/oauth/consent/{rid}", follow_redirects=False)
    assert resp.status_code == 200  # consent screen, not a 303 auto-issue
    assert "meme:write" in resp.text
    assert "code=" not in resp.headers.get("location", "")


def test_confused_deputy_unapproved_client_not_auto_issued(tmp_path: Path) -> None:
    # A session exists but the client was never approved: GET shows the consent
    # screen rather than silently issuing a code on the existing session.
    app, provider, _ = build(tmp_path)
    rid = _pending(provider)
    resp = _client(app, github_login="alice").get(f"/oauth/consent/{rid}", follow_redirects=False)
    assert resp.status_code == 200  # screen, not a 303 to the client
    assert "code=" not in resp.headers.get("location", "")


# -- single-use nonce ---------------------------------------------------------


def test_unknown_nonce_rejected(tmp_path: Path) -> None:
    app, _provider, _ = build(tmp_path)
    resp = _client(app, github_login="alice").get("/oauth/consent/nope", follow_redirects=False)
    assert resp.status_code == 400


def test_replayed_consent_after_code_issued_fails(tmp_path: Path) -> None:
    app, provider, _ = build(tmp_path)
    rid = _pending(provider)
    client = _client(app, github_login="alice", csrf_token=CSRF)
    first = client.post(
        f"/oauth/consent/{rid}",
        data={"decision": "approve", "csrf_token": CSRF},
        follow_redirects=False,
    )
    assert first.status_code == 303
    replay = client.post(
        f"/oauth/consent/{rid}",
        data={"decision": "approve", "csrf_token": CSRF},
        follow_redirects=False,
    )
    assert replay.status_code == 400  # pending was consumed
