from __future__ import annotations

import pytest

from meme_mcp.db.vectors import EmbeddingMetaStore
from meme_mcp.embeddings.client import validate_embedding_model
from meme_mcp.errors import ErrorCode, MemeMCPError


def test_validate_embedding_model_accepts_matching_history(tmp_path) -> None:
    store = EmbeddingMetaStore(tmp_path / "meta.db")
    store.record("template-1", model="text-embedding-3-small", text_hash="abc", dimensions=1536)

    validate_embedding_model(store, "text-embedding-3-small")


def test_validate_embedding_model_rejects_drift(tmp_path) -> None:
    store = EmbeddingMetaStore(tmp_path / "meta.db")
    store.record("template-1", model="text-embedding-3-small", text_hash="abc", dimensions=1536)

    with pytest.raises(MemeMCPError) as info:
        validate_embedding_model(store, "text-embedding-3-large")

    assert info.value.error_code is ErrorCode.INTERNAL_ERROR


def test_validate_embedding_model_skips_empty_history(tmp_path) -> None:
    store = EmbeddingMetaStore(tmp_path / "meta.db")
    validate_embedding_model(store, "anything")


def test_validate_embedding_model_rejects_orphan_vectors(tmp_path) -> None:
    from meme_mcp.db.vectors import SQLiteVecStore

    db = tmp_path / "meta.db"
    EmbeddingMetaStore(db)
    vectors = SQLiteVecStore(db, dimensions=3)
    vectors.upsert("orphan", [0.1, 0.2, 0.3])

    with pytest.raises(MemeMCPError) as info:
        validate_embedding_model(EmbeddingMetaStore(db), "any-model")
    assert "orphan" in str(info.value.errors).lower() or "stored vectors" in str(info.value.errors)
