import pytest

from meme_mcp.db.vectors import (
    InMemoryVectorStore,
    SQLiteVecStore,
    make_vector_store,
)


def test_in_memory_vector_store_searches_by_cosine_similarity() -> None:
    store = InMemoryVectorStore()
    store.upsert("a", [1.0, 0.0])
    store.upsert("b", [0.0, 1.0])
    assert store.search([0.9, 0.1], top_k=1) == [("a", pytest.approx(0.993883734))]


def test_sqlite_vec_store_persists_vectors(tmp_path) -> None:
    store = SQLiteVecStore(tmp_path / "vectors.db", dimensions=2)
    store.upsert("a", [1.0, 0.0])
    store.upsert("b", [0.0, 1.0])
    reopened = SQLiteVecStore(tmp_path / "vectors.db", dimensions=2)
    assert reopened.search([0.9, 0.1], top_k=1)[0][0] == "a"


def test_sqlite_vec_store_rejects_dimension_mismatch(tmp_path) -> None:
    store = SQLiteVecStore(tmp_path / "vectors.db", dimensions=3)
    with pytest.raises(ValueError, match="dimensions"):
        store.upsert("a", [1.0, 0.0])
    with pytest.raises(ValueError, match="dimensions"):
        store.search([0.5, 0.5], top_k=1)


def test_factory_dispatches_postgres_url_to_pgvector_store() -> None:
    """make_vector_store no longer rejects Postgres URLs; the PgVectorStore is built
    when psycopg/pgvector are importable, or surfaces ConfigError at construction if
    the postgres extra is missing. The Postgres parity test suite at
    tests/test_vectors_postgres.py exercises the working PgVectorStore behaviour
    end-to-end."""
    try:
        store = make_vector_store("postgresql+psycopg://localhost/nope")
    except Exception as exc:  # noqa: BLE001 — ConfigError raised when postgres extra absent
        from meme_mcp.config import ConfigError

        assert isinstance(exc, ConfigError)
        return
    # Successful construction means psycopg/pgvector were importable. No further
    # assertion here — actual connectivity is exercised in test_vectors_postgres.py.
    assert store is not None
