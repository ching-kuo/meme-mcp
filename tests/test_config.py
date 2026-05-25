import pytest
from pydantic import SecretStr

from meme_mcp.config import ConfigError, Settings, validate_at_startup


def good_settings(**overrides: object) -> Settings:
    data = {
        "storage_dir": "storage",
        "database_url": "sqlite+aiosqlite:///storage/test.db",
        "image_store_backend": "filesystem",
        "image_store_fs_path": "storage/images",
        "github_client_id": "cid",
        "github_client_secret": SecretStr("secret-32-chars-value-for-tests"),
        "github_redirect_uri": "http://localhost:8000/auth/callback",
        "github_allowlist_path": "storage/allowlist.txt",
        "operator_github_login": "operator",
        "session_secret": SecretStr("session-secret-32-chars-value-tests"),
        "pat_hash_pepper": SecretStr("pepper-secret-32-chars-value-tests"),
        "vlm_base_url": "https://example.test/v1",
        "vlm_api_key": SecretStr("vlm-key"),
        "vlm_model": "vlm-model",
        "embedding_api_key": SecretStr("embedding-key"),
    }
    data.update(overrides)
    return Settings(**data)


def test_valid_local_settings_pass() -> None:
    validate_at_startup(good_settings())


def test_non_loopback_rejects_dev_secret() -> None:
    settings = good_settings(mcp_host="0.0.0.0", session_secret=SecretStr("dev-placeholder"))
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        validate_at_startup(settings)


def test_s3_requires_s3_fields() -> None:
    settings = good_settings(image_store_backend="s3")
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        validate_at_startup(settings)


def test_secret_repr_is_redacted() -> None:
    assert repr(good_settings().github_client_secret) == "SecretStr('**********')"


def test_embedding_dimensions_accepts_override() -> None:
    assert good_settings(embedding_dimensions=768).embedding_dimensions == 768
    assert good_settings(embedding_dimensions=1536).embedding_dimensions == 1536

