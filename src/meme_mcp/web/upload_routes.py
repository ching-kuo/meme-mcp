"""Session-authenticated JSON upload endpoints for the browser front door.

These routes mirror the PAT-authenticated ``/api/uploads/*`` contract field for
field, but authenticate with the GitHub session only (never a PAT) and require
a CSRF header on every request (KTD3/KTD4). All pipeline behavior is delegated
to :mod:`meme_mcp.upload.service` so the two front doors cannot drift (KTD2).

Owner-scoping is enforced here, not in the shared service:

* approve fetches the pending row owner-scoped via ``pending_uploads.get(id,
  login)`` and raises an opaque ``NOT_FOUND`` on absence or owner mismatch, so
  the endpoint never reveals whether the id exists for a different friend (AE6).
* discard calls ``pending_uploads.delete_owned(id, login)`` (U4), which deletes
  only the owner's pending row and NEVER the blob -- a synchronous blob delete
  would race the ``analyze`` put-before-row-create window (KTD8/F02). All blob
  reclamation is the grace-windowed ``gc-uploads`` sweep. Discard returns
  success opaquely whether or not a row was deleted.

The pre-buffer body-size guard for ``/upload/analyze`` lives in app-level ASGI
middleware (KTD11/F01), not here: by the time a handler holds a parsed payload
Starlette has already buffered the body.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

from meme_mcp.auth.depends import Friend
from meme_mcp.auth.session import friend_from_request_or_header
from meme_mcp.envelope import make_success
from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.upload.service import analyze_image, approve_pending
from meme_mcp.web.csrf import require_csrf

if TYPE_CHECKING:
    from fastapi import FastAPI

    from meme_mcp.upload.service import UploadServiceDeps


def _session_friend(app: FastAPI, request: Request) -> Friend:
    """Authenticate an allowlisted GitHub session, rejecting any PAT.

    Calls friend_from_request_or_header with no Authorization header so a PAT can
    never authenticate a web route -- the helper only consults the session when
    authorization is falsy (KTD3).
    """
    return friend_from_request_or_header(app, request, None)


def register_upload_routes(
    app: FastAPI,
    upload_deps: Callable[[FastAPI], UploadServiceDeps],
) -> None:
    """Register the session-authed analyze/approve/discard endpoints on ``app``.

    ``upload_deps`` is the app's ``_upload_deps`` factory (reused so the shared
    service collaborators are identical to the PAT front door).
    """

    @app.post("/upload/analyze")
    async def analyze_web(request: Request, payload: dict[str, object]) -> JSONResponse:
        friend = _session_friend(app, request)
        require_csrf(request)
        # Web door defaults identify_online ON (KTD7): most friend uploads are
        # newer memes whose meaning is hard to recover from the image alone, so
        # enrichment-first is the right default for the interactive surface. The
        # friend can uncheck the toggle (U6); a configured-off feature still
        # yields a no-egress "unavailable" status in the service.
        result = await analyze_image(
            content_base64=str(payload.get("content_base64", "")),
            declared_mime=str(payload.get("mime", "")),
            filename=str(payload.get("filename", "upload")),
            title_hint=payload.get("title_hint"),
            friend_login=friend.principal,
            deps=upload_deps(app),
            identify_online=bool(payload.get("identify_online", True)),
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

    @app.post("/upload/approve/{upload_id}")
    async def approve_web(
        request: Request,
        upload_id: str,
        payload: dict[str, object],
    ) -> JSONResponse:
        friend = _session_friend(app, request)
        require_csrf(request)
        # Owner-scoped fetch: KeyError on absence OR owner mismatch maps to an
        # opaque NOT_FOUND so the endpoint never reveals another friend's id.
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
            deps=upload_deps(app),
            # The web review form is the trusted human-review surface: approving
            # here confirms the origin and promotes it to status="high" (KTD9).
            origin_reviewed=True,
        )
        return JSONResponse(make_success({"template_id": result.template_id, "slug": result.slug}))

    @app.post("/upload/discard/{upload_id}")
    async def discard_web(request: Request, upload_id: str) -> JSONResponse:
        friend = _session_friend(app, request)
        require_csrf(request)
        # Owner-scoped row delete only; the blob is left for the grace-windowed
        # gc-uploads sweep (KTD8). Success is returned opaquely whether or not a
        # row matched, so a friend cannot probe another friend's ids.
        app.state.pending_uploads.delete_owned(upload_id, friend.principal)
        return JSONResponse(make_success({"discarded": True}))
