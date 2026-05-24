from __future__ import annotations

from collections.abc import Container
from typing import Any, Protocol

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context

from meme_mcp.auth.pat import SQLitePatStore, verify_pat
from meme_mcp.envelope import Envelope

EXPECTED_TOOLS = {"find", "generate"}


class MCPBackend(Protocol):
    def find(self, query: str, filters: dict[str, Any] | None = None) -> Envelope: ...

    def generate(
        self,
        template_id: str,
        slot_fills: list[str],
        dry_run: bool = False,
        actor: str | None = None,
    ) -> Envelope: ...


def tool_schemas() -> dict[str, dict[str, Any]]:
    return {
        "find": {
            "description": "Find 3-5 ranked meme templates for a query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "filters": {"type": "object", "additionalProperties": True},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        "generate": {
            "description": "Render a selected meme template and return a receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "slot_fills": {"type": "array", "items": {"type": "string"}},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["template_id", "slot_fills"],
                "additionalProperties": False,
            },
        },
    }


class PatTokenVerifier(TokenVerifier):
    def __init__(
        self,
        pat_store: SQLitePatStore,
        allowlist: Container[str],
        pepper: str,
    ) -> None:
        self.pat_store = pat_store
        self.allowlist = allowlist
        self.pepper = pepper

    async def verify_token(self, token: str) -> AccessToken | None:
        login = verify_pat(self.pat_store, token, self.pepper)
        if login is None or login not in self.allowlist:
            return None
        return AccessToken(token=token, client_id=login, scopes=["meme:read", "meme:write"])


def create_mcp_server(
    pat_store: SQLitePatStore,
    allowlist: Container[str],
    pepper: str,
    backend: MCPBackend | None = None,
) -> FastMCP:
    mcp = FastMCP(
        "meme-mcp",
        instructions="Find and render private meme templates.",
        token_verifier=PatTokenVerifier(pat_store, allowlist, pepper),
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        auth=AuthSettings.model_validate(
            {
                "issuer_url": "http://localhost:8000",
                "resource_server_url": "http://localhost:8000/mcp",
                "required_scopes": ["meme:read"],
            }
        ),
    )

    @mcp.tool()
    def find(query: str, filters: dict[str, Any] | None = None) -> dict[str, Any] | Envelope:
        """Find 3-5 ranked meme templates for a query."""
        if backend is None:
            return {"ok": True, "data": {"query": query, "candidates": []}}
        return backend.find(query, filters)

    @mcp.tool()
    def generate(
        template_id: str,
        slot_fills: list[str],
        dry_run: bool = False,
        ctx: Context[Any, Any, Any] | None = None,
    ) -> dict[str, Any] | Envelope:
        """Render a selected meme template and return a receipt."""
        if backend is None:
            return {
                "ok": True,
                "data": {
                    "template_id": template_id,
                    "slot_count": len(slot_fills),
                    "rendered_url": None if dry_run else "",
                },
            }
        actor = ctx.client_id if ctx is not None else None
        return backend.generate(template_id, slot_fills, dry_run, actor)

    return mcp
