from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from PIL import Image
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meme_mcp.audit.sink import JsonlAuditSink
from meme_mcp.auth.allowlist import FileAllowlist, canonical_email
from meme_mcp.auth.authorization import display_label, is_authorized, normalize_principal
from meme_mcp.auth.depends import require_write
from meme_mcp.auth.google_oauth import GoogleOAuth
from meme_mcp.auth.google_pins import SQLiteGooglePinStore
from meme_mcp.auth.pat import SQLitePatStore, expires_at_for_login
from meme_mcp.auth.session import (
    friend_from_header,
    friend_from_request_or_header,
    has_web_session,
    session_login,
)
from meme_mcp.config import (
    Settings,
    resolve_public_base_url,
    session_cookie_secure,
    validate_at_startup,
)
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.migrations import run_migrations
from meme_mcp.db.outcomes import VALID_OUTCOMES, OutcomeEventStore
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateRow
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.db.vectors import EmbeddingMetaStore, SQLiteVecStore
from meme_mcp.embeddings.client import EmbeddingClient, validate_embedding_model
from meme_mcp.envelope import Envelope, make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.mcp.server import build_auth_server_routes, create_mcp_server, tool_schemas
from meme_mcp.metadata_locales import localized_metadata
from meme_mcp.rendering.image_store import make_image_store_from_settings
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme
from meme_mcp.rendering.signing import sign_render_url, verify_render_signature
from meme_mcp.retrieval.search import project_candidate_english
from meme_mcp.upload.dedupe import DuplicateIndex
from meme_mcp.upload.service import UploadServiceDeps, analyze_image, approve_pending
from meme_mcp.vlm.client import VLMClient
from meme_mcp.vlm.sanitize import sanitize_url
from meme_mcp.web.csrf import ensure_csrf_token, require_csrf, safe_lang_return, safe_next
from meme_mcp.web.i18n import COOKIE_NAME, SUPPORTED, js_catalog, plural, resolve_locale, t
from meme_mcp.web.pat_routes import register_pat_routes
from meme_mcp.web.upload_routes import register_upload_routes

if TYPE_CHECKING:
    from pydantic import SecretStr

    from meme_mcp.oauth.provider import MemeAuthProvider

# Pre-buffer body-size cap for the analyze endpoints. A 10 MB image base64
# encodes to ~13.3 MB; 14 MB leaves headroom for the surrounding JSON envelope
# while still rejecting clearly oversized bodies before they are buffered into
# memory (KTD11). validate_upload's 10 MB content check stays authoritative.
MAX_ANALYZE_BODY_BYTES = 14 * 1024 * 1024
_ANALYZE_PATHS = frozenset({"/api/uploads/analyze", "/upload/analyze"})

# Language-preference cookie lifetime (~1 year). The choice is a non-sensitive
# display preference (KTD5), so it persists long across sessions.
LANG_COOKIE_MAX_AGE = 31536000

# Templates shown per /browse page. The full row set is loaded (it is also the
# total-count source and the search reorder input), then sliced in Python; the
# library is small enough that DB-side LIMIT/OFFSET buys nothing.
BROWSE_PAGE_SIZE = 24


class BodySizeGuardMiddleware:
    """Bound the buffered body of analyze POSTs before the route parses it.

    A pure-ASGI middleware (not BaseHTTPMiddleware) so the rejection happens
    before Starlette parses or buffers the request body. It only inspects POSTs
    to the two analyze paths (KTD11/F01); every other request passes through
    untouched. A present, oversized Content-Length is rejected immediately; for
    requests with no/understated Content-Length (e.g. Transfer-Encoding: chunked)
    the body is read here and capped at max_bytes -- rejecting on overflow -- so a
    chunked client cannot drive request.json() into unbounded buffering. Returns
    the standard JSON error envelope rather than a 500.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path", "") in _ANALYZE_PATHS
        ):
            content_length = _header_value(scope, b"content-length")
            if content_length is not None and content_length > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                more_body = message.get("more_body", False)
            buffered = bytes(body)
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": buffered, "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, replay, send)
            return
        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            make_error(ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "size"}]),
            status_code=status_for_error(ErrorCode.UPLOAD_REJECTED),
        )
        await response(scope, receive, send)


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drive the mounted MCP transport's session manager for the app's lifetime.

    ``streamable_http_app()`` returns a sub-app whose own lifespan runs
    ``session_manager.run()`` (which starts the task group every request needs).
    Mounting a sub-app does not run its lifespan, so without this the transport
    raises "Task group is not initialized" on first request. The session manager
    is exposed for exactly this multi-app mounting case; it exists once
    ``streamable_http_app()`` has been called, which create_app does at mount
    time. When the app is built without settings there is no MCP server to run.
    """
    mcp_server = getattr(app.state, "mcp_server", None)
    if mcp_server is None:
        yield
        return
    async with mcp_server.session_manager.run():
        yield


class McpSlashNormalizeMiddleware:
    """Resolve a bare ``/mcp`` request to the mounted ``/mcp/`` endpoint in-process.

    The MCP Streamable HTTP app is mounted at ``/mcp`` with its own route at
    ``/``, so its real endpoint is ``/mcp/``. Starlette's Mount answers a bare
    ``/mcp`` (no trailing slash) with a 307 redirect to ``/mcp/``. Clients like
    ``mcp-remote`` do not replay the POST body across that redirect and fail
    with a 307 error, so we rewrite the path before routing instead -- no HTTP
    redirect, both ``/mcp`` and ``/mcp/`` reach the transport. Only the exact
    bare path is touched; ``/mcp/...`` sub-paths pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            updates: dict[str, object] = {"path": "/mcp/"}
            # Only rewrite raw_path when it matches the bare form; a proxy may
            # have set it to something else, and Mount routes on path anyway.
            if scope.get("raw_path") == b"/mcp":
                updates["raw_path"] = b"/mcp/"
            scope = {**scope, **updates}
        await self.app(scope, receive, send)


class OAuthRateLimitMiddleware:
    """Per-IP rate limit on the unauthenticated OAuth endpoints (/register,
    /authorize, /token, /revoke).

    These are SDK-generated routes mirrored onto the parent app, so the
    decorator-based ``pat_admin_limiter`` does not apply; this path-matching ASGI
    middleware fills the gap (R7/U6 open-DCR abuse mitigation). It reads only the
    ASGI scope (never the request body), so the ``/token`` form stays intact for
    the SDK ``TokenHandler`` downstream. The allowlist-at-issuance check remains
    the primary gate; this is defense-in-depth.
    """

    _PATHS = frozenset({"/register", "/authorize", "/token", "/revoke"})

    def __init__(self, app: ASGIApp, limiter: WindowedRateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self._PATHS:
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            try:
                self.limiter.hit(f"{scope['path']}:{ip}")
            except MemeMCPError as exc:
                payload = json.dumps(make_error(exc.error_code, exc.errors)).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": status_for_error(exc.error_code),
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return
        await self.app(scope, receive, send)


class LocaleVaryMiddleware:
    """Add ``Vary: Cookie, Accept-Language`` to HTML responses.

    Locale is negotiated from the ``lang`` cookie and the ``Accept-Language``
    header (U1/KTD2), so a cache must key on both or it could serve one
    visitor's language to another. A no-op for the current direct-served setup;
    it makes a future CDN/reverse proxy correct out of the box. Only ``text/html``
    responses are touched, and an existing ``Vary`` is extended, never clobbered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/html"):
                    existing = headers.get("vary")
                    parts = [p.strip() for p in existing.split(",")] if existing else []
                    lowered = {p.lower() for p in parts}
                    for value in ("Cookie", "Accept-Language"):
                        if value.lower() not in lowered:
                            parts.append(value)
                    headers["vary"] = ", ".join(parts)
            await send(message)

        await self.app(scope, receive, send_with_vary)


def _i18n_context(request: Request) -> dict[str, object]:
    """Inject the active locale and bound translation helpers into every render.

    Attached to the shared ``Jinja2Templates`` instance as a context processor
    (KTD3), so all ``TemplateResponse`` call sites -- in ``app.py`` and
    ``pat_routes.py`` -- receive ``t``, ``locale``, ``supported_locales``, and
    ``plural`` without per-route plumbing. ``t`` and ``plural`` are bound to the
    resolved locale so templates call ``t("key")`` / ``plural(n, "base")``.
    """

    locale = resolve_locale(request)
    return {
        "t": partial(t, locale=locale),
        "locale": locale,
        "supported_locales": SUPPORTED,
        "plural": partial(plural, locale=locale),
        "js_catalog_json": _js_catalog_json(locale),
    }


def _js_catalog_json(locale: str) -> Markup:
    """Serialize the active locale's JS catalog for embedding in a <script> tag.

    Jinja's HTML autoescaping does not protect inside a ``<script>`` element, so
    every ``<`` is escaped to ``\\u003c`` before embedding (KTD6). That neutralizes
    ``</script>``, ``<!--``, and ``<script`` breakout sequences while remaining
    valid JSON (``JSON.parse`` restores the ``<``). Wrapped in ``Markup`` so Jinja
    emits it verbatim instead of re-escaping the quotes.
    """

    raw = json.dumps(js_catalog(locale), ensure_ascii=False)
    return Markup(raw.replace("<", "\\u003c"))


def _header_value(scope: Scope, name: bytes) -> int | None:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return int(value)
            except ValueError:
                return None
    return None


class GitHubOAuthClient(Protocol):
    async def fetch_user(self, code: str, code_verifier: str | None = None) -> dict[str, str]: ...


class GitHubOAuthUnavailable:
    async def fetch_user(self, code: str, code_verifier: str | None = None) -> dict[str, str]:
        del code, code_verifier
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "oauth", "reason": "unavailable"}])


class GitHubOAuthHTTPClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http_client = http_client or httpx.AsyncClient()

    async def fetch_user(self, code: str, code_verifier: str | None = None) -> dict[str, str]:
        token_response = await self.http_client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier or "",
            },
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("GitHub OAuth response missing access_token")
        user_response = await self.http_client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        user_response.raise_for_status()
        user = user_response.json()
        return {"login": str(user.get("login", ""))}


# Stored template images keep their original extension (seeds are png; uploads
# keep their validated mime's extension). The /templates/{id}/image route maps
# the extension back to an explicit content type so the response stays renderable
# under X-Content-Type-Options: nosniff.
_IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is not None:
        validate_at_startup(settings)
    Image.MAX_IMAGE_PIXELS = 40 * 1024 * 1024
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    app = FastAPI(title="meme-mcp", lifespan=_app_lifespan)
    web_dir = Path(__file__).parent / "web"
    templates = Jinja2Templates(directory=web_dir / "templates", context_processors=[_i18n_context])
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
    # Locale varies by cookie + Accept-Language; tag HTML so any future cache
    # keys on it. Added before the settings-gated middleware so it wraps every
    # templated response, including the settings-less test app's landing page.
    app.add_middleware(LocaleVaryMiddleware)
    if settings is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret.get_secret_value(),
            same_site="lax",
            https_only=session_cookie_secure(settings),
        )
        # Added after SessionMiddleware so it is the OUTERMOST layer
        # (add_middleware prepends): it rejects oversized analyze bodies on
        # Content-Length before the session layer or any route reads the body
        # (KTD11/F01).
        app.add_middleware(BodySizeGuardMiddleware, max_bytes=MAX_ANALYZE_BODY_BYTES)
        app.state.settings = settings
        app.state.web_templates = templates
        run_migrations(settings)
        db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
        app.state.pat_store = SQLitePatStore(db_path)
        # Google sub->email pins. Held by app.state so the three front doors read
        # live pin state per request (the is_authorized google branch consults it).
        app.state.pin_store = SQLiteGooglePinStore(db_path)
        app.state.receipts = ReceiptStore(db_path)
        app.state.outcomes = OutcomeEventStore(db_path)
        # Wire the semantic layer only on the serving path: the repository
        # composes the vector store + query embedder around the pure lexical
        # search() as an ADDITIVE boost, degrading to lexical-only on any
        # embedding/store failure (U7). The store dimensions follow
        # EMBEDDING_DIMENSIONS so a query vector length mismatch is caught and
        # degraded rather than corrupting cosine. CLI/seed paths build the
        # repository with no embedder/store and keep the pure lexical behavior.
        app.state.search_vector_store = SQLiteVecStore(
            db_path, dimensions=settings.embedding_dimensions
        )
        app.state.search_embedder = EmbeddingClient(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key.get_secret_value(),
            base_url=settings.embedding_base_url,
        )
        app.state.templates = SQLiteTemplateRepository(
            db_path,
            embedder=app.state.search_embedder,
            vector_store=app.state.search_vector_store,
        )
        app.state.pending_uploads = PendingUploadStore(db_path)
        app.state.embedding_meta = EmbeddingMetaStore(db_path)
        validate_embedding_model(
            app.state.embedding_meta,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        app.state.image_store = make_image_store_from_settings(settings)
        app.state.web_allowlist = FileAllowlist(settings.github_allowlist_path)
        app.state.allowlist = app.state.web_allowlist
        app.state.find_limiter = WindowedRateLimiter(settings.rate_find_per_min, 60)
        app.state.generate_limiter = WindowedRateLimiter(settings.rate_generate_per_min, 60)
        app.state.upload_limiter = WindowedRateLimiter(settings.rate_upload_per_hour, 60 * 60)
        app.state.pat_admin_limiter = WindowedRateLimiter(
            settings.rate_pat_admin_per_hour, 60 * 60
        )
        audit_log_path = settings.audit_log_path or str(Path(settings.storage_dir) / "audit.jsonl")
        app.state.audit_sink = JsonlAuditSink(audit_log_path)
        app.state.github_oauth = GitHubOAuthHTTPClient(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret.get_secret_value(),
            redirect_uri=settings.github_redirect_uri,
        )
        # Null object when Google sign-in is off (KTD); the real client is built
        # once here so missing/malformed config fails fast at startup, not at the
        # first /auth/google/login.
        app.state.google_oauth = _make_google_oauth(settings)
        public_app_base_url = resolve_public_base_url(settings)
        # Stored so AppMCPBackend.generate can build absolute rendered_url values
        # off the same origin advertised in OAuth metadata.
        app.state.public_app_base_url = public_app_base_url
        app.state.vlm_client = VLMClient(
            settings.vlm_model,
            api_key=settings.vlm_api_key.get_secret_value(),
            base_url=settings.vlm_base_url,
        )
        # None when the feature is off (KTD7); the credentials path was already
        # validated as a regular file at startup (U1). Built once here so a
        # missing/malformed SA file fails fast, not at first request (KTD4).
        app.state.reverse_image_client = _make_reverse_image_client(settings)
        app.state.pat_hash_pepper_value = settings.pat_hash_pepper.get_secret_value()
        oauth_provider = _make_oauth_provider(app, settings, db_path, public_app_base_url)
        app.state.mcp_server = create_mcp_server(
            app.state.pat_store,
            app.state.allowlist,
            app.state.pat_hash_pepper_value,
            public_app_base_url,
            AppMCPBackend(app),
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
            pin_store=app.state.pin_store,
            auth_provider=oauth_provider,
        )
        auth_settings = app.state.mcp_server.settings.auth
        if auth_settings is not None and auth_settings.resource_server_url is not None:
            from mcp.server.auth.routes import create_protected_resource_routes

            # FastMCP registers RFC 9728 metadata inside the mounted sub-app,
            # which makes the external path `/mcp/.well-known/...` instead of
            # the spec path at the origin root. Mirror the route on the parent
            # app so the advertised resource_metadata URL actually resolves.
            app.router.routes.extend(
                create_protected_resource_routes(
                    resource_url=auth_settings.resource_server_url,
                    authorization_servers=[auth_settings.issuer_url],
                    scopes_supported=auth_settings.required_scopes,
                )
            )
        app.mount("/mcp", app.state.mcp_server.streamable_http_app())
        # Rewrite bare /mcp -> /mcp/ before routing so the mount serves it
        # directly instead of 307-redirecting (which mcp-remote can't follow on
        # POST). add_middleware prepends, so this runs before SessionMiddleware
        # and routing.
        app.add_middleware(McpSlashNormalizeMiddleware)
        # AS mode: FastMCP mounts the OAuth routes inside /mcp, but the metadata
        # advertises them at the origin root. Mirror them onto the parent app at
        # the origin root (KTD6) and register the parent-app consent route the
        # provider's authorize() redirects to (U4).
        if oauth_provider is not None:
            from meme_mcp.oauth.consent import register_consent_routes

            app.router.routes.extend(
                build_auth_server_routes(oauth_provider, app.state.mcp_server.settings.auth)
            )
            register_consent_routes(app, provider=oauth_provider, templates=templates)
            app.state.oauth_limiter = WindowedRateLimiter(settings.rate_oauth_per_min, 60)
            app.add_middleware(OAuthRateLimitMiddleware, limiter=app.state.oauth_limiter)

    @app.exception_handler(MemeMCPError)
    async def meme_error_handler(_request: Request, exc: MemeMCPError) -> JSONResponse:
        return JSONResponse(
            make_error(exc.error_code, exc.errors),
            status_code=status_for_error(exc.error_code),
        )

    @app.exception_handler(404)
    async def not_found_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(make_error(ErrorCode.NOT_FOUND, []), status_code=404)

    @app.get("/")
    async def landing(request: Request) -> Response:
        # Public entry point. The bare domain previously matched no route (a 404
        # the gateway could stall on); serve a real page that points anonymous
        # visitors at GitHub login and signed-in friends at the app.
        login = session_login(app, request) if hasattr(app.state, "settings") else None
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "web_session": login is not None,
                "friend_login": display_label(login, app.state.pin_store) if login else None,
                "pat_expires_in_days": _pat_expires_in_days(app, login) if login else None,
                # A signed-in visitor sees the nav here too, so the logout button
                # needs a token; minted only when there is a real web session.
                "csrf_token": ensure_csrf_token(request.session) if login is not None else None,
                "google_oauth_enabled": (
                    app.state.settings.google_oauth_enabled
                    if hasattr(app.state, "settings")
                    else False
                ),
            },
        )

    @app.get("/lang/{locale}")
    async def set_language(locale: str, request: Request) -> RedirectResponse:
        # Manual override (KTD5): set the lang cookie and 303-redirect back to a
        # validated relative target preserving its query. An unknown locale never
        # writes a junk cookie -- it just redirects. The cookie mirrors the
        # session-cookie policy (Secure off only on localhost) and is HttpOnly
        # because JS reads the locale from the window.I18N blob, never the cookie.
        target = safe_lang_return(request.query_params.get("next"))
        response = RedirectResponse(target, status_code=303)
        if locale not in SUPPORTED:
            return response
        secure = settings is not None and session_cookie_secure(settings)
        response.set_cookie(
            COOKIE_NAME,
            locale,
            max_age=LANG_COOKIE_MAX_AGE,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/browse")
    async def browse(
        request: Request,
        q: str = "",
        page: str = "1",
        authorization: str | None = Header(default=None),
    ) -> Response:
        # A browser visitor with neither a PAT header nor a web session is sent
        # through GitHub login (like /upload), not handed a JSON 401 it cannot
        # act on. A present-but-invalid PAT still surfaces as 401 below, and the
        # programmatic equivalent (/api/templates) keeps returning 401. The
        # settings guard mirrors the landing route so a settings-less test app
        # never dereferences app.state.web_allowlist here.
        if authorization is None and (
            not hasattr(app.state, "settings") or session_login(app, request) is None
        ):
            return RedirectResponse("/auth/login?next=/browse", status_code=303)
        friend = friend_from_request_or_header(app, request, authorization)
        app.state.find_limiter.hit(friend.principal)
        query = q.strip()
        all_rows = _template_rows(app, query)
        total_count = len(all_rows)
        total_pages = max(1, math.ceil(total_count / BROWSE_PAGE_SIZE))
        # Clamp out-of-range pages (page<1, page>last, ?page=999) to a real page
        # so a bad link still renders the nearest valid window instead of 404ing.
        # `page` is parsed by hand rather than typed `int` so a non-numeric
        # ?page=foo (bots, prefetch) falls back to page 1 instead of a raw 422.
        try:
            requested_page = int(page)
        except ValueError:
            requested_page = 1
        page_number = min(max(requested_page, 1), total_pages)
        start = (page_number - 1) * BROWSE_PAGE_SIZE
        locale = resolve_locale(request)
        page_rows = [
            _localized_template_row(row, locale)
            for row in all_rows[start : start + BROWSE_PAGE_SIZE]
        ]
        web_session = has_web_session(app, request)
        return templates.TemplateResponse(
            request,
            "browse.html",
            {
                "query": query,
                "templates": page_rows,
                "total_count": total_count,
                "pagination": _browse_pagination(query, page_number, total_pages),
                "friend_login": display_label(friend.principal, app.state.pin_store),
                "pat_expires_in_days": _pat_expires_in_days(app, friend.principal),
                "web_session": web_session,
                # Minted only for a real web session so the nav's logout button
                # has a token; PAT-only requests get no nav and no stray cookie.
                "csrf_token": ensure_csrf_token(request.session) if web_session else None,
            },
        )

    @app.get("/upload")
    async def upload_page(request: Request) -> Response:
        # Session-gated: an unauthenticated or non-allowlisted visitor is sent
        # through login and back to a validated /upload (KTD9/U6). Only an
        # allowlisted session reaches the page, which mints the CSRF token the
        # client reads from the meta tag (login clears the session, so the token
        # must be (re)minted here -- KTD4/U2).
        login = session_login(app, request)
        if login is None:
            return RedirectResponse("/auth/login?next=/upload", status_code=303)
        csrf_token = ensure_csrf_token(request.session)
        return templates.TemplateResponse(
            request,
            "upload.html",
            {
                "csrf_token": csrf_token,
                "friend_login": display_label(login, app.state.pin_store),
                "pat_expires_in_days": _pat_expires_in_days(app, login),
                "web_session": True,
                # Drives the egress toggle: when the feature is off, the page must
                # NOT render a checked Google-bound toggle (U6/KTD7).
                "reverse_image_enabled": app.state.settings.reverse_image_enabled,
            },
        )

    @app.get("/auth/login")
    async def auth_login(request: Request) -> Response:
        if not hasattr(app.state, "settings"):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
        # When Google sign-in is enabled, /auth/login is a provider chooser so a
        # protected-page redirect (/browse, /upload) can reach either provider;
        # an explicit ?provider=github starts the GitHub flow from the chooser.
        # GitHub-only deploys are unchanged (straight to GitHub).
        if (
            app.state.settings.google_oauth_enabled
            and request.query_params.get("provider") != "github"
        ):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next_target": safe_next(request.query_params.get("next")),
                    "web_session": False,
                    "friend_login": None,
                    "pat_expires_in_days": None,
                },
            )
        # As with the Google route, a speculative preload must not mint OAuth
        # state (a stale state would fail the callback's state check).
        if _is_prefetch(request):
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        request.session["oauth_state"] = state
        request.session["oauth_code_verifier"] = verifier
        request.session["post_login_next"] = safe_next(request.query_params.get("next"))
        challenge = _pkce_challenge(verifier)
        query = urlencode(
            {
                "client_id": app.state.settings.github_client_id,
                "redirect_uri": app.state.settings.github_redirect_uri,
                "scope": "read:user",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str, state: str) -> Response:
        if not secrets.compare_digest(str(request.session.get("oauth_state", "")), state):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "state", "reason": "mismatch"}])
        code_verifier = request.session.get("oauth_code_verifier")
        user = await app.state.github_oauth.fetch_user(
            code,
            str(code_verifier) if code_verifier is not None else None,
        )
        login = str(user.get("login", "")).strip()
        try:
            principal = normalize_principal(login) if login else ""
        except ValueError:
            principal = ""
        if not principal or not is_authorized(
            principal,
            allowlist=app.state.web_allowlist,
            pin_store=getattr(app.state, "pin_store", None),
        ):
            # Render the explanatory page without establishing a session; the
            # actor stays unauthenticated. A JSON 403 would be opaque to a
            # human arriving in a browser (KTD9).
            return templates.TemplateResponse(
                request,
                "restricted.html",
                {
                    "operator_github_login": app.state.settings.operator_github_login,
                    "pat_expires_in_days": None,
                    "friend_login": None,
                    "web_session": False,
                },
                status_code=403,
            )
        # Read and re-validate the return target BEFORE clearing the session:
        # session.clear() drops post_login_next, so reading it afterwards would
        # always yield the default and silently lose the requested page. The
        # subsequent clear() discards it, so a separate pop is unnecessary.
        # Re-validating defends against a tampered session value (KTD9).
        target = safe_next(request.session.get("post_login_next"))
        request.session.clear()
        # The session key is unchanged for back-compat, but it now carries the
        # namespaced principal (github:<login>); session_login normalizes either
        # form on read.
        request.session["github_login"] = principal
        return RedirectResponse(target, status_code=303)

    @app.get("/auth/google/login")
    async def auth_google_login(request: Request) -> Response:
        # Authlib generates and stores state, nonce, and the PKCE S256 challenge
        # in the session; the redirect URI is the canonical serving origin's
        # /auth/google/callback (validated at startup to match the public origin).
        if not hasattr(app.state, "settings"):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
        # A speculative preload (Arc/Chromium) must not start the flow: it would
        # rotate the state Authlib stores, so the clicked redirect's state would
        # no longer match and the callback would 500 with a CSRF state mismatch.
        if _is_prefetch(request):
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        request.session["post_login_next"] = safe_next(request.query_params.get("next"))
        oauth: GoogleOAuth = app.state.google_oauth
        return await oauth.authorize_redirect(request, app.state.settings.google_redirect_uri)

    @app.get("/auth/google/callback")
    async def auth_google_callback(request: Request) -> Response:
        if not hasattr(app.state, "settings"):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
        # authorize_access_token validates state, the id_token, and the nonce;
        # the authz-bearing claims are read from the validated id_token, never a
        # separate /userinfo fetch.
        oauth: GoogleOAuth = app.state.google_oauth
        identity = await oauth.resolve_identity(request)
        # First gate (always): strictly-boolean email_verified. Reject with no
        # session before any allowlist or pin work. The string forms Google has
        # historically emitted ("true"/"false") are rejected by the `is True`
        # check (R15). The domain is intentionally NOT restricted to @gmail.com:
        # a consumer Google account can carry a verified non-Gmail mailbox (e.g.
        # @icloud.com), and authorization keys on the FULL allowlisted email plus
        # the immutable sub-pin -- so the Workspace-domain-takeover risk that the
        # original Gmail-only gate guarded against does not apply (nobody can mint
        # an @icloud.com / @gmail.com address in a Workspace they control).
        email = (identity.email or "").strip().lower()
        local, _, domain = email.partition("@")
        if identity.email_verified is not True or not local or not domain or "@" in domain:
            return _google_restricted(app, request)
        principal = _resolve_google_principal(app, identity.subject, email)
        if principal is None:
            return _google_restricted(app, request)
        target = safe_next(request.session.get("post_login_next"))
        # Regenerate the session on success (OWASP session-fixation). authorize_
        # access_token already consumed Authlib's in-flight state/nonce keys, so
        # clearing here is safe.
        request.session.clear()
        request.session["github_login"] = principal
        return RedirectResponse(target, status_code=303)

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        require_csrf(request)
        request.session.clear()
        return JSONResponse(make_success({"logged_out": True}))

    @app.get("/api/mcp/tools")
    async def mcp_tools(authorization: str | None = Header(default=None)) -> JSONResponse:
        friend_from_header(app, authorization)
        return JSONResponse(make_success({"tools": sorted(tool_schemas())}))

    @app.get("/api/templates")
    async def list_templates(
        request: Request,
        q: str = "",
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = friend_from_request_or_header(app, request, authorization)
        app.state.find_limiter.hit(friend.principal)
        rows = _template_rows(app, q.strip())
        return JSONResponse(make_success({"templates": [_template_payload(row) for row in rows]}))

    @app.post("/api/templates/{template_id}/preview")
    async def preview_template(
        request: Request,
        template_id: str,
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend_from_request_or_header(app, request, authorization)
        slot_fills_raw = payload.get("slot_fills", [])
        if not isinstance(slot_fills_raw, list):
            raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "slot_fills", "reason": "list"}])
        slot_fills = [str(fill) for fill in slot_fills_raw]
        spec = _template_spec(app, template_id)
        rendered = preview_transient(spec, slot_fills)
        data_url = "data:image/png;base64," + base64.b64encode(rendered).decode()
        return JSONResponse(make_success({"template_id": template_id, "data_url": data_url}))

    @app.post("/api/mcp/find")
    async def mcp_find(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = friend_from_header(app, authorization)
        query = str(payload.get("query", "")).strip()
        if not query:
            raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "query", "reason": "required"}])
        filters_raw = payload.get("filters", {})
        filters = filters_raw if isinstance(filters_raw, dict) else {}
        return JSONResponse(AppMCPBackend(app).find(query, filters, friend.principal))

    @app.post("/api/mcp/generate")
    async def mcp_generate(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = require_write(friend_from_header(app, authorization))
        template_id = str(payload.get("template_id", "")).strip()
        slot_fills_raw = payload.get("slot_fills", [])
        if not template_id or not isinstance(slot_fills_raw, list):
            raise MemeMCPError(
                ErrorCode.INVALID_INPUT,
                [{"field": "template_id", "reason": "required"}],
            )
        slot_fills = [str(fill) for fill in slot_fills_raw]
        dry_run = bool(payload.get("dry_run", False))
        return JSONResponse(
            AppMCPBackend(app).generate(template_id, slot_fills, dry_run, friend.principal)
        )

    @app.post("/api/uploads/analyze")
    async def analyze_upload(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = require_write(friend_from_header(app, authorization))
        # PAT door defaults identify_online OFF: a programmatic caller does not
        # silently begin egressing images when the operator enables the feature
        # (KTD7). It must opt in explicitly per request.
        result = await analyze_image(
            content_base64=str(payload.get("content_base64", "")),
            declared_mime=str(payload.get("mime", "")),
            filename=str(payload.get("filename", "upload")),
            title_hint=payload.get("title_hint"),
            friend_login=friend.principal,
            deps=_upload_deps(app),
            identify_online=bool(payload.get("identify_online", False)),
            # PAT door has no UI locale: agents review the canonical English
            # proposal (R9 view defaults to "en"). The machine zh-TW counterpart
            # is still generated and drift-gated.
            locale="en",
        )
        return JSONResponse(
            make_success(
                {
                    "pending_upload_id": result.pending_upload_id,
                    "metadata": result.metadata,
                    "slot_definitions": result.slot_definitions,
                    "duplicate": {
                        "action": result.duplicate_action,
                        "template_id": result.duplicate_template_id,
                    },
                    "suspect_flags": result.suspect_flags,
                    "reverse_image_status": result.reverse_image_status,
                }
            )
        )

    @app.post("/api/uploads/{upload_id}/approve")
    async def approve_upload(
        upload_id: str,
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = require_write(friend_from_header(app, authorization))
        try:
            pending = app.state.pending_uploads.get(upload_id, friend.principal)
        except KeyError as exc:
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        metadata_raw = payload.get("metadata")
        slot_definitions_raw = payload.get("slot_definitions")
        result = approve_pending(
            pending=pending,
            actor=friend,
            metadata_overrides=metadata_raw if isinstance(metadata_raw, dict) else None,
            slot_overrides=slot_definitions_raw if isinstance(slot_definitions_raw, list) else None,
            ack_suspect=bool(payload.get("ack_suspect", False)),
            deps=_upload_deps(app),
        )
        return JSONResponse(
            make_success({"template_id": result.template_id, "slug": result.slug})
        )

    @app.get("/renders/{prefix}/{filename}")
    async def render_file(
        request: Request,
        prefix: str,
        filename: str,
        authorization: str | None = Header(default=None),
        exp: str | None = None,
        sig: str | None = None,
    ) -> FileResponse:
        # A valid signed URL (handed back by `generate`) is its own capability,
        # so an unauthenticated image client can load the PNG; absent or invalid
        # signatures fall back to session/PAT auth plus the receipt-ownership
        # check, which still gates ad-hoc fetches and the web detail page.
        signed_ok = False
        if exp and sig:
            signed_ok = verify_render_signature(
                f"{prefix}/{filename}",
                exp,
                sig,
                app.state.settings.session_secret.get_secret_value(),
                int(time.time()),
            )
        if not signed_ok:
            friend = friend_from_request_or_header(app, request, authorization)
            rendered_hash = f"{prefix}{Path(filename).stem}"
            if not app.state.receipts.exists_for_friend(rendered_hash, friend.principal):
                raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        root = Path(app.state.settings.image_store_fs_path).resolve()
        path = (root / prefix / filename).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        return FileResponse(path, media_type="image/png")

    @app.get("/templates/{template_id}")
    async def template_detail(
        request: Request,
        template_id: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        # The full-detail page for one template, reached by clicking a /browse
        # card. Auth-gated like /browse: an anonymous browser is bounced through
        # GitHub login with this page as the return target (detail URLs are
        # shareable), while a present-but-invalid PAT still surfaces as 401. The
        # settings guard mirrors /browse so a settings-less test app never
        # dereferences app.state here.
        if authorization is None and (
            not hasattr(app.state, "settings") or session_login(app, request) is None
        ):
            target = urlencode({"next": f"/templates/{template_id}"})
            return RedirectResponse(f"/auth/login?{target}", status_code=303)
        friend = friend_from_request_or_header(app, request, authorization)
        # Metered like /browse so a friend cannot enumerate every template ID
        # through the detail route while the gallery and /api/templates stay
        # rate-limited.
        app.state.find_limiter.hit(friend.principal)
        try:
            template = app.state.templates.get(template_id)
        except KeyError as exc:
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        template = _localized_template_row(template, resolve_locale(request))
        # Server-side https gate for the origin source link: Jinja autoescape does
        # NOT neutralize a javascript: href, so the safe URL is computed here and a
        # non-https one renders as plain text, never a live link (U6/KTD6). Stored
        # origin is already store-sanitized; this is defense-in-depth for any
        # legacy/seed row.
        origin = template.metadata.get("origin")
        origin = origin if isinstance(origin, dict) else None
        origin_source_url_safe = sanitize_url(str(origin.get("source_url", ""))) if origin else ""
        web_session = has_web_session(app, request)
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "template": template,
                "origin": origin,
                "origin_source_url_safe": origin_source_url_safe,
                "friend_login": display_label(friend.principal, app.state.pin_store),
                "pat_expires_in_days": _pat_expires_in_days(app, friend.principal),
                "web_session": web_session,
                # See browse: token only for a real web session (nav logout).
                "csrf_token": ensure_csrf_token(request.session) if web_session else None,
            },
        )

    @app.get("/templates/{template_id}/image")
    async def template_image(
        request: Request,
        template_id: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        # Serves a template's stored base image for the /browse gallery. Auth-gated
        # like /browse (friends only): the <img> tags on the browse page carry the
        # session cookie, so this resolves for the same actors who can see the page.
        # The content type is derived from the stored extension, not sniffed, so it
        # stays correct under the gateway's X-Content-Type-Options: nosniff header.
        friend_from_request_or_header(app, request, authorization)
        try:
            template = app.state.templates.get(template_id)
        except KeyError as exc:
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        ext = template.image_path.rsplit(".", 1)[-1].lower()
        media_type = _IMAGE_CONTENT_TYPES.get(ext)
        if media_type is None:
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        try:
            content = app.state.image_store.get(template.image_path)
        except FileNotFoundError as exc:
            # Row exists but its backing blob is gone (GC'd, or never landed); a
            # missing image is a 404, not a 500.
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    register_upload_routes(app, _upload_deps)
    if hasattr(app.state, "settings"):
        register_pat_routes(app)

    return app


def create_configured_app() -> FastAPI:
    return create_app(Settings())  # type: ignore[call-arg]


class AppMCPBackend:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def find(
        self,
        query: str,
        filters: dict[str, object] | None,
        actor: str,
    ) -> Envelope:
        self.app.state.find_limiter.hit(actor)
        outcomes = self.app.state.outcomes
        candidates = [
            project_candidate_english(candidate)
            for candidate in self.app.state.templates.search(
                query,
                filters or {},
                outcome_lookup=outcomes.recent_used_count,
            )
        ]
        return make_success({"candidates": candidates})

    def generate(
        self,
        template_id: str,
        slot_fills: list[str],
        dry_run: bool,
        actor: str,
    ) -> Envelope:
        self.app.state.generate_limiter.hit(actor)
        spec = _template_spec(self.app, template_id)
        if dry_run:
            _validate_slot_fills(spec, slot_fills)
            return make_success(_receipt(template_id, slot_fills, None, None))
        result = render_meme(
            spec, slot_fills, self.app.state.image_store, self.app.state.public_app_base_url
        )
        self.app.state.receipts.record(result.hash, template_id, actor)
        settings = self.app.state.settings
        # Sign the URL so the calling agent's image client can fetch the PNG
        # without a credential (it cannot replay the Bearer PAT/session); the
        # auth-gated route accepts the signature in lieu of auth.
        signed_url = sign_render_url(
            result.rendered_url,
            settings.session_secret.get_secret_value(),
            int(time.time()),
            settings.render_url_ttl_seconds,
        )
        return make_success(
            _receipt(template_id, slot_fills, signed_url, result.hash, result.alt_text)
        )

    def record_outcome(self, template_id: str, outcome: str, actor: str) -> Envelope:
        if outcome not in VALID_OUTCOMES:
            raise MemeMCPError(
                ErrorCode.INVALID_INPUT,
                [{"field": "outcome", "reason": "must be used|sent|dropped"}],
            )
        self.app.state.find_limiter.hit(actor)
        self.app.state.outcomes.record(template_id, actor, outcome)
        return make_success({"template_id": template_id, "outcome": outcome})


def _pat_expires_in_days(app: FastAPI, login: str) -> int | None:
    pat_store = getattr(app.state, "pat_store", None)
    if not isinstance(pat_store, SQLitePatStore):
        return None
    expires_at = expires_at_for_login(pat_store, login)
    # Fail closed on a naive timestamp (corrupt/legacy row): a naive minus aware
    # subtraction would raise and 500 the page, so suppress the banner instead.
    if expires_at is None or expires_at.tzinfo is None:
        return None
    delta = expires_at - datetime.now(UTC)
    return max(0, delta.days)


def _template_spec(app: FastAPI, template_id: str) -> TemplateSpec:
    try:
        template = app.state.templates.get(template_id)
    except KeyError as exc:
        raise MemeMCPError(
            ErrorCode.NOT_FOUND,
            [{"field": "template_id", "reason": "missing"}],
        ) from exc
    return TemplateSpec(
        template_id=template_id,
        image_bytes=app.state.image_store.get(template.image_path),
        slots=template.slot_definitions,
    )


def _duplicate_index(app: FastAPI) -> DuplicateIndex:
    index = DuplicateIndex()
    for template in app.state.templates.list_rows():
        index.add(template.template_id, template.exact_hash, template.perceptual_hash)
    return index


def _google_restricted(app: FastAPI, request: Request) -> Response:
    """403 restricted page for a rejected Google sign-in, no session established.

    Passes ``operator_github_login=None`` so the Google path never leaks the
    operator's GitHub handle (the provider-agnostic wording lands in U7).
    """
    return cast(
        Response,
        app.state.web_templates.TemplateResponse(
            request,
            "restricted.html",
            {
                "operator_github_login": None,
                "pat_expires_in_days": None,
                "friend_login": None,
                "web_session": False,
            },
            status_code=403,
        ),
    )


def _resolve_google_principal(app: FastAPI, sub: str, email: str) -> str | None:
    """Pin-first authorization for a verified Google sign-in (U6).

    Returning friend (a pin exists for ``sub``): authorize on the immutable sub
    via the shared predicate -- the pinned email (the operator's invite) is what
    is checked, so an email rename does not 403. First-timer (no pin): require the
    claim email to be currently allowlisted, then create the pin; the ``email
    UNIQUE`` constraint enforces first-sign-in-wins (a second sub for an
    already-pinned email is rejected). Returns the ``google:<sub>`` principal on
    success, else None. The email may be any verified Google mailbox, not only
    Gmail; dot/+ canonicalization still applies to Gmail addresses only.
    """
    if not sub:
        return None
    principal = f"google:{sub}"
    pin_store = app.state.pin_store
    allowlist = app.state.web_allowlist
    if pin_store.email_for_sub(sub) is not None:
        # Returning friend: resolve authorization by the sub-pin, NOT the live
        # claim email (the drift case).
        if is_authorized(principal, allowlist=allowlist, pin_store=pin_store):
            return principal
        return None
    # First sign-in: the verified claim email must be currently allowlisted. Pin
    # the CANONICAL mailbox so a later `allowlist remove`/`pin revoke` (R13) can
    # match it by the operator's invited address regardless of dot/+ variants.
    canonical = canonical_email(email)
    if not allowlist.is_allowlisted(f"google:{canonical}"):
        return None
    if not pin_store.create_pin(sub, canonical):
        # email UNIQUE rejected the pin: already bound to another sub.
        return None
    return principal


def _make_oauth_provider(
    app: FastAPI, settings: Settings, db_path: str | Path, public_base_url: str
) -> MemeAuthProvider | None:
    """Build the MCP OAuth authorization-server provider when enabled, else None.

    Returns None (resource-server-only PAT mode) unless OAUTH_AS_ENABLED. Imported
    lazily so the oauth package is only loaded when the AS is configured. The
    store shares the same local SQLite file as the PAT/pin stores; the provider's
    load_access_token also recognizes existing PATs so the mcp-remote path keeps
    working (R13).
    """
    if not settings.oauth_as_enabled:
        return None
    from meme_mcp.oauth.provider import MemeAuthProvider
    from meme_mcp.oauth.store import SQLiteOAuthStore

    store = SQLiteOAuthStore(
        db_path,
        token_pepper=_secret(settings.oauth_token_pepper),
        secret_enc_key=_secret(settings.oauth_secret_enc_key),
    )
    app.state.oauth_store = store
    provider = MemeAuthProvider(
        store=store,
        allowlist=app.state.allowlist,
        pat_store=app.state.pat_store,
        pat_pepper=app.state.pat_hash_pepper_value,
        resource_url=f"{public_base_url.rstrip('/')}/mcp",
        pin_store=app.state.pin_store,
    )
    app.state.oauth_provider = provider
    return provider


def _secret(value: SecretStr | None) -> str:
    """Unwrap a SecretStr (validated present at startup when the AS is enabled)."""
    return value.get_secret_value() if value is not None else ""


def _make_google_oauth(settings: Settings) -> object:
    """Build the Authlib Google client when enabled, else the unavailable sentinel.

    Imported lazily so the Authlib client tree is only loaded when Google sign-in
    is actually configured (mirrors the reverse-image client gating).
    """
    from meme_mcp.auth.google_oauth import GoogleOAuthUnavailable

    if not settings.google_oauth_enabled:
        return GoogleOAuthUnavailable()
    from meme_mcp.auth.google_oauth import GoogleOAuthClient

    return GoogleOAuthClient(
        client_id=settings.google_client_id or "",
        client_secret=(
            settings.google_client_secret.get_secret_value()
            if settings.google_client_secret
            else ""
        ),
    )


def _make_reverse_image_client(settings: Settings) -> object | None:
    """Build the Vision client when enabled, else None (the disabled sentinel).

    Imported lazily so the heavy google.cloud.vision dependency tree is only
    loaded when the feature is actually on.
    """
    if not settings.reverse_image_enabled or not settings.google_vision_credentials_path:
        return None
    from meme_mcp.reverse_image.client import GoogleVisionClient

    return GoogleVisionClient.from_credentials_path(settings.google_vision_credentials_path)


def _upload_deps(app: FastAPI) -> UploadServiceDeps:
    return UploadServiceDeps(
        upload_limiter=app.state.upload_limiter,
        vlm_client=app.state.vlm_client,
        image_store=app.state.image_store,
        pending_uploads=app.state.pending_uploads,
        templates=app.state.templates,
        duplicate_index=_duplicate_index(app),
        reverse_image_client=getattr(app.state, "reverse_image_client", None),
    )


def _browse_page_url(query: str, page: int) -> str:
    # Preserve the active search across page links and omit page=1 so the first
    # page keeps the canonical /browse(?q=...) URL. Returns a plain str (never a
    # Markup) so Jinja autoescapes the user-supplied `query` portion in href="".
    params: dict[str, str | int] = {}
    if query:
        params["q"] = query
    if page > 1:
        params["page"] = page
    suffix = urlencode(params)
    return f"/browse?{suffix}" if suffix else "/browse"


def _browse_pagination(
    query: str, page: int, total_pages: int
) -> dict[str, object] | None:
    # None when a single page fits everything -- the template then renders no
    # pager at all (the common case for a small library). Every page number is
    # listed (no windowing/ellipsis): the library is small enough that the link
    # row stays short; revisit if it grows past a few dozen pages.
    if total_pages <= 1:
        return None
    return {
        "page": page,
        "total_pages": total_pages,
        "prev_url": _browse_page_url(query, page - 1) if page > 1 else None,
        "next_url": _browse_page_url(query, page + 1) if page < total_pages else None,
        # `current` is derived in the template from page == p.number.
        "pages": [
            {"number": p, "url": _browse_page_url(query, p)}
            for p in range(1, total_pages + 1)
        ],
    }


def _template_rows(app: FastAPI, query: str) -> list[TemplateRow]:
    if not query:
        return cast(list[TemplateRow], app.state.templates.list_rows())
    ids = [candidate.template_id for candidate in app.state.templates.search(query)]
    all_rows = cast(list[TemplateRow], app.state.templates.list_rows())
    rows = {row.template_id: row for row in all_rows}
    return [rows[template_id] for template_id in ids if template_id in rows]


def _localized_template_row(row: TemplateRow, locale: str) -> TemplateRow:
    metadata = localized_metadata(row.metadata, locale)
    return replace(row, name=str(metadata.get("name") or row.name), metadata=metadata)


def _template_payload(row: TemplateRow) -> dict[str, object]:
    return {
        "template_id": row.template_id,
        "slug": row.slug,
        "name": row.name,
        "source": row.source,
        "metadata": row.metadata,
        "slot_definitions": row.slot_definitions,
    }


def _validate_slot_fills(spec: TemplateSpec, slot_fills: list[str]) -> None:
    if len(slot_fills) != len(spec.slots):
        raise MemeMCPError(
            ErrorCode.SLOT_MISMATCH,
            [{"field": "slot_fills", "reason": "must match template slot count"}],
        )


def _receipt(
    template_id: str,
    slot_fills: list[str],
    rendered_url: str | None,
    rendered_hash: str | None,
    alt_text: str | None = None,
) -> dict[str, object]:
    final_alt = alt_text or f"Meme {template_id}: " + " / ".join(slot_fills)
    return {
        "template_id": template_id,
        "slot_fills": slot_fills,
        "rendered_url": rendered_url,
        "hash": rendered_hash,
        "alt_text": final_alt,
        "markdown_snippet": f"![{final_alt}]({rendered_url})" if rendered_url else None,
    }


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _is_prefetch(request: Request) -> bool:
    """True for a speculative (prefetch/prerender) request.

    Chromium browsers (Arc especially) preload links and send
    ``Sec-Purpose: prefetch`` (or ``prefetch;prerender``); Firefox sends
    ``X-Moz: prefetch``; older/Safari variants use ``Purpose``/``X-Purpose``.
    Such a request must NOT start a sign-in flow: doing so rotates the OAuth
    ``state`` stored in the session, so the redirect the user actually clicks can
    carry a now-stale state and the callback fails with a CSRF state mismatch.
    The OAuth-init routes return a cache-busting no-op for these so the GET is
    safe to preload and only a real navigation initiates sign-in.
    """
    headers = request.headers
    return (
        "prefetch" in headers.get("sec-purpose", "").lower()
        or headers.get("purpose", "").lower() == "prefetch"
        or headers.get("x-purpose", "").lower() in {"prefetch", "preview"}
        or headers.get("x-moz", "").lower() == "prefetch"
    )
