"""HMAC signing for render-output URLs.

A rendered meme lives behind the auth-gated ``GET /renders/...`` route, but the
absolute ``rendered_url`` handed back by ``generate`` is fetched by image clients
(Claude Desktop, a browser tab, an ``<img>`` tag) that cannot attach the caller's
Bearer PAT or session cookie. Without a credential the route returns a JSON 401
instead of the PNG.

These helpers append a short-lived HMAC signature (``?exp=&sig=``) so possession
of the URL is itself the capability to view that one render -- the presigned-URL
model. The signing key is derived from ``session_secret`` with a domain tag so it
never collides with the session-cookie signer that consumes the same secret.

Invariant: the signature TTL (``render_url_ttl_seconds``) must stay <= the
render-output GC retention (``render_gc_ttl_days``), or a still-valid URL could
point at a blob that GC already deleted. ``validate_at_startup`` enforces this.
"""

from __future__ import annotations

import base64
import hmac

_DOMAIN = b"render-url-v1"


def _signing_key(secret: str) -> bytes:
    """Derive a render-URL-specific key from the app signing secret.

    Domain separation keeps this key distinct from the raw ``session_secret``
    used by Starlette's session-cookie signer, so a render signature can never
    be cross-used as a session token (or vice versa).
    """
    return hmac.new(secret.encode(), _DOMAIN, "sha256").digest()


def _signature(path: str, exp: int, secret: str) -> str:
    mac = hmac.new(_signing_key(secret), f"{path}:{exp}".encode(), "sha256").digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def sign_render_url(rendered_url: str, secret: str, now: int, ttl_seconds: int) -> str:
    """Append ``?exp=&sig=`` to an absolute ``.../renders/<aa>/<rest>.png`` URL.

    The signature binds to the content-addressed store path (the segment after
    ``/renders/``, which the route reconstructs from its two URL segments) rather
    than the host, so a host change does not invalidate it. Deriving the path from
    the URL keeps it a single source of truth -- there is no separate ``path``
    argument that could disagree with the URL being signed.
    """
    exp = now + ttl_seconds
    path = rendered_url.split("/renders/", 1)[1]
    return f"{rendered_url}?exp={exp}&sig={_signature(path, exp, secret)}"


def verify_render_signature(path: str, exp: str, sig: str, secret: str, now: int) -> bool:
    """True iff ``sig`` is a live signature for ``path``.

    Fails closed on a non-integer ``exp`` and on an expired timestamp; the digest
    comparison is constant-time. A malformed/absent signature is a verification
    miss, not an error, so the caller falls back to session/PAT auth.
    """
    try:
        exp_int = int(exp)
    except ValueError:
        return False
    if now > exp_int:
        return False
    return hmac.compare_digest(sig, _signature(path, exp_int, secret))
