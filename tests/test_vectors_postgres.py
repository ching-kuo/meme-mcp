"""Postgres parity test suite for PgVectorStore.

Skips entirely when ``MEMEMCP_TEST_POSTGRES_URL`` is unset; configure it (and an
accessible Postgres+pgvector instance via ``deploy/docker-compose.test.yml``) to run.

The tests mirror the SQLite parity scenarios from ``tests/test_vectors.py`` so the
two backends share assertion shape.
"""

from __future__ import annotations

import os

import pytest

POSTGRES_URL = os.environ.get("MEMEMCP_TEST_POSTGRES_URL")

if not POSTGRES_URL:
    pytest.skip(
        "Set MEMEMCP_TEST_POSTGRES_URL=postgresql://user:pass@host/db to run the "
        "Postgres parity suite (see deploy/docker-compose.test.yml).",
        allow_module_level=True,
    )

try:
    import psycopg  # noqa: F401
    from pgvector.psycopg import register_vector  # noqa: F401
except ImportError:
    pytest.skip(
        "Install the 'postgres' extra to run this suite (uv sync --extra postgres).",
        allow_module_level=True,
    )

from meme_mcp.db.migrations import _sync_url  # noqa: E402
from meme_mcp.db.vectors import PgVectorStore  # noqa: E402


@pytest.fixture()
def pg_store():  # type: ignore[no-untyped-def]
    import psycopg as _psycopg

    sync_url = _sync_url(POSTGRES_URL)
    with _psycopg.connect(sync_url) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("DROP TABLE IF EXISTS template_vectors")
        conn.execute(
            "CREATE TABLE template_vectors ("
            "  template_id TEXT PRIMARY KEY,"
            "  embedding vector(4) NOT NULL"
            ")"
        )
        conn.commit()
    store = PgVectorStore(POSTGRES_URL, dimensions=4)
    yield store
    with _psycopg.connect(sync_url) as conn:
        conn.execute("DROP TABLE IF EXISTS template_vectors")
        conn.commit()


def test_empty_store_returns_no_results(pg_store: PgVectorStore) -> None:
    assert pg_store.search([1.0, 0.0, 0.0, 0.0], top_k=5) == []


def test_upsert_then_search_returns_inserted_template(pg_store: PgVectorStore) -> None:
    pg_store.upsert("drake", [1.0, 0.0, 0.0, 0.0])
    pg_store.upsert("orphan", [0.0, 1.0, 0.0, 0.0])
    results = pg_store.search([0.9, 0.1, 0.0, 0.0], top_k=2)
    assert results[0][0] == "drake"


def test_upsert_overwrites_previous_vector(pg_store: PgVectorStore) -> None:
    pg_store.upsert("drake", [1.0, 0.0, 0.0, 0.0])
    pg_store.upsert("drake", [0.0, 1.0, 0.0, 0.0])
    [(template_id, _)] = pg_store.search([0.0, 1.0, 0.0, 0.0], top_k=1)
    assert template_id == "drake"


def test_search_respects_top_k(pg_store: PgVectorStore) -> None:
    for slug, vec in (
        ("a", [1.0, 0.0, 0.0, 0.0]),
        ("b", [0.9, 0.1, 0.0, 0.0]),
        ("c", [0.8, 0.2, 0.0, 0.0]),
    ):
        pg_store.upsert(slug, vec)
    assert len(pg_store.search([1.0, 0.0, 0.0, 0.0], top_k=2)) == 2


def test_dimension_mismatch_raises(pg_store: PgVectorStore) -> None:
    with pytest.raises(ValueError, match="dimensions"):
        pg_store.upsert("a", [1.0, 0.0])  # 2 dims vs configured 4
    with pytest.raises(ValueError, match="dimensions"):
        pg_store.search([1.0, 0.0], top_k=1)
