"""Google OpenID Connect provider behind a small two-method seam.

Google is OIDC, so its token response carries a signed ``id_token`` plus a
``nonce`` -- JWT/JWKS/nonce validation that the hand-rolled GitHub flow does not
need. Authlib handles discovery, PKCE, ``state``, ``nonce``, and ID-token
validation, so this module is a thin wrapper exposing exactly what the routes
call:

* ``authorize_redirect(request, redirect_uri)`` -- start the flow.
* ``resolve_identity(request) -> ResolvedIdentity`` -- finish it, returning the
  claims read from the nonce-validated ``id_token`` (never a separate
  ``/userinfo`` fetch).

Both providers converge on one :class:`ResolvedIdentity` shape so the
``email_verified`` gate and principal-minting read a single
structure at one chokepoint (U5). The GitHub flow stays hand-rolled; this is an
identity-type convergence, not a shared fetch interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from meme_mcp.errors import ErrorCode, MemeMCPError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"


@dataclass(frozen=True)
class ResolvedIdentity:
    """A provider's resolved claims, shared by both callbacks.

    ``email_verified`` carries the raw claim value (bool, the strings Google has
    historically emitted, or ``None``); the strict ``is True`` gate is applied at
    the callback (R15), not here, so the gate can reject the string forms. GitHub
    sets ``email=None`` and ``email_verified=False`` since it has no verified
    email in this flow.
    """

    provider: str
    subject: str
    email: str | None = None
    email_verified: bool | str | None = False


class GoogleOAuth(Protocol):
    async def authorize_redirect(self, request: Request, redirect_uri: str) -> Response: ...

    async def resolve_identity(self, request: Request) -> ResolvedIdentity: ...


class GoogleOAuthUnavailable:
    """Null object when Google sign-in is not configured (mirrors GitHub).

    Any attempt to drive the flow raises the standard unauthorized error rather
    than dereferencing an unregistered client.
    """

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> Response:
        del request, redirect_uri
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "oauth", "reason": "unavailable"}])

    async def resolve_identity(self, request: Request) -> ResolvedIdentity:
        del request
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "oauth", "reason": "unavailable"}])


class GoogleOAuthClient:
    """Authlib-backed Google OIDC client.

    Registration is config-only (no network); discovery is fetched lazily by
    Authlib on the first authorize. The routes never touch the Authlib registry
    directly -- they call the two seam methods.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        server_metadata_url: str = GOOGLE_DISCOVERY_URL,
    ) -> None:
        from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]

        oauth = OAuth()
        oauth.register(
            name="google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=server_metadata_url,
            client_kwargs={"scope": "openid email"},
        )
        self._client = oauth.create_client("google")

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> Response:
        # Authlib generates and stores state, nonce, and the PKCE S256 challenge
        # in request.session. (Authlib is untyped, so the return is Any.)
        return await self._client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]

    async def resolve_identity(self, request: Request) -> ResolvedIdentity:
        # authorize_access_token validates state, the ID token, and the nonce.
        token = await self._client.authorize_access_token(request)
        claims = token.get("userinfo") or {}
        return ResolvedIdentity(
            provider="google",
            subject=str(claims.get("sub", "")),
            email=claims.get("email"),
            email_verified=claims.get("email_verified"),
        )
