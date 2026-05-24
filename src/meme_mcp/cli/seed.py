from __future__ import annotations

from pathlib import Path

from meme_mcp.config import Settings
from meme_mcp.corpus.seed_memegen import seed_templates
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.rendering.image_store import FilesystemImageStore


def run(settings: Settings) -> int:
    db_path = _sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    count = seed_templates(
        SQLiteTemplateRepository(db_path),
        FilesystemImageStore(settings.image_store_fs_path),
    )
    print(f"seeded {count} templates")
    return 0


def _sqlite_path(database_url: str, fallback: Path) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite+aiosqlite:///"):
        return Path(database_url.removeprefix("sqlite+aiosqlite:///"))
    return fallback
