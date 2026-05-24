from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from meme_mcp.auth.depends import Friend, require_pat
from meme_mcp.auth.pat import SQLitePatStore
from meme_mcp.config import Settings, validate_at_startup
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.envelope import Envelope, make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error
from meme_mcp.mcp.server import create_mcp_server, tool_schemas
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme
from meme_mcp.upload.dedupe import DuplicateIndex, check_duplicates
from meme_mcp.upload.strip import strip_and_reencode
from meme_mcp.upload.validation import compute_hashes, validate_upload
from meme_mcp.vlm.client import VLMClient


class RuntimeAllowlist(set[str]):
    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is not None:
        validate_at_startup(settings)
    Image.MAX_IMAGE_PIXELS = 40 * 1024 * 1024
    app = FastAPI(title="meme-mcp")
    if settings is not None:
        app.state.settings = settings
        db_path = _sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
        app.state.pat_store = SQLitePatStore(db_path)
        app.state.receipts = ReceiptStore(db_path)
        app.state.templates = SQLiteTemplateRepository(db_path)
        app.state.pending_uploads = PendingUploadStore(db_path)
        app.state.image_store = FilesystemImageStore(settings.image_store_fs_path)
        app.state.vlm_client = VLMClient(
            settings.vlm_model,
            api_key=settings.vlm_api_key.get_secret_value(),
            base_url=settings.vlm_base_url,
        )
        app.state.allowlist = RuntimeAllowlist()
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
    async def browse(authorization: str | None = Header(default=None)) -> JSONResponse:
        _friend_from_header(app, authorization)
        return JSONResponse(make_success({"templates": []}))

    @app.get("/api/mcp/tools")
    async def mcp_tools(authorization: str | None = Header(default=None)) -> JSONResponse:
        _friend_from_header(app, authorization)
        return JSONResponse(make_success({"tools": sorted(tool_schemas())}))

    @app.post("/api/mcp/find")
    async def mcp_find(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _friend_from_header(app, authorization)
        query = str(payload.get("query", "")).strip()
        if not query:
            raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "query", "reason": "required"}])
        filters_raw = payload.get("filters", {})
        filters = filters_raw if isinstance(filters_raw, dict) else {}
        candidates = [
            candidate.__dict__
            for candidate in app.state.templates.search(query, filters)
        ]
        return JSONResponse(make_success({"candidates": candidates}))

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
        if bool(payload.get("dry_run", False)):
            return JSONResponse(
                make_success({"template_id": template_id, "rendered_url": None, "hash": None})
            )
        try:
            template = app.state.templates.get(template_id)
        except KeyError as exc:
            raise MemeMCPError(
                ErrorCode.NOT_FOUND,
                [{"field": "template_id", "reason": "missing"}],
            ) from exc
        spec = TemplateSpec(
            template_id=template_id,
            image_bytes=app.state.image_store.get(template.image_path),
            slots=template.slot_definitions,
        )
        result = render_meme(spec, slot_fills, app.state.image_store)
        app.state.receipts.record(result.hash, template_id, friend.github_login)
        return JSONResponse(
            make_success(
                {
                    "template_id": template_id,
                    "rendered_url": result.rendered_url,
                    "hash": result.hash,
                }
            )
        )

    @app.post("/api/uploads/analyze")
    async def analyze_upload(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        friend = _friend_from_header(app, authorization)
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
        enrichment = app.state.vlm_client.enrich_template(
            sanitized,
            _optional_string(payload.get("title_hint")),
        )
        if enrichment.status == "timeout":
            raise MemeMCPError(ErrorCode.VLM_UNAVAILABLE, [{"field": "vlm", "reason": "timeout"}])
        if enrichment.status != "success" or enrichment.metadata is None:
            raise MemeMCPError(
                ErrorCode.VLM_OUTPUT_SUSPECT,
                [{"field": "vlm", "reason": enrichment.status}],
            )
        metadata = enrichment.metadata
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
            suspect_flags=enrichment.suspect_flags,
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
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = pending.metadata
        slot_definitions_raw = payload.get("slot_definitions")
        if isinstance(slot_definitions_raw, list):
            slot_definitions = slot_definitions_raw
        else:
            slot_definitions = pending.slot_definitions
        name = str(metadata.get("name") or pending.metadata.get("name") or "Uploaded Meme")
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
        prefix: str,
        filename: str,
        authorization: str | None = Header(default=None),
    ) -> FileResponse:
        friend = _friend_from_header(app, authorization)
        rendered_hash = f"{prefix}{Path(filename).stem}"
        if not app.state.receipts.exists_for_friend(rendered_hash, friend.github_login):
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        path = Path(app.state.settings.image_store_fs_path) / prefix / filename
        if not path.exists():
            raise MemeMCPError(ErrorCode.NOT_FOUND, [])
        return FileResponse(path, media_type="image/png")

    return app


class AppMCPBackend:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def find(self, query: str, filters: dict[str, object] | None = None) -> Envelope:
        candidates = [
            candidate.__dict__
            for candidate in self.app.state.templates.search(query, filters or {})
        ]
        return make_success({"candidates": candidates})

    def generate(
        self,
        template_id: str,
        slot_fills: list[str],
        dry_run: bool = False,
        actor: str | None = None,
    ) -> Envelope:
        if dry_run:
            return make_success({"template_id": template_id, "rendered_url": None, "hash": None})
        try:
            template = self.app.state.templates.get(template_id)
        except KeyError as exc:
            raise MemeMCPError(
                ErrorCode.NOT_FOUND,
                [{"field": "template_id", "reason": "missing"}],
            ) from exc
        spec = TemplateSpec(
            template_id=template_id,
            image_bytes=self.app.state.image_store.get(template.image_path),
            slots=template.slot_definitions,
        )
        result = render_meme(spec, slot_fills, self.app.state.image_store)
        self.app.state.receipts.record(result.hash, template_id, actor or "mcp")
        return make_success(
            {
                "template_id": template_id,
                "rendered_url": result.rendered_url,
                "hash": result.hash,
            }
        )


def _friend_from_header(app: FastAPI, authorization: str | None) -> Friend:
    if not hasattr(app.state, "settings"):
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
    return require_pat(
        authorization,
        app.state.pat_store,
        set(app.state.allowlist),
        app.state.pat_hash_pepper_value,
    )


def _sqlite_path(database_url: str, fallback: Path) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite+aiosqlite:///"):
        return Path(database_url.removeprefix("sqlite+aiosqlite:///"))
    return fallback


def _duplicate_index(app: FastAPI) -> DuplicateIndex:
    index = DuplicateIndex()
    for template in app.state.templates.list_rows():
        index.add(template.template_id, template.exact_hash, template.perceptual_hash)
    return index


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


def _template_id(name: str, exact_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "uploaded-meme"
    return f"{slug}-{exact_hash[:8]}"
