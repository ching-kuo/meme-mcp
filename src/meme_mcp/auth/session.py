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

from meme_mcp.auth.authorization import is_authorized, normalize_principal
from meme_mcp.auth.depends import Friend, require_pat
from meme_mcp.errors import ErrorCode, MemeMCPError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


def session_login(app: FastAPI, request: Request) -> str | None:
    """The allowlisted principal carried by the session, or None.

    Returns the provider-namespaced principal (``github:<login>`` or
    ``google:<sub>``), normalizing a legacy bare value so an in-flight session
    cookie predating the namespace change still authenticates. Reads only
    ``request.session`` (never an Authorization header), so it can never return a
    PAT-derived identity. This is the single source of truth for "valid web
    session": the ``/upload`` page gate, the nav-link visibility, and the
    web-route authenticator all derive from it, so they cannot diverge.
    """
    raw = request.session.get("github_login")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        principal = normalize_principal(raw)
    except ValueError:
        return None
    if is_authorized(
        principal,
        allowlist=app.state.web_allowlist,
        pin_store=getattr(app.state, "pin_store", None),
    ):
        return principal
    return None


def has_web_session(app: FastAPI, request: Request) -> bool:
    """True only for an allowlisted browser session (not a PAT-authed request).

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
        getattr(app.state, "pin_store", None),
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
