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
ALLOWED_NEXT_PATHS = frozenset({"/upload", "/browse", "/account"})
TEMPLATE_DETAIL_PREFIX = "/templates/"

# The language switch (KTD7) must work on every rendered page, including the
# anonymous landing page "/" that the login-oriented ALLOWED_NEXT_PATHS omits.
# Its default return is "/" (not /browse), and it preserves the query string so
# switching language on a search/filter page does not discard the user's query.
LANG_RETURN_PATHS = frozenset({"/", "/upload", "/browse", "/account"})
LANG_DEFAULT_NEXT = "/"


def _is_template_detail(path: str) -> bool:
    """True for a single-segment template detail path (``/templates/<id>``).

    Detail pages are shareable, so a friend who opens one while signed out
    should return to it after GitHub login rather than the generic gallery.
    Only a single trailing segment is accepted: an embedded slash never matches
    (so the ``/templates/<id>/image`` sub-route is excluded), and a bare ``.``
    or ``..`` segment that a browser would normalize to a parent path is
    rejected. Open-redirect safety is already guaranteed by the scheme/netloc
    checks in :func:`safe_next`; this is an in-origin allowlist only.
    """

    if not path.startswith(TEMPLATE_DETAIL_PREFIX):
        return False
    rest = path[len(TEMPLATE_DETAIL_PREFIX) :]
    return bool(rest) and "/" not in rest and rest not in {".", ".."}


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


def safe_next(
    raw: object,
    *,
    allowlist: frozenset[str] = ALLOWED_NEXT_PATHS,
    default: str = DEFAULT_NEXT,
    keep_query: bool = False,
) -> str:
    """Return a safe relative return path, or ``default``.

    Accepts only a server-relative path with a single leading slash and no
    scheme or netloc. Rejects protocol-relative (``//``) and backslash
    (``/\\``) prefixes, leading control characters, and any absolute or
    scheme-bearing URL. The accepted path must additionally be on ``allowlist``
    or be a template detail path (``/templates/<id>``). Anything else falls back
    to ``default``. Mirrors Django's ``url_has_allowed_host_and_scheme`` logic.

    The ``allowlist`` / ``default`` / ``keep_query`` parameters let a second
    consumer reuse this single anti-open-redirect core instead of duplicating
    it (a duplicate would have to mirror every future security fix). The login
    flow uses the defaults; :func:`safe_lang_return` passes the switch's own
    allowlist and preserves the query. Open-redirect safety is unaffected by
    ``keep_query``: scheme/netloc only ever appear at the start of a URL, so a
    ``//evil.com`` sitting in the query of an allowlisted path stays a
    same-origin query value, never a redirect target.
    """

    if not isinstance(raw, str) or not raw:
        return default
    # Reject any control or whitespace character outright (do NOT strip first):
    # browsers strip leading control/whitespace, so " /x", "\t/x", or "\x00//x"
    # could smuggle a scheme or netloc past the parser if normalized away.
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        return default
    # Must be a path-only reference: single leading slash, not protocol-relative
    # ("//") and not a backslash-prefixed ("/\\") trick that some browsers
    # normalize to a scheme-relative URL.
    if not raw.startswith("/") or raw.startswith(("//", "/\\")):
        return default
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return default
    if parts.path not in allowlist and not _is_template_detail(parts.path):
        return default
    if keep_query and parts.query:
        return f"{parts.path}?{parts.query}"
    return parts.path


def safe_lang_return(raw: object) -> str:
    """Validate the language switch's ``next`` return target (KTD7).

    A thin delegate of :func:`safe_next` parameterized with the switch's
    all-rendered-pages allowlist (including the landing page ``/``), a ``/``
    default, and query preservation so search/filter state survives a switch.
    The input is the already-percent-decoded ``next`` query param; it is not
    decoded again here. The restricted page's ``/auth/callback`` return is not
    on the allowlist and so correctly falls back to ``/``.
    """

    return safe_next(raw, allowlist=LANG_RETURN_PATHS, default=LANG_DEFAULT_NEXT, keep_query=True)
