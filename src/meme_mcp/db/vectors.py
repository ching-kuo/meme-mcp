from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Protocol

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
    """pgvector-backed semantic search.

    Requires the `postgres` extra (psycopg + pgvector). Connection strings use the
    sync psycopg driver (`postgresql+psycopg://…` or `postgresql://…`); aiosqlite-flavoured
    or asyncpg-flavoured URLs are rewritten to the sync driver before the store opens a
    connection, mirroring the same rewrite Alembic's env.py applies.
    """

    def __init__(self, database_url: str, dimensions: int = 1536) -> None:
        try:
            import psycopg  # type: ignore[import-not-found]
            from pgvector.psycopg import register_vector  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigError(
                "PgVectorStore requires the 'postgres' extra (psycopg + pgvector)"
            ) from exc
        self._psycopg = psycopg
        self._register_vector = register_vector
        self.dimensions = dimensions
        self.database_url = _to_psycopg_url(database_url)

    def _connect(self) -> Any:
        conn = self._psycopg.connect(self.database_url)
        self._register_vector(conn)
        return conn

    def upsert(self, template_id: str, vector: list[float]) -> None:
        if len(vector) != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {len(vector)}")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO template_vectors (template_id, embedding) VALUES (%s, %s) "
                "ON CONFLICT (template_id) DO UPDATE SET embedding = EXCLUDED.embedding",
                (template_id, vector),
            )

    def search(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]:
        if len(query_vector) != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {len(query_vector)}")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT template_id, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM template_vectors ORDER BY embedding <=> %s::vector LIMIT %s",
                (query_vector, query_vector, top_k),
            )
            rows = cur.fetchall()
        return [(str(template_id), float(similarity)) for template_id, similarity in rows]


def _to_psycopg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://")
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://")
    return url


class EmbeddingMetaStore:
    """Tracks which embedding model produced each stored vector.

    Used by the startup guard to refuse mixing dimensions/models in one corpus.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS template_embeddings_meta (
                    template_id TEXT PRIMARY KEY,
                    embedding_model TEXT NOT NULL,
                    embedded_text_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(
        self,
        template_id: str,
        *,
        model: str,
        text_hash: str,
        dimensions: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO template_embeddings_meta
                    (template_id, embedding_model, embedded_text_hash, dimensions)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    embedding_model = excluded.embedding_model,
                    embedded_text_hash = excluded.embedded_text_hash,
                    dimensions = excluded.dimensions
                """,
                (template_id, model, text_hash, dimensions),
            )

    def models_in_use(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT embedding_model FROM template_embeddings_meta"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def orphan_vector_count(self) -> int:
        """Vectors stored without corresponding meta — pre-guard installs.

        Returns 0 if the vector table does not yet exist.
        """
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='template_vectors'"
            ).fetchone()
            if existing is None:
                return 0
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM template_vectors v
                LEFT JOIN template_embeddings_meta m ON m.template_id = v.template_id
                WHERE m.template_id IS NULL
                """
            ).fetchone()
        return int(row[0]) if row else 0


def make_vector_store(database_url: str) -> VectorStore:
    if database_url.startswith("postgresql"):
        return PgVectorStore(database_url)
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
