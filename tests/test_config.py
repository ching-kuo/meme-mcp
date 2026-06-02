import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsError

from meme_mcp.config import (
    ConfigError,
    Settings,
    resolve_public_base_url,
    session_cookie_secure,
    validate_at_startup,
)


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


def test_non_loopback_requires_public_allowed_host() -> None:
    # A public bind that keeps the loopback-only default allowlist would 421 all
    # real MCP traffic at runtime; validate_at_startup must reject it up front.
    settings = good_settings(mcp_host="0.0.0.0")
    with pytest.raises(ConfigError, match="MCP_ALLOWED_HOSTS"):
        validate_at_startup(settings)


def test_non_loopback_passes_with_public_allowed_host() -> None:
    validate_at_startup(good_settings(mcp_host="0.0.0.0", mcp_allowed_hosts=["meme.igene.tw"]))


def test_mcp_allowed_hosts_env_parses_json_array(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", '["meme.igene.tw", "meme.igene.tw:*"]')
    assert good_settings().mcp_allowed_hosts == ["meme.igene.tw", "meme.igene.tw:*"]


def test_mcp_allowed_hosts_env_rejects_bare_scalar(monkeypatch) -> None:
    # list[str] settings are JSON-decoded from env; a bare scalar is invalid.
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "meme.igene.tw")
    with pytest.raises(SettingsError):
        good_settings()


def test_s3_requires_s3_fields() -> None:
    settings = good_settings(image_store_backend="s3")
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        validate_at_startup(settings)


def test_render_url_ttl_above_gc_retention_rejected() -> None:
    # A signed URL must not outlive its GC'd blob: TTL > retention is refused.
    settings = good_settings(render_gc_ttl_days=1, render_url_ttl_seconds=2 * 86400)
    with pytest.raises(ConfigError, match="RENDER_URL_TTL_SECONDS"):
        validate_at_startup(settings)


def test_render_url_ttl_within_gc_retention_passes() -> None:
    validate_at_startup(good_settings(render_gc_ttl_days=30, render_url_ttl_seconds=7 * 86400))


def test_render_gc_ttl_days_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="RENDER_GC_TTL_DAYS"):
        validate_at_startup(good_settings(render_gc_ttl_days=0))


def test_secret_repr_is_redacted() -> None:
    assert repr(good_settings().github_client_secret) == "SecretStr('**********')"


def test_embedding_dimensions_accepts_override() -> None:
    assert good_settings(embedding_dimensions=768).embedding_dimensions == 768
    assert good_settings(embedding_dimensions=1536).embedding_dimensions == 1536


def test_reverse_image_disabled_validates_without_credentials() -> None:
    # The default (feature off) needs no Vision credentials.
    validate_at_startup(good_settings())
    validate_at_startup(good_settings(reverse_image_enabled=False))


def test_reverse_image_enabled_with_credentials_file_passes(tmp_path) -> None:
    creds = tmp_path / "vision.json"
    creds.write_text("{}")
    validate_at_startup(
        good_settings(
            reverse_image_enabled=True,
            google_vision_credentials_path=str(creds),
        )
    )


def test_reverse_image_enabled_without_path_fails() -> None:
    with pytest.raises(ConfigError, match="GOOGLE_VISION_CREDENTIALS_PATH is required"):
        validate_at_startup(good_settings(reverse_image_enabled=True))


def test_reverse_image_enabled_with_missing_file_fails(tmp_path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        validate_at_startup(
            good_settings(
                reverse_image_enabled=True,
                google_vision_credentials_path=str(tmp_path / "absent.json"),
            )
        )


def test_reverse_image_enabled_with_directory_path_fails(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not a regular file"):
        validate_at_startup(
            good_settings(
                reverse_image_enabled=True,
                google_vision_credentials_path=str(tmp_path),
            )
        )


def test_reverse_image_enabled_warns_when_adc_set(tmp_path, monkeypatch, caplog) -> None:
    creds = tmp_path / "vision.json"
    creds.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/some/other/creds.json")
    with caplog.at_level("WARNING"):
        validate_at_startup(
            good_settings(
                reverse_image_enabled=True,
                google_vision_credentials_path=str(creds),
            )
        )
    assert any("GOOGLE_APPLICATION_CREDENTIALS" in record.message for record in caplog.records)


def test_public_base_url_unset_falls_back_to_github_redirect_derivation() -> None:
    settings = good_settings()
    validate_at_startup(settings)
    assert resolve_public_base_url(settings) == "http://localhost:8000"


def test_public_base_url_set_used_verbatim() -> None:
    settings = good_settings(
        github_redirect_uri="https://meme.example/auth/callback",
        public_base_url="https://meme.example",
    )
    validate_at_startup(settings)
    assert resolve_public_base_url(settings) == "https://meme.example"


def test_public_base_url_trailing_slash_stripped() -> None:
    settings = good_settings(
        github_redirect_uri="https://meme.example/auth/callback",
        public_base_url="https://meme.example/",
    )
    assert resolve_public_base_url(settings) == "https://meme.example"


def test_public_base_url_malformed_rejected() -> None:
    settings = good_settings(public_base_url="meme.example")
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        validate_at_startup(settings)


def test_public_base_url_origin_conflict_rejected() -> None:
    # Same host, different scheme/port => different origin => fail closed because
    # this origin signs render URLs.
    settings = good_settings(
        github_redirect_uri="https://meme.example/auth/callback",
        public_base_url="http://meme.example",
    )
    with pytest.raises(ConfigError, match="conflicts with"):
        validate_at_startup(settings)


def test_public_base_url_matching_origin_passes() -> None:
    settings = good_settings(
        github_redirect_uri="https://meme.example:443/auth/callback",
        public_base_url="https://meme.example",
    )
    # Default https port 443 normalizes to the bare host, so origins match.
    validate_at_startup(settings)


def test_session_cookie_secure_follows_canonical_origin() -> None:
    # Local dev (unset, http://localhost redirect) => not Secure, so OAuth state
    # round-trips on localhost.
    assert session_cookie_secure(good_settings()) is False
    # https PUBLIC_BASE_URL => Secure.
    secure = good_settings(
        github_redirect_uri="https://meme.example/auth/callback",
        public_base_url="https://meme.example",
    )
    assert session_cookie_secure(secure) is True

