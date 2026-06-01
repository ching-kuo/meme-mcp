from __future__ import annotations

import base64
import hashlib
import secrets
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meme_mcp.auth.allowlist import FileAllowlist
from meme_mcp.auth.depends import require_write
from meme_mcp.auth.pat import SQLitePatStore, expires_at_for_login
from meme_mcp.auth.session import (
    friend_from_header,
    friend_from_request_or_header,
    has_web_session,
    session_login,
)
from meme_mcp.config import Settings, validate_at_startup
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.migrations import run_migrations
from meme_mcp.db.outcomes import VALID_OUTCOMES, OutcomeEventStore
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateRow
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.db.vectors import EmbeddingMetaStore
from meme_mcp.embeddings.client import validate_embedding_model
from meme_mcp.envelope import Envelope, make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.mcp.server import create_mcp_server, tool_schemas
from meme_mcp.rendering.image_store import make_image_store_from_settings
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme
from meme_mcp.upload.dedupe import DuplicateIndex
from meme_mcp.upload.service import UploadServiceDeps, analyze_image, approve_pending
from meme_mcp.vlm.client import VLMClient
from meme_mcp.vlm.sanitize import sanitize_url
from meme_mcp.web.csrf import ensure_csrf_token, require_csrf, safe_next
from meme_mcp.web.upload_routes import register_upload_routes

# Pre-buffer body-size cap for the analyze endpoints. A 10 MB image base64
# encodes to ~13.3 MB; 14 MB leaves headroom for the surrounding JSON envelope
# while still rejecting clearly oversized bodies before they are buffered into
# memory (KTD11). validate_upload's 10 MB content check stays authoritative.
MAX_ANALYZE_BODY_BYTES = 14 * 1024 * 1024
_ANALYZE_PATHS = frozenset({"/api/uploads/analyze", "/upload/analyze"})


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
    app = FastAPI(title="meme-mcp")
    web_dir = Path(__file__).parent / "web"
    templates = Jinja2Templates(directory=web_dir / "templates")
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")
    if settings is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret.get_secret_value(),
            same_site="lax",
            https_only=not settings.github_redirect_uri.startswith("http://localhost"),
        )
        # Added after SessionMiddleware so it is the OUTERMOST layer
        # (add_middleware prepends): it rejects oversized analyze bodies on
        # Content-Length before the session layer or any route reads the body
        # (KTD11/F01).
        app.add_middleware(BodySizeGuardMiddleware, max_bytes=MAX_ANALYZE_BODY_BYTES)
        app.state.settings = settings
        run_migrations(settings)
        db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
        app.state.pat_store = SQLitePatStore(db_path)
        app.state.receipts = ReceiptStore(db_path)
        app.state.outcomes = OutcomeEventStore(db_path)
        app.state.templates = SQLiteTemplateRepository(db_path)
        app.state.pending_uploads = PendingUploadStore(db_path)
        app.state.embedding_meta = EmbeddingMetaStore(db_path)
        validate_embedding_model(app.state.embedding_meta, settings.embedding_model)
        app.state.image_store = make_image_store_from_settings(settings)
        app.state.web_allowlist = FileAllowlist(settings.github_allowlist_path)
        app.state.allowlist = app.state.web_allowlist
        app.state.find_limiter = WindowedRateLimiter(settings.rate_find_per_min, 60)
        app.state.generate_limiter = WindowedRateLimiter(settings.rate_generate_per_min, 60)
        app.state.upload_limiter = WindowedRateLimiter(settings.rate_upload_per_hour, 60 * 60)
        app.state.github_oauth = GitHubOAuthHTTPClient(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret.get_secret_value(),
            redirect_uri=settings.github_redirect_uri,
        )
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
        app.state.mcp_server = create_mcp_server(
            app.state.pat_store,
            app.state.allowlist,
            app.state.pat_hash_pepper_value,
            AppMCPBackend(app),
        )
        app.mount("/mcp", app.state.mcp_server.streamable_http_app())

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
                "friend_login": login,
                "pat_expires_in_days": _pat_expires_in_days(app, login) if login else None,
            },
        )

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
        app.state.find_limiter.hit(friend.github_login)
        query = q.strip()
        template_rows = _template_rows(app, query)
        return templates.TemplateResponse(
            request,
            "browse.html",
            {
                "query": query,
                "templates": template_rows,
                "friend_login": friend.github_login,
                "pat_expires_in_days": _pat_expires_in_days(app, friend.github_login),
                "web_session": has_web_session(app, request),
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
                "friend_login": login,
                "pat_expires_in_days": _pat_expires_in_days(app, login),
                "web_session": True,
                # Drives the egress toggle: when the feature is off, the page must
                # NOT render a checked Google-bound toggle (U6/KTD7).
                "reverse_image_enabled": app.state.settings.reverse_image_enabled,
            },
        )

    @app.get("/auth/login")
    async def auth_login(request: Request) -> RedirectResponse:
        if not hasattr(app.state, "settings"):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
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
        if not login or not app.state.web_allowlist.is_allowlisted(login):
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
        request.session["github_login"] = login
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
        app.state.find_limiter.hit(friend.github_login)
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
        return JSONResponse(AppMCPBackend(app).find(query, filters, friend.github_login))

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
            AppMCPBackend(app).generate(template_id, slot_fills, dry_run, friend.github_login)
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
            friend_login=friend.github_login,
            deps=_upload_deps(app),
            identify_online=bool(payload.get("identify_online", False)),
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
            pending = app.state.pending_uploads.get(upload_id, friend.github_login)
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
    ) -> FileResponse:
        friend = friend_from_request_or_header(app, request, authorization)
        rendered_hash = f"{prefix}{Path(filename).stem}"
        if not app.state.receipts.exists_for_friend(rendered_hash, friend.github_login):
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
        app.state.find_limiter.hit(friend.github_login)
        try:
            template = app.state.templates.get(template_id)
        except KeyError as exc:
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        # Server-side https gate for the origin source link: Jinja autoescape does
        # NOT neutralize a javascript: href, so the safe URL is computed here and a
        # non-https one renders as plain text, never a live link (U6/KTD6). Stored
        # origin is already store-sanitized; this is defense-in-depth for any
        # legacy/seed row.
        origin = template.metadata.get("origin")
        origin = origin if isinstance(origin, dict) else None
        origin_source_url_safe = sanitize_url(str(origin.get("source_url", ""))) if origin else ""
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "template": template,
                "origin": origin,
                "origin_source_url_safe": origin_source_url_safe,
                "friend_login": friend.github_login,
                "pat_expires_in_days": _pat_expires_in_days(app, friend.github_login),
                "web_session": has_web_session(app, request),
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
            candidate.__dict__
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
        result = render_meme(spec, slot_fills, self.app.state.image_store)
        self.app.state.receipts.record(result.hash, template_id, actor)
        return make_success(
            _receipt(template_id, slot_fills, result.rendered_url, result.hash, result.alt_text)
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
    if expires_at is None:
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


def _template_rows(app: FastAPI, query: str) -> list[TemplateRow]:
    if not query:
        return cast(list[TemplateRow], app.state.templates.list_rows())
    ids = [candidate.template_id for candidate in app.state.templates.search(query)]
    all_rows = cast(list[TemplateRow], app.state.templates.list_rows())
    rows = {row.template_id: row for row in all_rows}
    return [rows[template_id] for template_id in ids if template_id in rows]


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
