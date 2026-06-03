from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.responses import RedirectResponse

from meme_mcp.auth.google_oauth import ResolvedIdentity
from tests.test_upload_flow import good_settings


def _google_settings(tmp_path):
    base = good_settings(tmp_path)
    return base.model_copy(
        update={
            "google_oauth_enabled": True,
            "google_client_id": "gid",
            "google_client_secret": SecretStr("gsecret"),
            "google_redirect_uri": "http://localhost:8000/auth/google/callback",
        }
    )


class FakeGoogleOAuth:
    """Test seam mirroring FakeGitHubOAuth: no Authlib, no network.

    resolve_identity returns a ResolvedIdentity carrying the raw email_verified
    value so the callback's strict `is True` gate (R15) can be exercised.
    """

    def __init__(self, *, sub: str, email: str, email_verified: object = True) -> None:
        self.sub = sub
        self.email = email
        self.email_verified = email_verified

    async def authorize_redirect(self, request, redirect_uri):  # noqa: ANN001
        return RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth?state=fake", status_code=302
        )

    async def resolve_identity(self, request) -> ResolvedIdentity:  # noqa: ANN001
        return ResolvedIdentity("google", self.sub, self.email, self.email_verified)


def _app(tmp_path, fake: FakeGoogleOAuth | None = None, *, allow: str | None = None):
    from meme_mcp.app import create_app

    app = create_app(_google_settings(tmp_path))
    if fake is not None:
        app.state.google_oauth = fake
    if allow is not None:
        app.state.allowlist.add(allow)
    return app


def _complete_google_login(client: TestClient, *, next_q: str = "") -> object:
    query = f"?next={next_q}" if next_q else ""
    client.get(f"/auth/google/login{query}", follow_redirects=False)
    return client.get("/auth/google/callback", follow_redirects=False)


# --- chooser ------------------------------------------------------------------


def test_auth_login_renders_chooser_when_google_enabled(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    page = client.get("/auth/login?next=/upload", follow_redirects=False)
    assert page.status_code == 200
    assert "/auth/google/login" in page.text
    assert "/auth/login?provider=github" in page.text


def test_provider_github_bypasses_chooser(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    started = client.get("/auth/login?provider=github", follow_redirects=False)
    assert started.status_code == 307
    assert "github.com/login/oauth/authorize" in started.headers["location"]


def test_anonymous_browse_redirects_to_chooser(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    bounce = client.get("/browse", follow_redirects=False)
    assert bounce.status_code == 303
    assert bounce.headers["location"] == "/auth/login?next=/browse"


# --- first gate: email_verified (R15); any verified Google mailbox -----------


@pytest.mark.parametrize("verified", [False, "true", "false", None, "", 1])
def test_non_true_email_verified_rejected(tmp_path, verified) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="alice@gmail.com", email_verified=verified),
        allow="google:alice@gmail.com",
    )
    client = TestClient(app)
    response = _complete_google_login(client)
    assert response.status_code == 403
    # No session: a subsequent /browse bounces back to login.
    assert client.get("/browse", follow_redirects=False).status_code == 303


def test_boolean_true_verified_gmail_proceeds(tmp_path) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="alice@gmail.com", email_verified=True),
        allow="google:alice@gmail.com",
    )
    client = TestClient(app)
    response = _complete_google_login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert client.get("/browse").status_code == 200


def test_verified_non_gmail_allowlisted_address_proceeds(tmp_path) -> None:
    # A verified non-Gmail Google mailbox (e.g. an @icloud.com account) is
    # accepted when allowlisted: authorization keys on the full email + sub-pin,
    # so the domain is not restricted to Gmail.
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="friend@icloud.com", email_verified=True),
        allow="google:friend@icloud.com",
    )
    client = TestClient(app)
    response = _complete_google_login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert client.get("/browse").status_code == 200
    assert app.state.pin_store.email_for_sub("s1") == "friend@icloud.com"


@pytest.mark.parametrize("bad_email", ["@icloud.com", "friend@", "a@@b.com", "noatsign", ""])
def test_malformed_verified_email_rejected(tmp_path, bad_email) -> None:
    # The structural gate (non-empty local + single-@ + non-empty domain) rejects
    # malformed claims before any allowlist lookup, even if email_verified is true.
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email=bad_email, email_verified=True),
        allow=f"google:{bad_email}",
    )
    client = TestClient(app)
    assert _complete_google_login(client).status_code == 403


def test_verified_non_gmail_not_allowlisted_rejected(tmp_path) -> None:
    # Relaxing the domain gate must not relax the allowlist requirement.
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="stranger@icloud.com", email_verified=True),
    )
    client = TestClient(app)
    assert _complete_google_login(client).status_code == 403


def test_verified_gmail_not_allowlisted_rejected(tmp_path) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="stranger@gmail.com", email_verified=True),
    )
    client = TestClient(app)
    assert _complete_google_login(client).status_code == 403


def test_open_redirect_next_rejected(tmp_path) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="s1", email="alice@gmail.com", email_verified=True),
        allow="google:alice@gmail.com",
    )
    client = TestClient(app)
    response = _complete_google_login(client, next_q="https://evil.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


def test_google_routes_unavailable_when_disabled(tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))  # google disabled
    client = TestClient(app)
    # The unavailable sentinel raises the standard unauthorized error.
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 401


# --- pin-first resolution (U6 scenarios via the callback) ---------------------


def test_first_verified_invited_sign_in_creates_pin(tmp_path) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="sub-A", email="alice@gmail.com", email_verified=True),
        allow="google:alice@gmail.com",
    )
    client = TestClient(app)
    assert _complete_google_login(client).status_code == 303
    assert app.state.pin_store.email_for_sub("sub-A") == "alice@gmail.com"


def test_ae2_github_and_google_identities_are_independent(tmp_path) -> None:
    from meme_mcp.auth.pat import issue_pat

    # GitHub alice is allowlisted and holds a PAT; Google alice@gmail.com is NOT
    # invited -> the Google sign-in is rejected and the GitHub PAT is untouched.
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="sub-A", email="alice@gmail.com", email_verified=True),
        allow="alice",  # bare => GitHub
    )
    token = issue_pat(app.state.pat_store, "alice", app.state.pat_hash_pepper_value)
    client = TestClient(app)
    assert _complete_google_login(client).status_code == 403
    from meme_mcp.auth.pat import verify_pat

    assert verify_pat(app.state.pat_store, token, app.state.pat_hash_pepper_value) == (
        "github:alice",
        "readwrite",
    )


def test_first_sign_in_wins_blocks_second_sub(tmp_path) -> None:
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="sub-A", email="alice@gmail.com", email_verified=True),
        allow="google:alice@gmail.com",
    )
    client_a = TestClient(app)
    assert _complete_google_login(client_a).status_code == 303
    # A different sub presenting the same invited email is rejected.
    app.state.google_oauth = FakeGoogleOAuth(
        sub="sub-B", email="alice@gmail.com", email_verified=True
    )
    client_b = TestClient(app)
    assert _complete_google_login(client_b).status_code == 403
    assert app.state.pin_store.sub_for_email("alice@gmail.com") == "sub-A"


def test_drift_returning_sub_after_gmail_rename_still_authorized(tmp_path) -> None:
    # sub-A first signs in as alice@gmail.com (pinned). Later the claim email is a
    # different (non-allowlisted) address, but the pinned email is still
    # allowlisted -> the returning sub is authorized, not 403'd.
    app = _app(
        tmp_path,
        FakeGoogleOAuth(sub="sub-A", email="alice@gmail.com", email_verified=True),
        allow="google:alice@gmail.com",
    )
    first = TestClient(app)
    assert _complete_google_login(first).status_code == 303
    app.state.google_oauth = FakeGoogleOAuth(
        sub="sub-A", email="renamed@gmail.com", email_verified=True
    )
    again = TestClient(app)
    response = _complete_google_login(again)
    assert response.status_code == 303
    assert again.get("/browse").status_code == 200
