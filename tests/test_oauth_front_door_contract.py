"""Contract test for the fourth authorization front door (U7).

The browser session, the web PAT, the MCP transport PAT, and the MCP OAuth token
path must all delegate to the single ``is_authorized`` leaf so authorization
cannot silently diverge. This locks the OAuth path (``MemeAuthProvider``) with a
structural guard plus a behavioral spy, mirroring the grep-guard discipline in
``tests/test_authorization.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import meme_mcp.oauth.provider as provider_module
from meme_mcp.auth.pat import SQLitePatStore
from meme_mcp.oauth.provider import MemeAuthProvider
from meme_mcp.oauth.store import SQLiteOAuthStore

PEPPER = "oauth-token-pepper-32-chars-value-test"
ENC_KEY = "oauth-secret-enc-key-32-chars-value-test"


class _AllowAll:
    def is_allowlisted(self, value: str) -> bool:  # noqa: ARG002 - contract spy needs the signature
        return True


def test_oauth_load_access_token_source_references_is_authorized() -> None:
    # Structural guard: the OAuth bearer path must call is_authorized by name, so
    # a refactor cannot inline a bare allowlist membership check.
    source = inspect.getsource(MemeAuthProvider.load_access_token)
    assert "is_authorized(" in source


def test_load_access_token_path_in_source() -> None:
    # The grep-guard equivalent at file scope (catches a helper extraction that
    # moves the call out of the method body but keeps it in the module).
    source = Path("src/meme_mcp/oauth/provider.py").read_text(encoding="utf-8")
    assert "is_authorized(" in source


async def test_oauth_path_actually_invokes_is_authorized(tmp_path, monkeypatch) -> None:
    # Behavioral contract: loading an OAuth access token routes the principal
    # through is_authorized (not a bare allowlist call), per request.
    store = SQLiteOAuthStore(tmp_path / "oauth.db", token_pepper=PEPPER, secret_enc_key=ENC_KEY)
    provider = MemeAuthProvider(
        store=store,
        allowlist=_AllowAll(),
        pat_store=SQLitePatStore(tmp_path / "pats.db"),
        pat_pepper="pat-pepper-32-chars-value-for-tests-xx",
        resource_url="https://meme.igene.tw/mcp",
    )
    access, _refresh = store.issue_initial_tokens(
        client_id="c1", principal="github:friend", scopes=["meme:read"], resource=None
    )
    seen: list[str] = []

    def spy(principal: str, **kwargs: object) -> bool:
        seen.append(principal)
        return True

    monkeypatch.setattr(provider_module, "is_authorized", spy)
    assert await provider.load_access_token(access) is not None
    assert seen == ["github:friend"]
