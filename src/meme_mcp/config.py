from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    storage_dir: str = "storage"
    database_url: str = "sqlite+aiosqlite:///storage/meme-mcp.db"
    image_store_backend: Literal["filesystem", "s3"] = "filesystem"
    image_store_fs_path: str = "storage/images"

    # Render-output retention, in days. Single source of truth: the gc-renders
    # CLI sweeps blobs older than this (the cronjob runs `gc-renders` with no
    # flag and inherits it), and it bounds render_url_ttl_seconds below so a
    # signed URL can never outlive its blob. Default 30 days.
    render_gc_ttl_days: int = 30

    # TTL for the signed ``?exp=&sig=`` token on a generated render URL. Long
    # enough that an MCP client can display the meme across a conversation.
    # validate_at_startup rejects a value above render_gc_ttl_days so a live URL
    # never points at an already-GC'd blob. Default 7 days.
    render_url_ttl_seconds: int = 7 * 24 * 60 * 60
    # Long-edge cap for inline MCP image display. The stored render remains full
    # size; only the image-content response is downscaled.
    inline_image_max_px: int = 1280

    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_region: str | None = None
    s3_force_path_style: bool = True

    github_client_id: str
    github_client_secret: SecretStr
    github_redirect_uri: str
    github_allowlist_path: str
    operator_github_login: str

    # Externally visible base URL (scheme://host[:port], no path). Optional and
    # provider-independent: it is the canonical origin advertised in MCP OAuth
    # metadata AND used to sign rendered_url values. When unset it is derived from
    # GITHUB_REDIRECT_URI (strip /auth/callback) for zero-migration. When set it
    # is used verbatim, decoupling the public origin from any single provider's
    # redirect URI so a second OAuth provider can be added without the advertised
    # origin diverging. Changing its origin invalidates outstanding signed render
    # URLs, so validate_at_startup fails closed on an origin conflict (below).
    public_base_url: str | None = None

    session_secret: SecretStr
    pat_hash_pepper: SecretStr

    trusted_proxy_depth: int = 0

    vlm_base_url: str
    vlm_api_key: SecretStr
    vlm_model: str

    # Reverse-image enrichment (Google Cloud Vision Web Detection). Deploy-gated
    # and OFF by default: enabling sends uploaded images off-box to Google when a
    # caller opts in per request (KTD7). The credentials path is a plain str -- the
    # service-account JSON it points to is the secret and is never read or logged
    # here; U2 hands the path to the SDK, which opens it.
    reverse_image_enabled: bool = False
    google_vision_credentials_path: str | None = None

    # Google OAuth sign-in (optional second provider, OFF by default). Mirrors the
    # reverse-image gating convention: when disabled, only GitHub login is offered
    # and absence of these values never breaks the GitHub path. When enabled, all
    # three are required and google_redirect_uri must end in /auth/google/callback
    # and resolve to the app's canonical public origin (see validate_at_startup).
    google_oauth_enabled: bool = False
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None

    # Native MCP OAuth 2.1 authorization server (optional, OFF by default). When
    # enabled, meme-mcp serves its own /authorize, /token, /register, /revoke and
    # RFC 8414 metadata so Claude's native custom-connector UI can connect with a
    # URL + sign-in (no mcp-remote bridge). Both secrets are required when on:
    # OAUTH_TOKEN_PEPPER hashes issued tokens/codes/nonces at rest;
    # OAUTH_SECRET_ENC_KEY is a reversible AEAD key encrypting a confidential
    # client's secret (the SDK compares it directly, so it cannot be hashed).
    # The issuer/resource origin reuses resolve_public_base_url (no new origin).
    oauth_as_enabled: bool = False
    oauth_token_pepper: SecretStr | None = None
    oauth_secret_enc_key: SecretStr | None = None
    # Per-IP rate limit (requests/min) on the pre-auth OAuth endpoints
    # (/register, /authorize, /token); open-DCR abuse mitigation (R7, U6).
    rate_oauth_per_min: int = 60
    # Delete registered clients with no successful authorization within this many
    # days (open-DCR storage hygiene), consistent with render_gc_ttl_days.
    oauth_client_gc_ttl_days: int = 30

    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_api_key: SecretStr
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: int = 1024

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    # Streamable HTTP DNS-rebinding allowlists. These are list[str] settings, so
    # env values are JSON arrays, e.g. MCP_ALLOWED_HOSTS='["meme.igene.tw"]' (a
    # bare scalar raises SettingsError). A public deploy MUST set its gateway
    # Host/Origin here or every authenticated MCP request is rejected with 421;
    # validate_at_startup fails fast if a non-local deploy keeps these defaults.
    # Defaults mirror FastMCP's localhost auto-allowlist (IPv4 + IPv6 loopback).
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    )
    mcp_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
    )

    rate_find_per_min: int = 60
    rate_generate_per_min: int = 30
    rate_upload_per_hour: int = 10
    rate_pat_admin_per_hour: int = 10
    audit_log_path: str | None = None


def _secret_value(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret else ""


def _is_loopback_host(entry: str) -> bool:
    """True if an allowed-host entry only matches a loopback address."""
    base = entry[:-2] if entry.endswith(":*") else entry
    return base in ("127.0.0.1", "localhost", "[::1]")


def validate_at_startup(settings: Settings) -> None:
    problems: list[str] = []
    # The MCP public OAuth issuer/resource URLs are derived by stripping
    # /auth/callback off this; a URI without that suffix would silently bake a
    # broken path into the advertised metadata, so reject it before migrations
    # or any other startup side effect run.
    if not settings.github_redirect_uri.rstrip("/").endswith("/auth/callback"):
        problems.append("GITHUB_REDIRECT_URI must end with /auth/callback")
    if settings.render_gc_ttl_days <= 0:
        problems.append("RENDER_GC_TTL_DAYS must be > 0")
    if settings.render_url_ttl_seconds <= 0:
        problems.append("RENDER_URL_TTL_SECONDS must be > 0")
    elif settings.render_url_ttl_seconds > settings.render_gc_ttl_days * 86400:
        # A URL outliving its blob would 404 silently; refuse the config so the
        # signed-URL TTL and the GC retention cannot drift apart.
        problems.append(
            "RENDER_URL_TTL_SECONDS must be <= RENDER_GC_TTL_DAYS in seconds "
            "(a signed render URL must not outlive its GC'd blob)"
        )
    if settings.inline_image_max_px <= 0:
        problems.append("INLINE_IMAGE_MAX_PX must be > 0")
    if settings.mcp_host != "127.0.0.1":
        if len(_secret_value(settings.session_secret)) < 32 or "dev" in _secret_value(
            settings.session_secret
        ):
            problems.append("SESSION_SECRET must be at least 32 chars and non-placeholder")
        if len(_secret_value(settings.pat_hash_pepper)) < 32 or "dev" in _secret_value(
            settings.pat_hash_pepper
        ):
            problems.append("PAT_HASH_PEPPER must be at least 32 chars and non-placeholder")
        # A non-local bind serves a public Host; if every allowed host is still a
        # loopback default the MCP transport 421s all real traffic at runtime, so
        # fail fast instead. (Set MCP_ALLOWED_HOSTS to the gateway host.)
        if all(_is_loopback_host(host) for host in settings.mcp_allowed_hosts):
            problems.append(
                "MCP_ALLOWED_HOSTS must include the public gateway host when MCP_HOST is non-local"
            )
    if settings.image_store_backend == "s3":
        missing = [
            name
            for name, value in {
                "S3_ENDPOINT": settings.s3_endpoint,
                "S3_BUCKET": settings.s3_bucket,
                "S3_ACCESS_KEY_ID": settings.s3_access_key_id,
                "S3_SECRET_ACCESS_KEY": settings.s3_secret_access_key,
                "S3_REGION": settings.s3_region,
            }.items()
            if value is None
        ]
        problems.extend(missing)
    if settings.reverse_image_enabled:
        _validate_vision_credentials(settings.google_vision_credentials_path, problems)
    if settings.public_base_url is not None:
        _validate_public_base_url(settings, problems)
    if settings.google_oauth_enabled:
        _validate_google_oauth(settings, problems)
    if settings.oauth_as_enabled:
        _validate_oauth_as(settings, problems)
    if problems:
        raise ConfigError("; ".join(problems))


def _validate_oauth_as(settings: Settings, problems: list[str]) -> None:
    """Fail fast on a missing or weak OAuth authorization-server secret set.

    Both secrets are always required when the AS is enabled -- the store cannot
    hash tokens or encrypt a client secret without them. The >=32-char /
    non-"dev" strength rule is relaxed on a loopback bind, mirroring the
    session/pat-pepper relaxation keyed on ``mcp_host`` above; note
    ``_validate_google_oauth`` has no loopback relaxation, so it is not the
    template for the strength check here.
    """
    pepper = _secret_value(settings.oauth_token_pepper)
    enc_key = _secret_value(settings.oauth_secret_enc_key)
    if not pepper:
        problems.append("OAUTH_TOKEN_PEPPER is required when OAUTH_AS_ENABLED is true")
    if not enc_key:
        problems.append("OAUTH_SECRET_ENC_KEY is required when OAUTH_AS_ENABLED is true")
    if settings.mcp_host != "127.0.0.1":
        if pepper and (len(pepper) < 32 or "dev" in pepper):
            problems.append("OAUTH_TOKEN_PEPPER must be at least 32 chars and non-placeholder")
        if enc_key and (len(enc_key) < 32 or "dev" in enc_key):
            problems.append("OAUTH_SECRET_ENC_KEY must be at least 32 chars and non-placeholder")


def _validate_google_oauth(settings: Settings, problems: list[str]) -> None:
    """Fail fast on an incomplete or origin-mismatched Google OAuth config.

    When enabled, all three Google fields are required and the redirect URI must
    end in ``/auth/google/callback`` AND resolve to the app's canonical public
    origin -- the callback route is served by this app, so a redirect URI on a
    different origin/base path would boot a dead Google config.
    """
    missing = [
        name
        for name, value in {
            "GOOGLE_CLIENT_ID": settings.google_client_id,
            "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
            "GOOGLE_REDIRECT_URI": settings.google_redirect_uri,
        }.items()
        if value is None
    ]
    if missing:
        problems.extend(f"{name} is required when GOOGLE_OAUTH_ENABLED is true" for name in missing)
        return
    redirect = (settings.google_redirect_uri or "").rstrip("/")
    if not redirect.endswith("/auth/google/callback"):
        problems.append("GOOGLE_REDIRECT_URI must end with /auth/google/callback")
        return
    base = redirect.removesuffix("/auth/google/callback").rstrip("/")
    try:
        canonical = resolve_public_base_url(settings)
    except ConfigError:
        # The github/public-base origin is itself invalid; that error is already
        # accumulated elsewhere, so do not pile on a confusing secondary message.
        return
    if _origin_key(base) != _origin_key(canonical):
        problems.append(
            f"GOOGLE_REDIRECT_URI origin {_origin_key(base)} must match the app's "
            f"public origin {_origin_key(canonical)}"
        )


def _origin_key(url: str) -> str:
    """Normalized scheme://host:port for an origin comparison.

    Default ports are filled in so ``http://host`` and ``http://host:80`` compare
    equal; scheme and host are lowercased.
    """
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}:{port}"


def _base_from_redirect(github_redirect_uri: str) -> str:
    """Derive the public base URL from the GitHub callback URL.

    The callback path must end in ``/auth/callback`` (the route the app serves);
    otherwise ``removesuffix`` is a silent no-op that would bake the full callback
    path into the advertised metadata, so fail fast.
    """
    parsed = urlsplit(github_redirect_uri)
    path = parsed.path.rstrip("/")
    if not path.endswith("/auth/callback"):
        raise ConfigError(
            f"GITHUB_REDIRECT_URI must end with /auth/callback: {github_redirect_uri}"
        )
    base_path = path.removesuffix("/auth/callback").rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def _validate_public_base_url(settings: Settings, problems: list[str]) -> None:
    """Fail closed on a malformed PUBLIC_BASE_URL or an origin conflict.

    Because this origin signs ``rendered_url`` values, a silent origin change
    invalidates every outstanding signed URL. When PUBLIC_BASE_URL and
    GITHUB_REDIRECT_URI resolve to different origins (scheme+host+port), refuse to
    start rather than re-sign against a new origin behind the operator's back.
    """
    explicit = (settings.public_base_url or "").rstrip("/")
    parsed = urlsplit(explicit)
    if not parsed.scheme or not parsed.netloc:
        problems.append("PUBLIC_BASE_URL must be an absolute URL with scheme and host")
        return
    redirect = settings.github_redirect_uri.rstrip("/")
    # Only compare origins when the redirect is itself well-formed; its own
    # /auth/callback check ran above and would already have flagged a bad value.
    if redirect.endswith("/auth/callback"):
        derived = redirect.removesuffix("/auth/callback").rstrip("/")
        if _origin_key(explicit) != _origin_key(derived):
            problems.append(
                f"PUBLIC_BASE_URL origin {_origin_key(explicit)} conflicts with "
                f"GITHUB_REDIRECT_URI origin {_origin_key(derived)} (this origin "
                "signs render URLs; repoint both deliberately to change it)"
            )


def resolve_public_base_url(settings: Settings) -> str:
    """Canonical externally-visible base URL (no trailing slash).

    Prefers PUBLIC_BASE_URL verbatim; otherwise derives from GITHUB_REDIRECT_URI.
    Assumes ``validate_at_startup`` already ran (create_app calls it first), so
    the conflict/format checks have surfaced; still defensive on the redirect
    suffix.
    """
    if settings.public_base_url is not None:
        return settings.public_base_url.rstrip("/")
    return _base_from_redirect(settings.github_redirect_uri)


def session_cookie_secure(settings: Settings) -> bool:
    """Whether the session/lang cookie ``Secure`` flag should be set.

    Follows the canonical public origin's scheme when PUBLIC_BASE_URL is set;
    otherwise keeps the historical GITHUB_REDIRECT_URI localhost check. The
    fail-closed origin-conflict rule guarantees the two never disagree when both
    are set, so OAuth state (carried in the session cookie) round-trips: a
    local-dev http://localhost deploy stays non-Secure, a production https origin
    is Secure.
    """
    if settings.public_base_url is not None:
        return settings.public_base_url.lower().startswith("https://")
    return not settings.github_redirect_uri.startswith("http://localhost")


def _validate_vision_credentials(path: str | None, problems: list[str]) -> None:
    """Fail fast on a missing/unusable Vision credentials path when enabled.

    Stats the file without opening it (the service-account JSON is the secret and
    must never be read or logged here); a missing path or non-regular-file is a
    hard config error so a misprovisioned deploy is caught at startup rather than
    silently degrading every upload to image-only enrichment at first request.
    Group/other-readability and a pre-set GOOGLE_APPLICATION_CREDENTIALS are
    warnings, not failures.
    """
    if not path:
        problems.append(
            "GOOGLE_VISION_CREDENTIALS_PATH is required when REVERSE_IMAGE_ENABLED is true"
        )
        return
    try:
        info = Path(path).stat()
    except OSError:
        problems.append(f"GOOGLE_VISION_CREDENTIALS_PATH does not exist or is unreadable: {path}")
        return
    if not stat.S_ISREG(info.st_mode):
        problems.append(f"GOOGLE_VISION_CREDENTIALS_PATH is not a regular file: {path}")
        return
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "Google Vision credentials file is group/other-readable; "
            "restrict it to the service account (chmod 600)."
        )
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        logger.warning(
            "GOOGLE_APPLICATION_CREDENTIALS is set process-wide; reverse-image "
            "enrichment passes its credentials path explicitly and does not use it."
        )
