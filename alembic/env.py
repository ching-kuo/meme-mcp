"""Alembic migration environment for meme-mcp.

The configured `DATABASE_URL` may be async-flavoured (`sqlite+aiosqlite://...`,
`postgresql+asyncpg://...`); Alembic's `command.upgrade` is sync-only, so this env
rewrites the URL to the matching sync driver before running migrations.
"""

from __future__ import annotations

import os

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config


def _sync_url(url: str) -> str:
    """Rewrite async-driver URLs to their sync counterparts.

    `aiosqlite` -> stdlib sqlite3; `asyncpg` -> psycopg.
    """
    if url.startswith("sqlite+aiosqlite:///"):
        return url.replace("sqlite+aiosqlite:///", "sqlite:///")
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return url


database_url = os.environ.get("MEMEMCP_ALEMBIC_URL") or os.environ.get("DATABASE_URL")
if database_url is not None:
    config.set_main_option("sqlalchemy.url", _sync_url(database_url))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
