"""MemeAuthProvider tests: registration hardening, the code->token->refresh->revoke
cycle, PKCE enforced by the SDK TokenHandler, per-request authorization, and the
backward-compatible PAT fallback. Async tests run under pytest asyncio_mode=auto."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request

from meme_mcp.auth.pat import SQLitePatStore, issue_pat
from meme_mcp.oauth.provider import MemeAuthProvider
from meme_mcp.oauth.store import REFRESH_GRACE_SECONDS, SQLiteOAuthStore

PEPPER = "oauth-token-pepper-32-chars-value-test"
ENC_KEY = "oauth-secret-enc-key-32-chars-value-test"
PAT_PEPPER = "pat-pepper-32-chars-value-for-tests-xx"
RESOURCE = "https://meme.igene.tw/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class FakeAllowlist:
    def __init__(self, logins: set[str]) -> None:
        self.logins = {login.lower() for login in logins}

    def is_allowlisted(self, value: str) -> bool:
        # is_authorized passes the bare GitHub login here.
        return value.strip().lower() in self.logins


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_provider(
    tmp_path: Path, allowed: set[str] | None = None, clock: FakeClock | None = None
) -> MemeAuthProvider:
    store = SQLiteOAuthStore(
        tmp_path / "oauth.db", token_pepper=PEPPER, secret_enc_key=ENC_KEY, clock=clock
    )
    pat_store = SQLitePatStore(tmp_path / "pats.db", clock=clock)
    return MemeAuthProvider(
        store=store,
        allowlist=FakeAllowlist(allowed if allowed is not None else {"alice"}),
        pat_store=pat_store,
        pat_pepper=PAT_PEPPER,
        resource_url=RESOURCE,
        pin_store=None,
    )


def public_client(client_id: str = "c-pub", redirect: str = REDIRECT) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=None,
        redirect_uris=[AnyUrl(redirect)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="meme:read meme:write",
    )


def confidential_client(client_id: str = "c-conf") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="the-secret",
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="meme:read",
    )


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _post_request(form: dict[str, str]) -> Request:
    body = urlencode(form).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/token",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    state = {"sent": False}

    async def receive() -> dict[str, object]:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


# -- registration hardening ---------------------------------------------------


async def test_register_valid_https_client(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    assert await provider.get_client("c-pub") is not None


@pytest.mark.parametrize(
    "redirect",
    [
        "http://evil.example.com/cb",  # http non-loopback
        "https://claude.ai/cb?x=1",  # query string
        "https://claude.ai/cb#frag",  # fragment
        "https://claude.ai/*",  # wildcard
    ],
)
async def test_register_rejects_bad_redirect(tmp_path: Path, redirect: str) -> None:
    provider = make_provider(tmp_path)
    with pytest.raises(RegistrationError):
        await provider.register_client(public_client(redirect=redirect))


async def test_register_allows_http_loopback(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client(redirect="http://127.0.0.1:33418/callback"))
    assert await provider.get_client("c-pub") is not None


async def test_confidential_secret_encrypted_and_returned_plaintext(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(confidential_client())
    loaded = await provider.get_client("c-conf")
    assert loaded is not None and loaded.client_secret == "the-secret"


# -- authorize parks state by nonce -------------------------------------------


async def test_authorize_returns_consent_redirect_with_nonce(tmp_path: Path) -> None:
    from mcp.server.auth.provider import AuthorizationParams

    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    params = AuthorizationParams(
        state="st",
        scopes=["meme:read"],
        code_challenge="chal",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    url = await provider.authorize(client, params)
    assert url.startswith("/oauth/consent/")
    nonce = url.rsplit("/", 1)[1]
    pending = provider.store.load_pending_request(nonce)
    assert pending is not None and pending.client_id == "c-pub"


# -- code -> token, single use, PKCE via the SDK handler ----------------------


def _seed_code(provider: MemeAuthProvider, verifier: str, scopes: list[str]) -> str:
    return provider.store.create_auth_code(
        client_id="c-pub",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        code_challenge=_challenge(verifier),
        scopes=scopes,
        principal="github:alice",
        resource=RESOURCE,
    )


async def test_token_handler_correct_verifier_mints(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    verifier = "verifier-0123456789-0123456789-0123456789"
    code = _seed_code(provider, verifier, ["meme:read", "meme:write"])
    handler = TokenHandler(provider, ClientAuthenticator(provider))
    resp = await handler.handle(
        _post_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "c-pub",
                "code_verifier": verifier,
                "redirect_uri": REDIRECT,
            }
        )
    )
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["access_token"] and body["refresh_token"]
    assert await provider.load_access_token(body["access_token"]) is not None


async def test_token_handler_wrong_verifier_rejected_before_provider(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    code = _seed_code(provider, "the-real-verifier-0123456789-0123456789", ["meme:read"])
    handler = TokenHandler(provider, ClientAuthenticator(provider))
    resp = await handler.handle(
        _post_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "c-pub",
                "code_verifier": "WRONG-verifier",
                "redirect_uri": REDIRECT,
            }
        )
    )
    assert resp.status_code == 400
    # The code was NOT consumed (PKCE failed before exchange), so it still loads.
    assert provider.store.load_auth_code(code) is not None


async def test_exchange_authorization_code_is_single_use(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    code = _seed_code(provider, "v-0123456789-0123456789-0123456789", ["meme:read"])
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    token = await provider.exchange_authorization_code(client, loaded)
    assert token.access_token
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)


async def test_issued_scopes_follow_the_grant(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    for scopes in (["meme:read"], ["meme:read", "meme:write"]):
        code = _seed_code(provider, "v-0123456789-0123456789-0123456789" + str(len(scopes)), scopes)
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is not None
        token = await provider.exchange_authorization_code(client, loaded)
        access = await provider.load_access_token(token.access_token)
        assert access is not None and list(access.scopes) == scopes


# -- refresh rotation, reuse, client binding ----------------------------------


async def _mint_pair(provider: MemeAuthProvider) -> tuple[str, str]:
    access, refresh = provider.store.issue_initial_tokens(
        client_id="c-pub",
        principal="github:alice",
        scopes=["meme:read", "meme:write"],
        resource=RESOURCE,
    )
    return access, refresh


async def test_refresh_rotation_and_reuse_detection(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    provider = make_provider(tmp_path, clock=clock)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    _access, refresh = await _mint_pair(provider)

    loaded = await provider.load_refresh_token(client, refresh)
    assert loaded is not None
    new_token = await provider.exchange_refresh_token(client, loaded, ["meme:read"])
    assert new_token.refresh_token and new_token.access_token

    # Replaying the original refresh beyond the grace window revokes the family.
    clock.advance(seconds=REFRESH_GRACE_SECONDS + 5)
    assert await provider.load_refresh_token(client, refresh) is None
    assert await provider.load_refresh_token(client, new_token.refresh_token) is None


async def test_refresh_scope_narrowing(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    _access, refresh = await _mint_pair(provider)
    loaded = await provider.load_refresh_token(client, refresh)
    assert loaded is not None
    token = await provider.exchange_refresh_token(client, loaded, ["meme:read"])
    access = await provider.load_access_token(token.access_token)
    assert access is not None and list(access.scopes) == ["meme:read"]


async def test_refresh_wrong_client_rejected_by_handler(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    await provider.register_client(public_client(client_id="c-other"))
    _access, refresh = await _mint_pair(provider)  # issued to c-pub
    handler = TokenHandler(provider, ClientAuthenticator(provider))
    resp = await handler.handle(
        _post_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": "c-other",  # different client
            }
        )
    )
    assert resp.status_code == 400


# -- revocation ---------------------------------------------------------------


async def test_revoke_access_token_kills_family(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    await provider.register_client(public_client())
    client = await provider.get_client("c-pub")
    assert client is not None
    access, refresh = await _mint_pair(provider)
    token = await provider.load_access_token(access)
    assert token is not None
    await provider.revoke_token(token)
    assert await provider.load_access_token(access) is None
    assert await provider.load_refresh_token(client, refresh) is None


# -- bearer verification: authorization, principal vs client_id, PAT fallback -


async def test_oauth_token_authorizes_then_deauthorizes(tmp_path: Path) -> None:
    allow = FakeAllowlist({"alice"})
    provider = make_provider(tmp_path)
    provider.allowlist = allow
    access, _refresh = await _mint_pair(provider)
    assert await provider.load_access_token(access) is not None
    allow.logins.clear()  # friend removed from allowlist
    assert await provider.load_access_token(access) is None  # next call denied


async def test_access_token_carries_principal_distinct_from_client_id(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    access, _ = await _mint_pair(provider)
    token = await provider.load_access_token(access)
    assert token is not None
    assert token.client_id == "c-pub"  # /revoke matches on this
    assert token.principal == "github:alice"  # actions attribute to the friend


async def test_pat_fallback_authenticates(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    pat = issue_pat(provider.pat_store, "alice", PAT_PEPPER)
    token = await provider.load_access_token(pat)
    assert token is not None
    assert token.principal == "github:alice"
    assert token.client_id == "github:alice"  # PAT: client_id == principal
    assert "meme:write" in token.scopes  # readwrite default capability


async def test_pat_fallback_denied_when_not_allowlisted(tmp_path: Path) -> None:
    provider = make_provider(tmp_path, allowed=set())
    pat = issue_pat(provider.pat_store, "alice", PAT_PEPPER)
    assert await provider.load_access_token(pat) is None


async def test_unknown_token_returns_none(tmp_path: Path) -> None:
    provider = make_provider(tmp_path)
    assert await provider.load_access_token("not-a-real-token") is None
