"""Shared session/PAT authentication helpers for HTTP front doors.

These live here (not in ``app.py``) so both the composition root and the web
upload routes import them top-level without a circular dependency: ``app.py``
imports ``register_upload_routes`` from ``web.upload_routes`` at module load, so
the web layer cannot import these helpers back from ``app.py``.

All helpers are pure functions of ``(app, request, authorization)`` reading only
``app.state``; none of them is web- or PAT-specific enough to live in just one
front door.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meme_mcp.auth.depends import Friend, require_pat
from meme_mcp.errors import ErrorCode, MemeMCPError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


def session_login(app: FastAPI, request: Request) -> str | None:
    """The allowlisted GitHub login carried by the session, or None.

    Reads only ``request.session`` (never an Authorization header), so it can
    never return a PAT-derived login. This is the single source of truth for
    "valid web session": the ``/upload`` page gate, the nav-link visibility, and
    the web-route authenticator all derive from it, so they cannot diverge.
    """
    login = request.session.get("github_login")
    if isinstance(login, str) and app.state.web_allowlist.is_allowlisted(login):
        return login
    return None


def has_web_session(app: FastAPI, request: Request) -> bool:
    """True only for an allowlisted GitHub session (not a PAT-authed request).

    The ``/upload`` nav link is gated on this rather than on ``friend_login``,
    because ``/browse`` also serves PAT-authenticated callers (who set
    ``friend_login`` but hold no web session and would be bounced from
    ``/upload``).
    """
    return session_login(app, request) is not None


def friend_from_header(app: FastAPI, authorization: str | None) -> Friend:
    if not hasattr(app.state, "settings"):
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [])
    return require_pat(
        authorization,
        app.state.pat_store,
        app.state.allowlist,
        app.state.pat_hash_pepper_value,
    )


def friend_from_request_or_header(
    app: FastAPI,
    request: Request,
    authorization: str | None,
) -> Friend:
    """Authenticate by PAT header when present, else by allowlisted session.

    Passing ``authorization=None`` makes this session-only, which is how the web
    upload routes ensure a PAT can never drive a browser endpoint (KTD3).
    """
    if authorization:
        return friend_from_header(app, authorization)
    login = session_login(app, request)
    if login is not None:
        return Friend(login)
    raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "session", "reason": "missing"}])
