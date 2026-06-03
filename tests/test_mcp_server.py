import json

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP

from meme_mcp.auth.pat import SQLitePatStore, issue_pat
from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.mcp.server import (
    EXPECTED_TOOLS,
    PatTokenVerifier,
    _authenticated_actor,
    create_mcp_server,
    tool_schemas,
)


class _StubAllowlist:
    """Minimal allowlist for verifier tests: matches a bare GitHub login.

    is_authorized passes the bare login (the subject of a github: principal), so
    membership is keyed on the bare value.
    """

    def __init__(self, *logins: str) -> None:
        self._logins = set(logins)

    def is_allowlisted(self, value: str) -> bool:
        return value in self._logins


def test_mcp_exposes_three_tools_under_schema_budget() -> None:
    schemas = tool_schemas()
    assert set(schemas) == EXPECTED_TOOLS == {"find", "generate", "record_outcome"}
    budget = sum(len(json.dumps(schema)) for schema in schemas.values())
    assert budget < 4096


async def test_pat_token_verifier_validates_sqlite_pat(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    token = issue_pat(store, "alice", "pepper")
    verifier = PatTokenVerifier(store, _StubAllowlist("alice"), "pepper")
    access_token = await verifier.verify_token(token)
    assert access_token is not None
    assert access_token.client_id == "github:alice"
    assert await verifier.verify_token("wrong") is None


async def test_pat_token_verifier_scopes_derive_from_capability(tmp_path) -> None:
    """Read-scope PATs receive only meme:read; readwrite PATs receive meme:read +
    meme:write. The MCP tool wrappers (and the web write routes) gate on the resulting
    scope set so a read-scope PAT cannot drive any write path.
    """
    store = SQLitePatStore(tmp_path / "auth.db")
    read_token = issue_pat(store, "alice", "pepper", capability="read")
    readwrite_token = issue_pat(store, "bob", "pepper", capability="readwrite")
    verifier = PatTokenVerifier(store, _StubAllowlist("alice", "bob"), "pepper")
    read_access = await verifier.verify_token(read_token)
    readwrite_access = await verifier.verify_token(readwrite_token)
    assert read_access is not None and readwrite_access is not None
    assert read_access.scopes == ["meme:read"]
    assert readwrite_access.scopes == ["meme:read", "meme:write"]


async def test_pat_token_verifier_authorizes_google_pat_over_transport(tmp_path) -> None:
    """AE4 + R12: a Google friend's PAT (subject google:<sub>) authenticates over
    the MCP transport identically to a GitHub friend's, resolving authorization
    through the pin store; deleting the pin denies the very next request.
    """
    from meme_mcp.auth.allowlist import FileAllowlist
    from meme_mcp.auth.google_pins import SQLiteGooglePinStore

    store = SQLitePatStore(tmp_path / "auth.db")
    pins = SQLiteGooglePinStore(tmp_path / "pins.db")
    pins.create_pin("sub-A", "alice@gmail.com")
    allow = FileAllowlist(tmp_path / "allowlist.txt")
    allow.add("google:alice@gmail.com")
    token = issue_pat(store, "google:sub-A", "pepper")

    verifier = PatTokenVerifier(store, allow, "pepper", pins)
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.client_id == "google:sub-A"

    # R12: terminal pin eviction denies the next transport request (no caching).
    pins.delete_by_sub("sub-A")
    assert await verifier.verify_token(token) is None


def test_create_mcp_server_registers_official_fastmcp_tools(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    server = create_mcp_server(store, {"alice"}, "pepper", "http://localhost:8000")
    assert isinstance(server, FastMCP)
    assert server.streamable_http_app() is not None


def test_create_mcp_server_uses_configured_public_auth_urls(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    server = create_mcp_server(store, {"alice"}, "pepper", "https://meme.igene.tw")
    assert str(server.settings.auth.issuer_url) == "https://meme.igene.tw/"
    assert str(server.settings.auth.resource_server_url) == "https://meme.igene.tw/mcp"


def test_authenticated_actor_rejects_unauthenticated_calls() -> None:
    with pytest.raises(MemeMCPError) as info:
        _authenticated_actor()
    assert info.value.error_code is ErrorCode.UNAUTHORIZED


def test_authenticated_actor_returns_verified_token_login() -> None:
    access = AccessToken(token="t", client_id="alice", scopes=["meme:read"])
    token_handle = auth_context_var.set(AuthenticatedUser(access))
    try:
        assert _authenticated_actor() == "alice"
    finally:
        auth_context_var.reset(token_handle)


def test_require_write_scope_rejects_read_only_token() -> None:
    from meme_mcp.mcp.server import _require_write_scope

    access = AccessToken(token="t", client_id="alice", scopes=["meme:read"])
    token_handle = auth_context_var.set(AuthenticatedUser(access))
    try:
        with pytest.raises(MemeMCPError) as info:
            _require_write_scope()
        assert info.value.error_code is ErrorCode.UNAUTHORIZED
    finally:
        auth_context_var.reset(token_handle)


def test_require_write_scope_accepts_readwrite_token() -> None:
    from meme_mcp.mcp.server import _require_write_scope

    access = AccessToken(
        token="t", client_id="alice", scopes=["meme:read", "meme:write"]
    )
    token_handle = auth_context_var.set(AuthenticatedUser(access))
    try:
        _require_write_scope()  # no exception
    finally:
        auth_context_var.reset(token_handle)


def test_require_write_scope_rejects_missing_context() -> None:
    from meme_mcp.mcp.server import _require_write_scope

    with pytest.raises(MemeMCPError) as info:
        _require_write_scope()
    assert info.value.error_code is ErrorCode.UNAUTHORIZED
