from __future__ import annotations

from typing import Protocol

from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.db.vectors import EmbeddingMetaStore, VectorStore
from meme_mcp.embeddings.client import EmbeddingClient, embedding_text_hash


class TemplateEmbedder(Protocol):
    model: str

    def embed_template(self, metadata: dict[str, object]) -> list[float]: ...


def run_fixture_reindex(metadata_rows: list[dict[str, object]]) -> list[str]:
    return [embedding_text_hash(row) for row in metadata_rows]


def reindex_embeddings(
    templates: SQLiteTemplateRepository,
    vectors: VectorStore,
    embedder: TemplateEmbedder,
    meta: EmbeddingMetaStore | None = None,
    *,
    force: bool = False,
) -> int:
    # --force is the documented remediation for the startup guard (U7): it must
    # purge stale state (old-model meta rows, orphan vectors from deleted
    # templates) that a plain upsert pass would leave latched in the guard.
    if force:
        vectors.clear()
        if meta is not None:
            meta.clear()
    count = 0
    for row in templates.list_rows():
        vector = embedder.embed_template(row.metadata)
        vectors.upsert(row.template_id, vector)
        if meta is not None:
            meta.record(
                row.template_id,
                model=embedder.model,
                text_hash=embedding_text_hash(row.metadata),
                dimensions=len(vector),
            )
        count += 1
    return count


def make_embedder(model: str, api_key: str, base_url: str) -> EmbeddingClient:
    return EmbeddingClient(model=model, api_key=api_key, base_url=base_url)
