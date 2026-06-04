"""U7: cross-lingual semantic recall wired into the repository search adapter.

The semantic layer lives in ``SQLiteTemplateRepository`` (composed around the
pure ``retrieval.search.search``). These tests exercise it with a deterministic
fake embedder + an in-memory/SQLite vector store -- no network -- plus the two
graceful-degradation classes (embedder raises; mixed-dimension store).

The real-endpoint zh-TW eval (recall@3 over a small fixture) is gated behind
``MEME_MCP_EMBEDDING_EVAL`` so CI never reaches Ollama; the fake-embedder AE4
assertion always runs.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.db.vectors import InMemoryVectorStore, SQLiteVecStore

# ---------------------------------------------------------------------------
# Fixtures: a tiny bilingual corpus + a deterministic fake embedder
# ---------------------------------------------------------------------------

# zh-TW eval set: query -> expected template_id. Used both by the fake-embedder
# AE4 assertion and (real endpoint) the env-gated recall@3 eval below. The point
# of AE4 is recall WITHOUT literal substring overlap: 「難過」 (sad) must reach
# the sad-reaction template even though no stored field contains 難過.
ZH_TW_EVAL: dict[str, str] = {
    "難過": "sad",
    "傷心": "sad",
    "想哭": "sad",
    "好笑": "happy",
    "開心": "happy",
    "高興": "happy",
    "生氣": "angry",
    "憤怒": "angry",
    "火大": "angry",
    "驚訝": "surprised",
    "嚇到": "surprised",
    "意外": "surprised",
}

# Deterministic emotion vectors (4-dim, unit-normalized). Each maps a cluster of
# zh-TW queries and one template to the same axis, so cosine recall is exact and
# offline.
_EMOTION_AXIS: dict[str, list[float]] = {
    "sad": [1.0, 0.0, 0.0, 0.0],
    "happy": [0.0, 1.0, 0.0, 0.0],
    "angry": [0.0, 0.0, 1.0, 0.0],
    "surprised": [0.0, 0.0, 0.0, 1.0],
}

_QUERY_AXIS: dict[str, str] = dict(ZH_TW_EVAL)


class FakeEmbedder:
    """Deterministic, offline embedder mapping eval queries/templates to axes.

    ``model`` exists so it can stand in for the reindex Protocol too. Unknown
    text maps to a neutral zero vector (cosine 0 against every axis).
    """

    model = "fake-emotion"
    dimensions = 4

    def embed_query(self, query: str) -> list[float]:
        axis = _QUERY_AXIS.get(query.strip())
        if axis is None:
            return [0.0, 0.0, 0.0, 0.0]
        return list(_EMOTION_AXIS[axis])

    def embed_template(self, metadata: dict[str, object]) -> list[float]:
        emotion = str(metadata.get("emotion", ""))
        return list(_EMOTION_AXIS.get(emotion, [0.0, 0.0, 0.0, 0.0]))


class RaisingEmbedder:
    """Stands in for an embedding endpoint that is down."""

    model = "raising"

    def embed_query(self, query: str) -> list[float]:
        raise RuntimeError("embedding endpoint unavailable")


_TEMPLATES: list[tuple[str, str, str, dict[str, object]]] = [
    (
        "sad",
        "sad-reaction",
        "Sad Reaction",
        {
            "name": "Sad Reaction",
            "description": "a forlorn face after bad news",
            "emotion": "sad",
            "usage_context": "consoling a friend",
            "tags": ["reaction"],
            "format": "static",
        },
    ),
    (
        "happy",
        "happy-dance",
        "Happy Dance",
        {
            "name": "Happy Dance",
            "description": "an ecstatic celebratory dance",
            "emotion": "happy",
            "usage_context": "celebrating a win",
            "tags": ["reaction"],
            "format": "static",
        },
    ),
    (
        "angry",
        "table-flip",
        "Table Flip",
        {
            "name": "Table Flip",
            "description": "flipping a table in rage",
            "emotion": "angry",
            "usage_context": "venting frustration",
            "tags": ["reaction"],
            "format": "static",
        },
    ),
    (
        "surprised",
        "shocked-pikachu",
        "Shocked Pikachu",
        {
            "name": "Shocked Pikachu",
            "description": "an open-mouthed shocked face",
            "emotion": "surprised",
            "usage_context": "feigning surprise at an obvious outcome",
            "tags": ["reaction"],
            "format": "static",
        },
    ),
]


def _seed(repo: SQLiteTemplateRepository) -> None:
    for template_id, slug, name, metadata in _TEMPLATES:
        repo.upsert(
            TemplateCreate(
                template_id=template_id,
                slug=slug,
                name=name,
                source="friend",
                metadata=metadata,
                slot_definitions=[{"name": "top", "position": "top"}],
                image_path=f"{template_id}.png",
                perceptual_hash="0" * 16,
                exact_hash=template_id.ljust(64, "a"),
            )
        )


def _index(embedder: FakeEmbedder, store: InMemoryVectorStore) -> None:
    for template_id, _slug, _name, metadata in _TEMPLATES:
        store.upsert(template_id, embedder.embed_template(metadata))


# ---------------------------------------------------------------------------
# AE4: semantic recall without literal substring overlap (always runs in CI)
# ---------------------------------------------------------------------------


def test_ae4_sad_query_recalls_sad_template_via_semantics(tmp_path) -> None:
    embedder = FakeEmbedder()
    store = InMemoryVectorStore()
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)
    _index(embedder, store)

    # No stored field on any template contains the literal query 難過 (sad), so a
    # purely lexical search returns nothing -- semantic recall is the only path.
    lexical_only = SQLiteTemplateRepository(tmp_path / "meme.db")
    assert lexical_only.search("難過") == []

    results = repo.search("難過")

    assert results, "semantic layer should surface the sad template"
    assert results[0].template_id == "sad"
    assert "semantic" in results[0].matched_fields


def test_semantic_layer_respects_emotion_clusters(tmp_path) -> None:
    embedder = FakeEmbedder()
    store = InMemoryVectorStore()
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)
    _index(embedder, store)

    for query, expected in ZH_TW_EVAL.items():
        top = repo.search(query)
        assert top, f"no result for {query!r}"
        assert top[0].template_id == expected, f"{query!r} -> {top[0].template_id!r}"


def test_results_never_exceed_top_k_cap(tmp_path) -> None:
    embedder = FakeEmbedder()
    store = InMemoryVectorStore()
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)
    # Map every template onto the same axis so a single query semantically hits
    # all of them; the cap (min(top_k, 5)) must still hold.
    for template_id, _slug, _name, _metadata in _TEMPLATES:
        store.upsert(template_id, list(_EMOTION_AXIS["sad"]))

    assert len(repo.search("難過", top_k=2)) <= 2
    assert len(repo.search("難過", top_k=10)) <= 5


# ---------------------------------------------------------------------------
# Graceful degradation: embedder down / mixed-dimension store
# ---------------------------------------------------------------------------


def test_embedder_failure_degrades_to_lexical(tmp_path) -> None:
    store = InMemoryVectorStore()
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=RaisingEmbedder(), vector_store=store
    )
    _seed(repo)

    # The query matches the sad template lexically (description/emotion); the
    # raising embedder must not propagate -- lexical result stands.
    results = repo.search("forlorn")

    assert results
    assert results[0].template_id == "sad"
    assert "semantic" not in results[0].matched_fields


def test_mixed_dimension_store_degrades_to_lexical(tmp_path) -> None:
    embedder = FakeEmbedder()
    # SQLiteVecStore declares 4 dims; the embedder yields 4-dim query vectors, but
    # we plant a 3-dim row so _cosine's strict zip raises during scoring.
    store = SQLiteVecStore(tmp_path / "vectors.db", dimensions=4)
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)

    with sqlite3.connect(tmp_path / "vectors.db") as conn:
        conn.execute(
            "INSERT INTO template_vectors (template_id, vector_json, dimensions) "
            "VALUES (?, ?, ?)",
            ("sad", json.dumps([1.0, 0.0, 0.0]), 3),
        )

    results = repo.search("forlorn")

    assert results
    assert results[0].template_id == "sad"
    assert "semantic" not in results[0].matched_fields


def test_query_vector_length_mismatch_degrades(tmp_path) -> None:
    # Store expects 8 dims; the fake embedder emits 4-dim query vectors, so
    # SQLiteVecStore.search raises ValueError -- which must degrade, not 500.
    embedder = FakeEmbedder()
    store = SQLiteVecStore(tmp_path / "vectors.db", dimensions=8)
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)

    results = repo.search("forlorn")

    assert results
    assert "semantic" not in results[0].matched_fields


# ---------------------------------------------------------------------------
# English regression: semantic is additive, never destructive
# ---------------------------------------------------------------------------


def test_english_lexical_ranking_unchanged_by_semantic_layer(tmp_path) -> None:
    embedder = FakeEmbedder()
    store = InMemoryVectorStore()
    _index(embedder, store)

    wired = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(wired)
    plain = SQLiteTemplateRepository(tmp_path / "meme.db")

    # An English query whose terms only the sad template carries; the fake
    # embedder maps unknown English to the zero vector (cosine 0 everywhere), so
    # the semantic layer adds nothing and the ranking is identical to lexical.
    for query in ("forlorn", "celebratory dance", "rage", "shocked"):
        wired_ids = [c.template_id for c in wired.search(query)]
        plain_ids = [c.template_id for c in plain.search(query)]
        assert wired_ids == plain_ids, f"ranking drifted for {query!r}"


# ---------------------------------------------------------------------------
# Real-endpoint eval: gated behind an env var so CI never reaches Ollama
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEME_MCP_EMBEDDING_EVAL"),
    reason="set MEME_MCP_EMBEDDING_EVAL=1 to run the real qwen3 endpoint eval",
)
def test_zh_tw_eval_recall_at_3_real_endpoint(tmp_path) -> None:
    from meme_mcp.cli.reindex_embeddings import make_embedder, reindex_embeddings
    from meme_mcp.config import Settings
    from meme_mcp.db.vectors import EmbeddingMetaStore

    settings = Settings()  # type: ignore[call-arg]
    embedder = make_embedder(
        settings.embedding_model,
        settings.embedding_api_key.get_secret_value(),
        settings.embedding_base_url,
    )
    store = SQLiteVecStore(tmp_path / "vectors.db", dimensions=settings.embedding_dimensions)
    repo = SQLiteTemplateRepository(
        tmp_path / "meme.db", embedder=embedder, vector_store=store
    )
    _seed(repo)
    reindex_embeddings(repo, store, embedder, EmbeddingMetaStore(tmp_path / "meta.db"))

    hits = 0
    for query, expected in ZH_TW_EVAL.items():
        top3 = [c.template_id for c in repo.search(query, top_k=3)]
        if expected in top3:
            hits += 1
    recall = hits / len(ZH_TW_EVAL)
    # Recorded in the PR description; assert a non-trivial floor so a broken
    # endpoint/model swap is caught rather than silently scoring 0.
    assert recall >= 0.5, f"zh-TW recall@3 too low: {recall:.2f}"
