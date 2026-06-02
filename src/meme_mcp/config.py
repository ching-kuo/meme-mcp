from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Literal

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

    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

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
    if problems:
        raise ConfigError("; ".join(problems))


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
