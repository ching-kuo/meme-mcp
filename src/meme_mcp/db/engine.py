from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    database_url: str


def dialect_name(database_url: str) -> str:
    if database_url.startswith("postgresql+"):
        return "postgresql"
    if database_url.startswith("sqlite+"):
        return "sqlite"
    return "unknown"

