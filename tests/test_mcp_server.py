import json

from mcp.server.fastmcp import FastMCP

from meme_mcp.auth.pat import SQLitePatStore, issue_pat
from meme_mcp.mcp.server import EXPECTED_TOOLS, PatTokenVerifier, create_mcp_server, tool_schemas


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


def test_create_mcp_server_registers_official_fastmcp_tools(tmp_path) -> None:
    store = SQLitePatStore(tmp_path / "auth.db")
    server = create_mcp_server(store, {"alice"}, "pepper")
    assert isinstance(server, FastMCP)
    assert server.streamable_http_app() is not None
