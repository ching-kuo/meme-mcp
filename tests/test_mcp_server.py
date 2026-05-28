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


def test_mcp_exposes_exactly_find_and_generate_with_small_schemas() -> None:
    schemas = tool_schemas()
    assert set(schemas) == EXPECTED_TOOLS == {"find", "generate"}
    budget = sum(len(json.dumps(schema)) for schema in schemas.values())
    assert budget < 4096


async def test_pat_token_verifier_validates_sqlite_pat(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    token = issue_pat(store, "alice", "pepper")
    verifier = PatTokenVerifier(store, {"alice"}, "pepper")
    access_token = await verifier.verify_token(token)
    assert access_token is not None
    assert access_token.client_id == "alice"
    assert await verifier.verify_token("wrong") is None


async def test_pat_token_verifier_scopes_are_static_until_u3(tmp_path) -> None:
    """Anchor test: AccessToken.scopes is currently hardcoded regardless of capability.
    When the capability propagation lands, this test must be updated to assert the
    scope set is derived from the verified PAT's capability. The failure of this test
    is the signal that the enforcement gap has closed.
    """
    store = SQLitePatStore(tmp_path / "auth.db")
    read_token = issue_pat(store, "alice", "pepper", capability="read")
    readwrite_token = issue_pat(store, "bob", "pepper", capability="readwrite")
    verifier = PatTokenVerifier(store, {"alice", "bob"}, "pepper")
    read_access = await verifier.verify_token(read_token)
    readwrite_access = await verifier.verify_token(readwrite_token)
    assert read_access is not None and readwrite_access is not None
    assert read_access.scopes == ["meme:read", "meme:write"]
    assert readwrite_access.scopes == ["meme:read", "meme:write"]


def test_create_mcp_server_registers_official_fastmcp_tools(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    server = create_mcp_server(store, {"alice"}, "pepper")
    assert isinstance(server, FastMCP)
    assert server.streamable_http_app() is not None


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
