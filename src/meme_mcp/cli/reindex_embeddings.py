from __future__ import annotations

from typing import Protocol

from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.db.vectors import VectorStore
from meme_mcp.embeddings.client import EmbeddingClient, embedding_text_hash


class TemplateEmbedder(Protocol):
    def embed_template(self, metadata: dict[str, object]) -> list[float]: ...


def run_fixture_reindex(metadata_rows: list[dict[str, object]]) -> list[str]:
    return [embedding_text_hash(row) for row in metadata_rows]


def reindex_embeddings(
    templates: SQLiteTemplateRepository,
    vectors: VectorStore,
    embedder: TemplateEmbedder,
) -> int:
    count = 0
    for row in templates.list_rows():
        vectors.upsert(row.template_id, embedder.embed_template(row.metadata))
        count += 1
    return count


def make_embedder(model: str, api_key: str, base_url: str) -> EmbeddingClient:
    return EmbeddingClient(model=model, api_key=api_key, base_url=base_url)
