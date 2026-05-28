"""meme-mcp migrate orchestrator tests.

External CLIs (pgloader, rclone) and Postgres/S3 connectivity are mocked here. The
real cutover is exercised by a human operator once per environment, per the plan.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from meme_mcp.cli.migrate import MigrateError, _sqlite_url_for_pgloader, _sync_url, run
from meme_mcp.config import Settings


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'meme.db'}",
        image_store_backend="filesystem",
        image_store_fs_path=str(tmp_path / "images"),
        github_client_id="cid",
        github_client_secret=SecretStr("secret-32-chars-value-for-tests"),
        github_redirect_uri="http://localhost:8000/auth/callback",
        github_allowlist_path=str(tmp_path / "allowlist.txt"),
        operator_github_login="operator",
        session_secret=SecretStr("session-secret-32-chars-value-tests"),
        pat_hash_pepper=SecretStr("pepper-secret-32-chars-value-tests"),
        vlm_base_url="https://example.test/v1",
        vlm_api_key=SecretStr("vlm-key"),
        vlm_model="vlm-model",
        embedding_api_key=SecretStr("embedding-key"),
    )


@pytest.fixture()
def all_checks_pass(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Patch every external dependency to succeed, so individual tests can flip one
    failure mode without rebuilding the whole stack."""
    with (
        patch("meme_mcp.cli.migrate._check_external_clis"),
        patch("meme_mcp.cli.migrate._check_postgres_reachable_with_pgvector"),
        patch("meme_mcp.cli.migrate._check_s3_reachable"),
    ):
        yield


def test_dry_run_succeeds_when_all_checks_pass(
    tmp_path: Path, all_checks_pass: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(
            _settings(tmp_path),
            target_db="postgresql+psycopg://u:p@h/db",
            target_s3_endpoint="https://s3.example",
            target_s3_bucket="bucket",
            target_s3_access_key="k",
            target_s3_secret_key="s",
            dry_run=True,
        )
        == 0
    )
    assert "dry-run: all checks passed" in capsys.readouterr().out


def test_missing_external_cli_returns_two_with_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(
        "meme_mcp.cli.migrate._check_external_clis",
        side_effect=MigrateError("EXTERNAL_CLI_MISSING", "required CLI not on PATH: pgloader"),
    ):
        result = run(
            _settings(tmp_path),
            target_db="postgresql://u:p@h/db",
            target_s3_endpoint="https://s3.example",
            target_s3_bucket="bucket",
            target_s3_access_key="k",
            target_s3_secret_key="s",
            dry_run=True,
        )
    assert result == 2
    out = capsys.readouterr().out
    assert "EXTERNAL_CLI_MISSING" in out
    assert "pgloader" in out


def test_pgvector_missing_returns_two_with_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("meme_mcp.cli.migrate._check_external_clis"),
        patch(
            "meme_mcp.cli.migrate._check_postgres_reachable_with_pgvector",
            side_effect=MigrateError("PGVECTOR_MISSING", "pgvector extension is not installed"),
        ),
    ):
        result = run(
            _settings(tmp_path),
            target_db="postgresql://u:p@h/db",
            target_s3_endpoint="https://s3.example",
            target_s3_bucket="bucket",
            target_s3_access_key="k",
            target_s3_secret_key="s",
            dry_run=True,
        )
    assert result == 2
    assert "PGVECTOR_MISSING" in capsys.readouterr().out


def test_s3_unreachable_returns_two_with_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("meme_mcp.cli.migrate._check_external_clis"),
        patch("meme_mcp.cli.migrate._check_postgres_reachable_with_pgvector"),
        patch(
            "meme_mcp.cli.migrate._check_s3_reachable",
            side_effect=MigrateError("S3_UNREACHABLE", "S3 round-trip probe failed"),
        ),
    ):
        result = run(
            _settings(tmp_path),
            target_db="postgresql://u:p@h/db",
            target_s3_endpoint="https://s3.example",
            target_s3_bucket="bucket",
            target_s3_access_key="k",
            target_s3_secret_key="s",
            dry_run=True,
        )
    assert result == 2
    assert "S3_UNREACHABLE" in capsys.readouterr().out


def test_live_pgloader_failure_restores_source_chmod(
    tmp_path: Path, all_checks_pass: None
) -> None:
    storage = tmp_path
    storage.chmod(0o755)
    with (
        patch(
            "meme_mcp.cli.migrate._run_pgloader",
            side_effect=MigrateError("PGLOADER_FAILED", "pgloader exit 1: boom"),
        ),
        patch("meme_mcp.cli.migrate._run_rclone_sync"),
        patch("meme_mcp.cli.migrate._run_reindex_embeddings_against_new_db"),
    ):
        result = run(
            _settings(tmp_path),
            target_db="postgresql://u:p@h/db",
            target_s3_endpoint="https://s3.example",
            target_s3_bucket="bucket",
            target_s3_access_key="k",
            target_s3_secret_key="s",
            dry_run=False,
        )
    assert result == 3
    # Source storage_dir restored to writable mode on failure.
    assert storage.stat().st_mode & 0o200


def test_live_success_writes_env_next(tmp_path: Path, all_checks_pass: None) -> None:
    import os
    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with (
            patch("meme_mcp.cli.migrate._run_pgloader"),
            patch("meme_mcp.cli.migrate._run_reindex_embeddings_against_new_db"),
            patch("meme_mcp.cli.migrate._run_rclone_sync"),
        ):
            result = run(
                _settings(tmp_path),
                target_db="postgresql+psycopg://u:p@h/db",
                target_s3_endpoint="https://s3.example",
                target_s3_bucket="bucket",
                target_s3_access_key="k",
                target_s3_secret_key="s",
                dry_run=False,
            )
        assert result == 0
        env_next = (tmp_path / ".env.next").read_text()
        assert "DATABASE_URL=postgresql+psycopg://u:p@h/db" in env_next
        assert "IMAGE_STORE_BACKEND=s3" in env_next
        assert "S3_ENDPOINT=https://s3.example" in env_next
        assert "S3_BUCKET=bucket" in env_next
    finally:
        os.chdir(cwd)


def test_sync_url_rewrites_async_drivers() -> None:
    assert _sync_url("postgresql+asyncpg://u@h/db") == "postgresql://u@h/db"
    assert _sync_url("postgresql+psycopg://u@h/db") == "postgresql://u@h/db"
    assert _sync_url("postgresql://u@h/db") == "postgresql://u@h/db"


def test_sqlite_url_for_pgloader_strips_async_driver() -> None:
    assert _sqlite_url_for_pgloader("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"
    assert _sqlite_url_for_pgloader("sqlite:///already.db") == "sqlite:///already.db"
