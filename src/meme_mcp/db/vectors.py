from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
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


class SQLiteVecStore:
    """SQLite-backed vector store.

    This uses a portable table for deterministic tests and deployment fallback. The interface is
    intentionally compatible with the sqlite-vec path so the storage layer can switch to `vec0`
    query acceleration without changing retrieval callers.
    """

    def __init__(self, path: str | Path, dimensions: int = 1536) -> None:
        self.path = Path(path)
        self.dimensions = dimensions
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS template_vectors (
                    template_id TEXT PRIMARY KEY,
                    vector_json TEXT NOT NULL,
                    dimensions INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def upsert(self, template_id: str, vector: list[float]) -> None:
        if len(vector) != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {len(vector)}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO template_vectors (template_id, vector_json, dimensions)
                VALUES (?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    vector_json = excluded.vector_json,
                    dimensions = excluded.dimensions
                """,
                (template_id, json.dumps(vector), self.dimensions),
            )

    def search(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]:
        if len(query_vector) != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {len(query_vector)}")
        with self._connect() as conn:
            rows = conn.execute("SELECT template_id, vector_json FROM template_vectors").fetchall()
        scored = [
            (str(template_id), _cosine(query_vector, json.loads(str(vector_json))))
            for template_id, vector_json in rows
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
    if database_url.startswith("sqlite:///"):
        return SQLiteVecStore(Path(database_url.removeprefix("sqlite:///")))
    if database_url.startswith("sqlite+aiosqlite:///"):
        return SQLiteVecStore(Path(database_url.removeprefix("sqlite+aiosqlite:///")))
    return InMemoryVectorStore()


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
