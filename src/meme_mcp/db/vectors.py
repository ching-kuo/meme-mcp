from __future__ import annotations

import math
from typing import Protocol

from meme_mcp.config import ConfigError


class VectorStore(Protocol):
    def upsert(self, template_id: str, vector: list[float]) -> None: ...

    def search(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]: ...


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.vectors: dict[str, list[float]] = {}

    def upsert(self, template_id: str, vector: list[float]) -> None:
        self.vectors[template_id] = vector

    def search(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]:
        scored = [
            (template_id, _cosine(query_vector, vector))
            for template_id, vector in self.vectors.items()
        ]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]


class PgVectorStore:
    """v1.5 implementation stub for pgvector-backed semantic search."""

    def upsert(self, template_id: str, vector: list[float]) -> None:
        del template_id, vector
        raise NotImplementedError("PgVectorStore is v1.5 - see docs/MIGRATION.md")

    def search(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]:
        del query_vector, top_k
        raise NotImplementedError("PgVectorStore is v1.5 - see docs/MIGRATION.md")


def make_vector_store(database_url: str) -> VectorStore:
    if database_url.startswith("postgresql+"):
        raise ConfigError("Postgres vector backend is v1.5 - only SQLite/in-memory is v1")
    return InMemoryVectorStore()


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)

