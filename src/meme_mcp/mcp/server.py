from __future__ import annotations

from typing import Any, Protocol

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    OAuthAuthorizationServerProvider,
    TokenVerifier,
)
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from meme_mcp.auth.authorization import (
    SupportsAllowlist,
    SupportsPinLookup,
    is_authorized,
)
from meme_mcp.auth.pat import SQLitePatStore, scopes_for_capability, verify_pat
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
        scopes = scopes_for_capability(capability)
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
    auth_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
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
    # Two mutually-exclusive auth modes. AS mode (auth_provider supplied) makes
    # meme-mcp its own OAuth 2.1 authorization server: FastMCP auto-mounts the
    # five OAuth routes and auto-wraps the provider as the bearer verifier (its
    # load_access_token also recognizes existing PATs, R13). Without it, the
    # original resource-server-only PAT verifier is used.
    auth_config: dict[str, Any] = {
        "issuer_url": public_base_url,
        "resource_server_url": f"{public_base_url}/mcp",
        "required_scopes": ["meme:read"],
    }
    auth_kwargs: dict[str, Any] = {}
    if auth_provider is not None:
        auth_kwargs["auth_server_provider"] = auth_provider
        auth_config["client_registration_options"] = {
            "enabled": True,
            "valid_scopes": ["meme:read", "meme:write"],
            "default_scopes": ["meme:read"],
        }
        auth_config["revocation_options"] = {"enabled": True}
    else:
        auth_kwargs["token_verifier"] = PatTokenVerifier(pat_store, allowlist, pepper, pin_store)
    mcp = FastMCP(
        "meme-mcp",
        instructions="Find and render private meme templates.",
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
        auth=AuthSettings.model_validate(auth_config),
        **auth_kwargs,
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
    if access is None:
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "auth", "reason": "missing"}])
    # An OAuth token (MemeAccessToken) carries the friend `principal` separately
    # from the OAuth `client_id`, so actions attribute to the friend, not the
    # registered client (F-003). A PAT token sets client_id == principal, so the
    # client_id fallback preserves the existing behavior when the AS flag is off.
    actor = getattr(access, "principal", None) or access.client_id
    if not actor:
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "auth", "reason": "missing"}])
    return actor


def _require_write_scope() -> None:
    access = get_access_token()
    if access is None or "meme:write" not in access.scopes:
        raise MemeMCPError(
            ErrorCode.UNAUTHORIZED,
            [{"field": "scope", "reason": "meme:write_required"}],
        )


def build_auth_server_routes(
    provider: OAuthAuthorizationServerProvider[Any, Any, Any],
    auth_settings: AuthSettings,
) -> list[Any]:
    """Mirror the SDK OAuth routes onto the parent app at the issuer origin root.

    FastMCP mounts ``create_auth_routes`` inside the ``/mcp`` sub-app, so the five
    endpoints externally resolve at ``/mcp/authorize`` while ``build_metadata``
    advertises them at the origin root (KTD6). Re-create them at the origin root
    so the advertised endpoints actually resolve, and patch the AS metadata to
    also advertise the public-client auth method ``none`` -- the AS accepts public
    PKCE clients, but the stock metadata hard-codes only ``client_secret_*``.
    """
    from mcp.server.auth.json_response import PydanticJSONResponse
    from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from starlette.routing import Route

    cro = auth_settings.client_registration_options or ClientRegistrationOptions()
    ro = auth_settings.revocation_options or RevocationOptions()
    routes = create_auth_routes(
        provider, auth_settings.issuer_url, auth_settings.service_documentation_url, cro, ro
    )
    metadata = build_metadata(
        auth_settings.issuer_url, auth_settings.service_documentation_url, cro, ro
    )
    methods = list(metadata.token_endpoint_auth_methods_supported or [])
    if "none" not in methods:
        metadata.token_endpoint_auth_methods_supported = [*methods, "none"]

    async def metadata_endpoint(_request: Any) -> Any:
        return PydanticJSONResponse(
            content=metadata, status_code=200, headers={"Cache-Control": "public, max-age=300"}
        )

    metadata_path = "/.well-known/oauth-authorization-server"
    patched = Route(
        metadata_path,
        endpoint=cors_middleware(metadata_endpoint, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )
    return [patched if route.path == metadata_path else route for route in routes]
