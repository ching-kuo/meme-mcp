from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
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

from meme_mcp.auth.allowlist import FileAllowlist
from meme_mcp.auth.depends import Friend, require_pat
from meme_mcp.auth.pat import SQLitePatStore, expires_at_for_login
from meme_mcp.config import Settings, validate_at_startup
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate, TemplateRow
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.db.vectors import EmbeddingMetaStore
from meme_mcp.embeddings.client import validate_embedding_model
from meme_mcp.envelope import Envelope, make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.mcp.server import create_mcp_server, tool_schemas
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, preview_transient, render_meme
from meme_mcp.upload.dedupe import DuplicateIndex, check_duplicates
from meme_mcp.upload.strip import strip_and_reencode
from meme_mcp.upload.validation import compute_hashes, validate_upload
from meme_mcp.vlm.client import VLMClient
from meme_mcp.vlm.sanitize import flag_anomalies, hard_sanitize_metadata


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
        app.state.settings = settings
        db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
        app.state.pat_store = SQLitePatStore(db_path)
        app.state.receipts = ReceiptStore(db_path)
        app.state.templates = SQLiteTemplateRepository(db_path)
        app.state.pending_uploads = PendingUploadStore(db_path)
        app.state.embedding_meta = EmbeddingMetaStore(db_path)
        validate_embedding_model(app.state.embedding_meta, settings.embedding_model)
        app.state.image_store = FilesystemImageStore(settings.image_store_fs_path)
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
        friend = _friend_from_request_or_header(app, request, authorization)
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
    async def auth_callback(request: Request, code: str, state: str) -> JSONResponse:
        if not secrets.compare_digest(str(request.session.get("oauth_state", "")), state):
            raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "state", "reason": "mismatch"}])
        code_verifier = request.session.get("oauth_code_verifier")
        user = await app.state.github_oauth.fetch_user(
            code,
            str(code_verifier) if code_verifier is not None else None,
        )
        login = str(user.get("login", "")).strip()
        if not login or not app.state.web_allowlist.is_allowlisted(login):
            raise MemeMCPError(
                ErrorCode.FORBIDDEN_NOT_ALLOWLISTED,
                [{"field": "github_login", "reason": "not_allowlisted"}],
            )
        request.session.clear()
        request.session["github_login"] = login
        return JSONResponse(make_success({"github_login": login}))

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        request.session.clear()
        return JSONResponse(make_success({"logged_out": True}))

    @app.get("/api/mcp/tools")
    async def mcp_tools(authorization: str | None = Header(default=None)) -> JSONResponse:
        _friend_from_header(app, authorization)
        return JSONResponse(make_success({"tools": sorted(tool_schemas())}))

    @app.get("/api/templates")
    async def list_templates(
        request: Request,
        q: str = "",
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = _friend_from_request_or_header(app, request, authorization)
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
        _friend_from_request_or_header(app, request, authorization)
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
        friend = _friend_from_header(app, authorization)
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
        friend = _friend_from_header(app, authorization)
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
        friend = _friend_from_header(app, authorization)
        app.state.upload_limiter.hit(friend.github_login)
        filename = str(payload.get("filename", "upload"))
        mime = str(payload.get("mime", ""))
        try:
            content = base64.b64decode(str(payload.get("content_base64", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MemeMCPError(
                ErrorCode.INVALID_INPUT,
                [{"field": "content_base64", "reason": "base64"}],
            ) from exc
        validated = validate_upload(content, mime, filename)
        sanitized = strip_and_reencode(content, validated.mime)
        exact_hash, perceptual_hash = compute_hashes(sanitized)
        duplicate = check_duplicates(_duplicate_index(app), exact_hash, perceptual_hash)
        if duplicate.action == "block":
            raise MemeMCPError(
                ErrorCode.DUPLICATE_TEMPLATE,
                [
                    {
                        "field": "file",
                        "reason": f"duplicate:{duplicate.template_id}",
                    }
                ],
            )
        enrichment = await asyncio.to_thread(
            app.state.vlm_client.enrich_template,
            sanitized,
            _optional_string(payload.get("title_hint")),
        )
        if enrichment.status == "success" and enrichment.metadata is not None:
            metadata = enrichment.metadata
            suspect_flags = enrichment.suspect_flags
        else:
            metadata = _blank_upload_metadata(_optional_string(payload.get("title_hint")))
            suspect_flags = [f"vlm_{enrichment.status}"]
        slot_definitions = _slot_definitions(metadata)
        image_path = app.state.image_store.put(sanitized, _extension_for_mime(validated.mime))
        pending = app.state.pending_uploads.create(
            friend_login=friend.github_login,
            image_path=image_path,
            metadata=metadata,
            slot_definitions=slot_definitions,
            exact_hash=exact_hash,
            perceptual_hash=perceptual_hash,
            duplicate_action=duplicate.action,
            duplicate_template_id=duplicate.template_id,
            suspect_flags=suspect_flags,
        )
        return JSONResponse(
            make_success(
                {
                    "pending_upload_id": pending.upload_id,
                    "metadata": pending.metadata,
                    "slot_definitions": pending.slot_definitions,
                    "duplicate": {
                        "action": pending.duplicate_action,
                        "template_id": pending.duplicate_template_id,
                    },
                    "suspect_flags": pending.suspect_flags,
                }
            )
        )

    @app.post("/api/uploads/{upload_id}/approve")
    async def approve_upload(
        upload_id: str,
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = _friend_from_header(app, authorization)
        try:
            pending = app.state.pending_uploads.get(upload_id, friend.github_login)
        except KeyError as exc:
            raise MemeMCPError(ErrorCode.NOT_FOUND, []) from exc
        metadata_raw = payload.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else pending.metadata
        metadata = _validated_metadata(
            metadata,
            pending.suspect_flags,
            bool(payload.get("ack_suspect", False)),
        )
        slot_definitions_raw = payload.get("slot_definitions")
        if isinstance(slot_definitions_raw, list):
            slot_definitions = slot_definitions_raw
        else:
            slot_definitions = pending.slot_definitions
        name = str(metadata["name"])
        template_id = _template_id(name, pending.exact_hash)
        app.state.templates.upsert(
            TemplateCreate(
                template_id=template_id,
                slug=template_id,
                name=name,
                source="friend",
                metadata=metadata,
                slot_definitions=[slot for slot in slot_definitions if isinstance(slot, dict)],
                image_path=pending.image_path,
                perceptual_hash=pending.perceptual_hash,
                exact_hash=pending.exact_hash,
            )
        )
        app.state.pending_uploads.delete(upload_id)
        return JSONResponse(make_success({"template_id": template_id, "slug": template_id}))

    @app.get("/renders/{prefix}/{filename}")
    async def render_file(
        request: Request,
        prefix: str,
        filename: str,
        authorization: str | None = Header(default=None),
    ) -> FileResponse:
        friend = _friend_from_request_or_header(app, request, authorization)
        rendered_hash = f"{prefix}{Path(filename).stem}"
        if not app.state.receipts.exists_for_friend(rendered_hash, friend.github_login):
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        root = Path(app.state.settings.image_store_fs_path).resolve()
        path = (root / prefix / filename).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        return FileResponse(path, media_type="image/png")

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
        candidates = [
            candidate.__dict__
            for candidate in self.app.state.templates.search(query, filters or {})
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


def _pat_expires_in_days(app: FastAPI, login: str) -> int | None:
    pat_store = getattr(app.state, "pat_store", None)
    if not isinstance(pat_store, SQLitePatStore):
        return None
    expires_at = expires_at_for_login(pat_store, login)
    if expires_at is None:
        return None
    delta = expires_at - datetime.now(UTC)
    return max(0, delta.days)


def _friend_from_header(app: FastAPI, authorization: str | None) -> Friend:
    if not hasattr(app.state, "settings"):
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
    return require_pat(
        authorization,
        app.state.pat_store,
        app.state.allowlist,
        app.state.pat_hash_pepper_value,
    )


def _friend_from_request_or_header(
    app: FastAPI,
    request: Request,
    authorization: str | None,
) -> Friend:
    if authorization:
        return _friend_from_header(app, authorization)
    login = request.session.get("github_login")
    if isinstance(login, str) and app.state.web_allowlist.is_allowlisted(login):
        return Friend(login)
    raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "session", "reason": "missing"}])


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


def _extension_for_mime(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _slot_definitions(metadata: dict[str, object]) -> list[dict[str, object]]:
    slots = metadata.get("slot_definitions")
    if isinstance(slots, list):
        typed_slots = [slot for slot in slots if isinstance(slot, dict)]
        if typed_slots:
            return typed_slots
    return [{"name": "top", "position": "top"}, {"name": "bottom", "position": "bottom"}]


def _blank_upload_metadata(title_hint: str | None) -> dict[str, object]:
    return {
        "name": title_hint or "Uploaded Meme",
        "description": "",
        "emotion": "",
        "usage_context": "",
        "tags": [],
        "format": "static",
        "slot_definitions": [{"name": "top", "position": "top"}],
    }


def _validated_metadata(
    metadata: dict[str, object],
    suspect_flags: list[str],
    ack_suspect: bool,
) -> dict[str, object]:
    raw_flags = flag_anomalies(metadata)
    cleaned = hard_sanitize_metadata(metadata)
    flags = sorted(set(suspect_flags) | set(raw_flags) | set(flag_anomalies(cleaned)))
    if flags and not ack_suspect:
        raise MemeMCPError(
            ErrorCode.VLM_OUTPUT_SUSPECT,
            [{"field": "metadata", "reason": ",".join(flags)}],
        )
    required = ["name", "description", "emotion", "usage_context", "tags", "format"]
    missing = [key for key in required if key not in cleaned]
    if missing:
        raise MemeMCPError(
            ErrorCode.INVALID_INPUT,
            [{"field": "metadata", "reason": f"missing:{','.join(missing)}"}],
        )
    if cleaned.get("format") != "static":
        raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "format", "reason": "static"}])
    if not isinstance(cleaned.get("tags"), list):
        raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "tags", "reason": "list"}])
    cleaned["slot_definitions"] = _slot_definitions(cleaned)
    return cleaned


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


def _template_id(name: str, exact_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "uploaded-meme"
    return f"{slug}-{exact_hash[:8]}"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
