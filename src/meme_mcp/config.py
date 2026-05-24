from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr
    embedding_model: str = "text-embedding-3-small"

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

    rate_find_per_min: int = 60
    rate_generate_per_min: int = 30
    rate_upload_per_hour: int = 10


def _secret_value(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret else ""


def validate_at_startup(settings: Settings) -> None:
    problems: list[str] = []
    if settings.mcp_host != "127.0.0.1":
        if len(_secret_value(settings.session_secret)) < 32 or "dev" in _secret_value(
            settings.session_secret
        ):
            problems.append("SESSION_SECRET must be at least 32 chars and non-placeholder")
        if len(_secret_value(settings.pat_hash_pepper)) < 32 or "dev" in _secret_value(
            settings.pat_hash_pepper
        ):
            problems.append("PAT_HASH_PEPPER must be at least 32 chars and non-placeholder")
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
    if problems:
        raise ConfigError("; ".join(problems))

