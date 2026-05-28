"""Alembic upgrade-to-head wrapper.

Stores still create tables inline via `CREATE TABLE IF NOT EXISTS` as defence-in-depth
for direct-test fixtures that bypass app startup. The Alembic baseline captures the
same shape so a fresh DB walked through migrations matches the inline schema exactly.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command
from meme_mcp.config import Settings

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url(database_url))
    return cfg


def _sync_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite:///"):
        return url.replace("sqlite+aiosqlite:///", "sqlite:///")
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return url


def run_migrations(settings: Settings) -> None:
    cfg = _alembic_config(settings.database_url)
    command.upgrade(cfg, "head")
