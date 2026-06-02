from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from meme_mcp.auth.authorization import display_login
from meme_mcp.auth.depends import Friend
from meme_mcp.auth.pat import PatStatus
from meme_mcp.auth.pat_web import WEB_TTL_DAYS, regenerate_web, revoke_web
from meme_mcp.auth.session import friend_from_request_or_header, session_login
from meme_mcp.envelope import make_success
from meme_mcp.web.csrf import ensure_csrf_token, require_csrf

if TYPE_CHECKING:
    from fastapi import FastAPI


def _session_friend(app: FastAPI, request: Request) -> Friend:
    """Authenticate an allowlisted GitHub session, rejecting any PAT.

    Passing ``authorization=None`` keeps these routes session-only so a PAT
    header can never drive them (R1/KTD3), mirroring ``upload_routes``.
    """
    return friend_from_request_or_header(app, request, None)


def register_pat_routes(app: FastAPI) -> None:
    templates = app.state.web_templates

    @app.get("/account")
    async def account_page(request: Request) -> Response:
        login = session_login(app, request)
        if login is None:
            return RedirectResponse("/auth/login?next=/account", status_code=303)
        csrf_token = ensure_csrf_token(request.session)
        status = app.state.pat_store.current_status(login)
        return cast(
            Response,
            templates.TemplateResponse(
                request,
                "account.html",
                {
                    "csrf_token": csrf_token,
                    "friend_login": display_login(login),
                    "pat_status": _status_payload(status),
                    "pat_expires_in_days": _status_expires_in_days(status),
                    "web_session": True,
                    "web_ttl_days": WEB_TTL_DAYS,
                },
            ),
        )

    @app.post("/account/token")
    async def issue_token(request: Request, payload: dict[str, object]) -> JSONResponse:
        friend = _session_friend(app, request)
        require_csrf(request)
        app.state.pat_admin_limiter.hit(friend.principal)
        plaintext = regenerate_web(
            store=app.state.pat_store,
            friend_login=friend.principal,
            pepper=app.state.pat_hash_pepper_value,
            capability=payload.get("scope"),
            ttl_days=payload.get("ttl_days"),
            audit_sink=app.state.audit_sink,
        )
        status = app.state.pat_store.current_status(friend.principal)
        # The one-time plaintext rides in this body; forbid any browser/proxy
        # caching so the secret cannot be replayed from a cache (R9).
        return JSONResponse(
            make_success({"token": plaintext, "token_status": _status_payload(status)}),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/account/token/revoke")
    async def revoke_token(request: Request) -> JSONResponse:
        friend = _session_friend(app, request)
        require_csrf(request)
        app.state.pat_admin_limiter.hit(friend.principal)
        revoked = revoke_web(
            store=app.state.pat_store,
            friend_login=friend.principal,
            audit_sink=app.state.audit_sink,
        )
        status = app.state.pat_store.current_status(friend.principal)
        return JSONResponse(
            make_success({"revoked": revoked, "token_status": _status_payload(status)})
        )


def _status_payload(status: PatStatus) -> dict[str, str | None]:
    return {
        "state": status.state,
        "scope": status.capability,
        "expires_at": _iso(status.expires_at),
        "last_used_at": _iso(status.last_used_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _status_expires_in_days(status: PatStatus) -> int | None:
    if status.state != "active" or status.expires_at is None:
        return None
    delta = status.expires_at - datetime.now(UTC)
    return max(0, delta.days)
