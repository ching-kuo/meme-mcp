"""Parent-app authorize bridge + per-client consent screen (U4).

The SDK's ``provider.authorize()`` hook has no ``Request`` and cannot touch the
session, so it parks the validated authorization params in the store under a
single-use nonce and redirects here. This module owns the user-facing half:
ensure the friend is signed in (reusing the existing GitHub/Google login), show a
per-client consent screen, enforce the friend allowlist **at issuance**, then mint
the authorization code -- closing the confused-deputy hole (a not-yet-approved
client cannot ride an existing upstream session; R8).

These routes live on the parent app (alongside ``/auth/*``) for origin-root URLs
and a real ``Request`` + session. Consent precedes code issuance regardless of any
upstream IdP consent cookie, and the live ``is_authorized`` check runs on every
issuance so a removed friend is denied even if previously approved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from meme_mcp.auth.authorization import display_label, is_authorized, normalize_principal
from meme_mcp.oauth.provider import CONSENT_PATH, MemeAuthProvider
from meme_mcp.oauth.store import PendingRequest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates


def register_consent_routes(
    app: FastAPI, *, provider: MemeAuthProvider, templates: Jinja2Templates
) -> None:
    """Register GET/POST ``/oauth/consent/{rid}`` on the parent app."""

    from meme_mcp.web.csrf import require_csrf_form

    def _login_redirect(rid: str) -> RedirectResponse:
        target = quote(f"{CONSENT_PATH}/{rid}")
        return RedirectResponse(f"/auth/login?next={target}", status_code=303)

    @app.get(CONSENT_PATH + "/{rid}")
    async def oauth_consent_get(request: Request, rid: str) -> Response:
        principal = _session_principal(request)
        if principal is None:
            return _login_redirect(rid)
        pending = provider.store.load_pending_request(rid)
        if pending is None:
            return _expired(request)
        if provider.store.has_approval(principal, pending.client_id):
            # Previously approved: skip the screen but still enforce the live
            # allowlist (a de-allowlisted friend is denied here).
            return _issue_or_deny(request, provider, principal, pending, rid)
        return _render_consent(request, templates, principal, pending, rid)

    @app.post(CONSENT_PATH + "/{rid}")
    async def oauth_consent_post(request: Request, rid: str) -> Response:
        # CSRF is validated before any consent logic runs.
        await require_csrf_form(request)
        principal = _session_principal(request)
        if principal is None:
            return _login_redirect(rid)
        pending = provider.store.load_pending_request(rid)
        if pending is None:
            return _expired(request)
        form = await request.form()
        if form.get("decision") != "approve":
            provider.store.consume_pending_request(rid)
            return RedirectResponse(
                construct_redirect_uri(
                    pending.redirect_uri, error="access_denied", state=pending.state
                ),
                status_code=303,
            )
        provider.store.record_approval(principal, pending.client_id)
        return _issue_or_deny(request, provider, principal, pending, rid)


def _session_principal(request: Request) -> str | None:
    """The normalized principal carried by the browser session, or None.

    Reads the raw session value WITHOUT the allowlist filter that
    ``session_login`` applies: the consent route must be able to tell a
    signed-in-but-de-allowlisted friend (-> deny at issuance with restricted.html)
    apart from a not-signed-in visitor (-> bounce to login). The live allowlist
    check then runs explicitly at code issuance (KTD4/R8).
    """
    raw = request.session.get("github_login")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return normalize_principal(raw)
    except ValueError:
        return None


def _scopes_for(pending: PendingRequest) -> list[str]:
    return list(pending.scopes) or ["meme:read"]


def _issue_or_deny(
    request: Request,
    provider: MemeAuthProvider,
    principal: str,
    pending: PendingRequest,
    rid: str,
) -> Response:
    allowlist = request.app.state.web_allowlist
    pin_store = getattr(request.app.state, "pin_store", None)
    if not is_authorized(principal, allowlist=allowlist, pin_store=pin_store):
        return _restricted(request)
    # Single-use: a replayed consent POST after the code was issued fails here.
    if not provider.store.consume_pending_request(rid):
        return _expired(request)
    code = provider.create_authorization_code(principal=principal, pending=pending)
    return RedirectResponse(
        construct_redirect_uri(pending.redirect_uri, code=code, state=pending.state),
        status_code=303,
    )


def _render_consent(
    request: Request,
    templates: Jinja2Templates,
    principal: str,
    pending: PendingRequest,
    rid: str,
) -> Response:
    pin_store = getattr(request.app.state, "pin_store", None)
    response = templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {
            "friend_login": display_label(principal, pin_store),
            "client_label": pending.client_id,
            "scopes": _scopes_for(pending),
            "redirect_uri": pending.redirect_uri,
            "consent_action": f"{CONSENT_PATH}/{rid}",
            "csrf_token": _csrf_token(request),
            "web_session": False,
            "pat_expires_in_days": None,
        },
    )
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _restricted(request: Request) -> Response:
    settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.web_templates
    return templates.TemplateResponse(
        request,
        "restricted.html",
        {
            "operator_github_login": settings.operator_github_login,
            "pat_expires_in_days": None,
            "friend_login": None,
            "web_session": False,
        },
        status_code=403,
    )


def _expired(request: Request) -> Response:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<p>This authorization request has expired or was already used. "
        "Please start the connection again from your client.</p>",
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def _csrf_token(request: Request) -> str:
    from meme_mcp.web.csrf import ensure_csrf_token

    return ensure_csrf_token(request.session)
