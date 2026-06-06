"""MemeAuthProvider: the MCP SDK ``OAuthAuthorizationServerProvider`` implementation.

Mints the server's own opaque tokens (never forwarding an upstream GitHub/Google
token) and owns the single per-request bearer-verification path, which recognizes
both newly-issued OAuth tokens and existing PATs so the ``mcp-remote`` + PAT path
keeps working with no client change (R13). Authorization is re-checked live on
every request via the shared ``is_authorized`` leaf (KTD4), so removing a friend
denies their next call.

Division of labor with the SDK (verified against ``mcp`` 1.27.1):

* The SDK ``TokenHandler`` verifies the PKCE ``code_verifier``, the redirect-uri
  match, code expiry, and the code/refresh ``client_id`` binding **before**
  calling ``exchange_*`` (F-004) -- this provider stores/returns the challenge and
  never sees the verifier.
* The SDK ``ClientAuthenticator`` compares a presented client secret directly to
  the value ``get_client`` returns, so a confidential client's secret is stored
  encrypted (reversible) and returned in plaintext here (F-001).
* The issued ``AccessToken`` is a subclass carrying the OAuth ``client_id`` and
  the friend ``principal`` separately: ``client_id`` keeps the SDK ``/revoke``
  ``token.client_id == client.client_id`` check working, while the tool layer
  reads ``principal`` for action attribution (F-003).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from meme_mcp.auth.authorization import (
    SupportsAllowlist,
    SupportsPinLookup,
    is_authorized,
)
from meme_mcp.auth.pat import SQLitePatStore, scopes_for_capability, verify_pat
from meme_mcp.oauth.store import ACCESS_TTL_SECONDS, SQLiteOAuthStore

# The parent-app consent route (U4) the provider redirects to from authorize().
# A relative path resolves against the issuer origin where /authorize is served.
CONSENT_PATH = "/oauth/consent"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class MemeAuthorizationCode(AuthorizationCode):
    """SDK ``AuthorizationCode`` plus the friend principal the code was issued to."""

    principal: str


class MemeRefreshToken(RefreshToken):
    """SDK ``RefreshToken`` plus internal mint context (principal, resource, family)."""

    principal: str
    resource: str | None = None
    family_id: str


class MemeAccessToken(AccessToken):
    """SDK ``AccessToken`` plus the friend ``principal`` (distinct from ``client_id``)."""

    principal: str


def _validate_redirect_uri(uri: AnyUrl) -> None:
    """Exact-match redirect-URI hardening at registration (R7).

    Rejects wildcards, query strings, and fragments (only exact URIs are honored)
    and requires ``https`` except for an http loopback (local dev). Raises
    ``RegistrationError`` so the SDK returns RFC 7591 ``invalid_redirect_uri``.
    """
    raw = str(uri)
    if "*" in raw:
        raise RegistrationError("invalid_redirect_uri", "redirect_uri must not contain a wildcard")
    parsed = urlsplit(raw)
    if parsed.fragment:
        raise RegistrationError("invalid_redirect_uri", "redirect_uri must not contain a fragment")
    if parsed.query:
        raise RegistrationError(
            "invalid_redirect_uri", "redirect_uri must not contain a query string"
        )
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        return
    raise RegistrationError(
        "invalid_redirect_uri", "redirect_uri must be https (or http on loopback)"
    )


class MemeAuthProvider(
    OAuthAuthorizationServerProvider[MemeAuthorizationCode, MemeRefreshToken, MemeAccessToken]
):
    def __init__(
        self,
        *,
        store: SQLiteOAuthStore,
        allowlist: SupportsAllowlist,
        pat_store: SQLitePatStore,
        pat_pepper: str,
        resource_url: str,
        pin_store: SupportsPinLookup | None = None,
    ) -> None:
        self.store = store
        self.allowlist = allowlist
        self.pat_store = pat_store
        self.pat_pepper = pat_pepper
        self.resource_url = resource_url
        self.pin_store = pin_store

    # -- registration & lookup --------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            _validate_redirect_uri(uri)
        # The SDK RegistrationHandler already minted a client_secret for a
        # confidential client (auth method != "none"); the store encrypts it.
        self.store.register_client(client_info)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    # -- authorize (no Request in this hook; park state by nonce) ---------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        nonce = self.store.create_pending_request(
            client_id=str(client.client_id),
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=params.scopes or [],
            resource=params.resource,
            state=params.state,
        )
        return f"{CONSENT_PATH}?rid={nonce}"

    # -- authorization-code exchange (PKCE already verified by the SDK) ---------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> MemeAuthorizationCode | None:
        stored = self.store.load_auth_code(authorization_code)
        if stored is None:
            return None
        return MemeAuthorizationCode(
            code=authorization_code,
            scopes=list(stored.scopes),
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=AnyUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource,
            principal=stored.principal,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: MemeAuthorizationCode
    ) -> OAuthToken:
        # Single-use: consume atomically, rejecting a replay (R10). The SDK has
        # already verified PKCE + redirect + client binding by this point.
        if not self.store.consume_auth_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        scopes = list(authorization_code.scopes)
        access, refresh = self.store.issue_initial_tokens(
            client_id=str(client.client_id),
            principal=authorization_code.principal,
            scopes=scopes,
            resource=authorization_code.resource,
        )
        self.store.mark_client_used(str(client.client_id))
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=ACCESS_TTL_SECONDS,
            scope=" ".join(scopes) or None,
        )

    # -- refresh rotation -------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> MemeRefreshToken | None:
        stored = self.store.load_refresh_token(refresh_token)
        if stored is None:
            return None
        return MemeRefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=list(stored.scopes),
            expires_at=int(stored.expires_at.timestamp()) if stored.expires_at else None,
            principal=stored.principal,
            resource=stored.resource,
            family_id=stored.family_id,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: MemeRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # The SDK already enforced client_id binding + scope-subset; rotate (or
        # detect reuse beyond the grace window -> family revoked -> None).
        rotated = self.store.rotate_refresh_token(refresh_token.token, scopes)
        if rotated is None:
            raise TokenError("invalid_grant", "refresh token is not valid")
        access, new_refresh = rotated
        self.store.mark_client_used(str(client.client_id))
        granted = scopes or list(refresh_token.scopes)
        return OAuthToken(
            access_token=access,
            refresh_token=new_refresh,
            expires_in=ACCESS_TTL_SECONDS,
            scope=" ".join(granted) or None,
        )

    # -- bearer verification (per request): OAuth token OR PAT fallback ---------

    async def load_access_token(self, token: str) -> MemeAccessToken | None:
        stored = self.store.load_access_token(token)
        if stored is not None:
            # Live authorization re-check (no caching) -- the same leaf the web
            # and PAT front doors call, so removing a friend denies the next call.
            if not is_authorized(
                stored.principal, allowlist=self.allowlist, pin_store=self.pin_store
            ):
                return None
            return MemeAccessToken(
                token=token,
                client_id=stored.client_id,
                scopes=list(stored.scopes),
                expires_at=int(stored.expires_at.timestamp()),
                resource=stored.resource,
                principal=stored.principal,
            )
        # Backward-compatible PAT fallback (R13): an existing mcp-remote PAT still
        # authenticates. client_id == principal here (no OAuth client involved).
        result = verify_pat(self.pat_store, token, self.pat_pepper)
        if result is None:
            return None
        principal, capability = result
        if not is_authorized(principal, allowlist=self.allowlist, pin_store=self.pin_store):
            return None
        return MemeAccessToken(
            token=token,
            client_id=principal,
            scopes=scopes_for_capability(capability),
            expires_at=None,
            resource=self.resource_url,
            principal=principal,
        )

    async def revoke_token(self, token: MemeAccessToken | MemeRefreshToken) -> None:
        self.store.revoke_token(token.token)
