import pytest

from meme_mcp.config import ConfigError
from meme_mcp.db.vectors import InMemoryVectorStore, PgVectorStore, make_vector_store


def test_in_memory_vector_store_searches_by_cosine_similarity() -> None:
    store = InMemoryVectorStore()
    store.upsert("a", [1.0, 0.0])
    store.upsert("b", [0.0, 1.0])
    assert store.search([0.9, 0.1], top_k=1) == [("a", pytest.approx(0.993883734))]


def test_pg_vector_store_is_v15_stub() -> None:
    with pytest.raises(NotImplementedError, match="v1.5"):
        PgVectorStore().upsert("x", [1.0])


def test_postgres_factory_rejects_at_startup() -> None:
    with pytest.raises(ConfigError, match="v1.5"):
        make_vector_store("postgresql+asyncpg://example")

