from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from meme_mcp.auth.depends import Friend, require_pat
from meme_mcp.auth.pat import SQLitePatStore
from meme_mcp.config import Settings, validate_at_startup
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.envelope import make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error
from meme_mcp.mcp.server import tool_schemas
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme


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
        app.state.image_store = FilesystemImageStore(settings.image_store_fs_path)
        app.state.allowlist = RuntimeAllowlist()
        app.state.pat_hash_pepper_value = settings.pat_hash_pepper.get_secret_value()

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

    @app.get("/mcp/tools")
    async def mcp_tools(authorization: str | None = Header(default=None)) -> JSONResponse:
        _friend_from_header(app, authorization)
        return JSONResponse(make_success({"tools": sorted(tool_schemas())}))

    @app.post("/mcp/find")
    async def mcp_find(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _friend_from_header(app, authorization)
        query = str(payload.get("query", "")).strip()
        if not query:
            raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "query", "reason": "required"}])
        return JSONResponse(make_success({"candidates": []}))

    @app.post("/mcp/generate")
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
        spec = TemplateSpec(
            template_id=template_id,
            image_bytes=_blank_template_png(),
            slots=[{"position": "top"} for _ in slot_fills],
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


def _blank_template_png() -> bytes:
    from io import BytesIO

    image = Image.new("RGB", (320, 180), "navy")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
