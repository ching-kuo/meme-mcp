from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineConfig:
    database_url: str


def dialect_name(database_url: str) -> str:
    if database_url.startswith("postgresql+"):
        return "postgresql"
    if database_url.startswith("sqlite+"):
        return "sqlite"
    return "unknown"


def sqlite_path(database_url: str, fallback: Path) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite+aiosqlite:///"):
        return Path(database_url.removeprefix("sqlite+aiosqlite:///"))
    return fallback

