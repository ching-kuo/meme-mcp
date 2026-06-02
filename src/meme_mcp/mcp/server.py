from __future__ import annotations

from typing import Any, Protocol

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from meme_mcp.auth.authorization import (
    SupportsAllowlist,
    SupportsPinLookup,
    is_authorized,
)
from meme_mcp.auth.pat import SQLitePatStore, verify_pat
from meme_mcp.envelope import Envelope
from meme_mcp.errors import ErrorCode, MemeMCPError

EXPECTED_TOOLS = {"find", "generate", "record_outcome"}


class MCPBackend(Protocol):
    def find(
        self,
        query: str,
        filters: dict[str, Any] | None,
        actor: str,
    ) -> Envelope: ...

    def generate(
        self,
        template_id: str,
        slot_fills: list[str],
        dry_run: bool,
        actor: str,
    ) -> Envelope: ...

    def record_outcome(
        self,
        template_id: str,
        outcome: str,
        actor: str,
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
        "record_outcome": {
            "description": "Report what happened with a template ('used'/'sent'/'dropped').",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["used", "sent", "dropped"]},
                },
                "required": ["template_id", "outcome"],
                "additionalProperties": False,
            },
        },
    }


class PatTokenVerifier(TokenVerifier):
    def __init__(
        self,
        pat_store: SQLitePatStore,
        allowlist: SupportsAllowlist,
        pepper: str,
        pin_store: SupportsPinLookup | None = None,
    ) -> None:
        self.pat_store = pat_store
        self.allowlist = allowlist
        self.pepper = pepper
        self.pin_store = pin_store

    async def verify_token(self, token: str) -> AccessToken | None:
        result = verify_pat(self.pat_store, token, self.pepper)
        if result is None:
            return None
        principal, capability = result
        # The MCP transport's authorization MUST route through the same predicate
        # as the browser and web-PAT front doors; a bare membership test here
        # would 401 every Google friend's PAT while their browser session works.
        # No caching: each request re-checks live allowlist + pin state (R12).
        if not is_authorized(principal, allowlist=self.allowlist, pin_store=self.pin_store):
            return None
        scopes = ["meme:read"] + (["meme:write"] if capability == "readwrite" else [])
        return AccessToken(token=token, client_id=principal, scopes=scopes)


def create_mcp_server(
    pat_store: SQLitePatStore,
    allowlist: SupportsAllowlist,
    pepper: str,
    public_base_url: str,
    backend: MCPBackend | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    pin_store: SupportsPinLookup | None = None,
) -> FastMCP:
    # The Streamable HTTP transport runs a DNS-rebinding guard that rejects any
    # Host/Origin not on its allowlist with 421. FastMCP only auto-populates that
    # allowlist for a localhost bind, so a public deployment behind a gateway
    # (Host like meme.igene.tw) must pass its own hosts explicitly or every
    # authenticated request 421s. Bearer-PAT auth already blocks browser-driven
    # rebinding (no ambient credentials); the allowlist is defense-in-depth.
    hosts = (
        allowed_hosts if allowed_hosts is not None else ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    )
    origins = (
        allowed_origins
        if allowed_origins is not None
        else ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    )
    public_base_url = public_base_url.rstrip("/")
    mcp = FastMCP(
        "meme-mcp",
        instructions="Find and render private meme templates.",
        token_verifier=PatTokenVerifier(pat_store, allowlist, pepper, pin_store),
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
        auth=AuthSettings.model_validate(
            {
                "issuer_url": public_base_url,
                "resource_server_url": f"{public_base_url}/mcp",
                "required_scopes": ["meme:read"],
            }
        ),
    )

    @mcp.tool()
    def find(query: str, filters: dict[str, Any] | None = None) -> dict[str, Any] | Envelope:
        """Find 3-5 ranked meme templates for a query."""
        if backend is None:
            return {"ok": True, "data": {"query": query, "candidates": []}}
        return backend.find(query, filters, _authenticated_actor())

    @mcp.tool()
    def generate(
        template_id: str, slot_fills: list[str], dry_run: bool = False
    ) -> dict[str, Any] | Envelope:
        """Render a selected meme template and return a receipt."""
        actor = _authenticated_actor()
        _require_write_scope()
        if backend is None:
            return {
                "ok": True,
                "data": {
                    "template_id": template_id,
                    "slot_count": len(slot_fills),
                    "rendered_url": None if dry_run else "",
                },
            }
        return backend.generate(template_id, slot_fills, dry_run, actor)

    @mcp.tool()
    def record_outcome(template_id: str, outcome: str) -> dict[str, Any] | Envelope:
        """Report what happened with a template ('used'/'sent'/'dropped')."""
        actor = _authenticated_actor()
        _require_write_scope()
        if backend is None:
            return {"ok": True, "data": {"template_id": template_id, "outcome": outcome}}
        return backend.record_outcome(template_id, outcome, actor)

    return mcp


def _authenticated_actor() -> str:
    access = get_access_token()
    if access is None or not access.client_id:
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "auth", "reason": "missing"}])
    return access.client_id


def _require_write_scope() -> None:
    access = get_access_token()
    if access is None or "meme:write" not in access.scopes:
        raise MemeMCPError(
            ErrorCode.UNAUTHORIZED,
            [{"field": "scope", "reason": "meme:write_required"}],
        )
