from __future__ import annotations

from meme_mcp.embeddings.client import embedding_text_hash


def run_fixture_reindex(metadata_rows: list[dict[str, object]]) -> list[str]:
    return [embedding_text_hash(row) for row in metadata_rows]

