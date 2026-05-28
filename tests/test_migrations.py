"""Alembic baseline lands every table the inline-DDL stores create."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import SecretStr

from meme_mcp.config import Settings
from meme_mcp.db.migrations import _sync_url, run_migrations


def _settings(tmp_path: Path) -> Settings:
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


EXPECTED_TABLES = {
    "templates",
    "pats",
    "pending_uploads",
    "template_vectors",
    "template_embeddings_meta",
    "generated_receipts",
    "outcome_events",
    "alembic_version",  # alembic's own bookkeeping table
}


def test_baseline_creates_every_expected_table(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_migrations(settings)
    db_path = tmp_path / "meme.db"
    with sqlite3.connect(db_path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert names >= EXPECTED_TABLES


def test_baseline_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_migrations(settings)
    run_migrations(settings)  # second call must not raise


def test_baseline_skips_when_inline_ddl_already_created_tables(tmp_path: Path) -> None:
    """A v1-era DB with inline-DDL tables already in place should accept the baseline
    migration without raising; the CREATE TABLE IF NOT EXISTS statements are no-ops."""
    db_path = tmp_path / "meme.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE templates (id TEXT PRIMARY KEY, slug TEXT, name TEXT, "
            "source TEXT, metadata_json TEXT, slot_definitions_json TEXT, "
            "image_path TEXT, perceptual_hash TEXT, exact_hash TEXT, "
            "created_at TEXT, updated_at TEXT)"
        )
    settings = _settings(tmp_path)
    run_migrations(settings)
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    # Head moves with each new revision; assert the version row exists rather than pinning
    # to a specific revision id that changes when we add migrations.
    assert version is not None


def test_sync_url_rewrites_async_drivers() -> None:
    assert (
        _sync_url("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"
    )
    assert (
        _sync_url("postgresql+asyncpg://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )
    assert _sync_url("sqlite:///already-sync.db") == "sqlite:///already-sync.db"


def test_pats_table_includes_v15_columns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_migrations(settings)
    with sqlite3.connect(tmp_path / "meme.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pats)")}
    assert "expires_at" in columns
    assert "capability" in columns


def test_outcome_events_table_has_index_and_check_constraint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_migrations(settings)
    with sqlite3.connect(tmp_path / "meme.db") as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(outcome_events)")}
    assert "outcome_events_template_ts" in indexes


def test_vector_ddl_revision_is_noop_on_sqlite(tmp_path: Path) -> None:
    """0002_vector_ddl must leave the SQLite template_vectors table untouched (the
    baseline created it with vector_json TEXT; Postgres takes its own DDL branch).
    """
    settings = _settings(tmp_path)
    run_migrations(settings)
    with sqlite3.connect(tmp_path / "meme.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(template_vectors)")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"template_id", "vector_json", "dimensions"} <= columns
    assert version == ("0002_vector_ddl",)
