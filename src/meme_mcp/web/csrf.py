"""Session-bound CSRF token helper and a relative-path ``next`` validator.

CSRF protection follows the OWASP synchronizer-token pattern: a per-session
token is minted with :func:`secrets.token_urlsafe`, stored in the signed
session cookie, surfaced to the client, and required as a custom
``X-CSRF-Token`` request header on every state-changing request. Validation
uses :func:`secrets.compare_digest`.

The header-only transport is itself a CSRF defense: a custom request header
cannot be set on a cross-origin request without a CORS pre-flight that the
server never grants. A form-field token fallback is deliberately omitted
because a form field is submittable cross-origin and would reintroduce the
hole the header requirement closes.

:func:`safe_next` validates an optional login-return path, mirroring Django's
``url_has_allowed_host_and_scheme`` logic, and is folded here rather than into
a single-consumer module.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.requests import Request

from meme_mcp.errors import ErrorCode, MemeMCPError

CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

DEFAULT_NEXT = "/browse"
ALLOWED_NEXT_PATHS = frozenset({"/upload", "/browse"})


def ensure_csrf_token(session: dict[str, object]) -> str:
    """Return the session CSRF token, minting one if absent.

    Idempotent within a session: repeated calls return the same token until
    the session is cleared (login clears the session, so ``GET /upload`` must
    call this to guarantee a token exists for the post-login page).
    """

    existing = session.get(CSRF_SESSION_KEY)
    if isinstance(existing, str) and existing:
        return existing
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def require_csrf(request: Request) -> None:
    """Validate the ``X-CSRF-Token`` header against the session token.

    Raises :class:`MemeMCPError` with :data:`ErrorCode.FORBIDDEN` when the
    header is missing, the session has no token, or the two do not match under
    a constant-time comparison. Reads the header only -- there is no form-field
    fallback.
    """

    session_token = request.session.get(CSRF_SESSION_KEY)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if not isinstance(session_token, str) or not session_token or not header_token:
        raise MemeMCPError(ErrorCode.FORBIDDEN, [{"field": "csrf", "reason": "missing"}])
    if not secrets.compare_digest(session_token, header_token):
        raise MemeMCPError(ErrorCode.FORBIDDEN, [{"field": "csrf", "reason": "mismatch"}])


def safe_next(raw: object) -> str:
    """Return a safe relative login-return path, or :data:`DEFAULT_NEXT`.

    Accepts only a server-relative path with a single leading slash and no
    scheme or netloc. Rejects protocol-relative (``//``) and backslash
    (``/\\``) prefixes, leading control characters, and any absolute or
    scheme-bearing URL. The accepted path must additionally be on the
    :data:`ALLOWED_NEXT_PATHS` allowlist. Anything else falls back to
    :data:`DEFAULT_NEXT`. Mirrors Django's
    ``url_has_allowed_host_and_scheme`` logic.
    """

    if not isinstance(raw, str) or not raw:
        return DEFAULT_NEXT
    # Reject any control or whitespace character outright (do NOT strip first):
    # browsers strip leading control/whitespace, so " /x", "\t/x", or "\x00//x"
    # could smuggle a scheme or netloc past the parser if normalized away.
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        return DEFAULT_NEXT
    # Must be a path-only reference: single leading slash, not protocol-relative
    # ("//") and not a backslash-prefixed ("/\\") trick that some browsers
    # normalize to a scheme-relative URL.
    if not raw.startswith("/") or raw.startswith(("//", "/\\")):
        return DEFAULT_NEXT
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT
    if parts.path not in ALLOWED_NEXT_PATHS:
        return DEFAULT_NEXT
    return parts.path
