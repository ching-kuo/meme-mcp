import pytest

from meme_mcp.config import ConfigError
from meme_mcp.db.vectors import (
    InMemoryVectorStore,
    PgVectorStore,
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


def test_pg_vector_store_is_v15_stub() -> None:
    with pytest.raises(NotImplementedError, match="v1.5"):
        PgVectorStore().upsert("x", [1.0])


def test_postgres_factory_rejects_at_startup() -> None:
    with pytest.raises(ConfigError, match="v1.5"):
        make_vector_store("postgresql+asyncpg://example")
