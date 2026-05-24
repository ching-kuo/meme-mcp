from __future__ import annotations

import hashlib
from typing import Any


def embedding_text(metadata: dict[str, Any]) -> str:
    tags = metadata.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return " | ".join(
        [
            str(metadata.get("description", "")),
            str(metadata.get("emotion", "")),
            str(metadata.get("usage_context", "")),
            " ".join(sorted(str(tag) for tag in tags)),
        ]
    )


def embedding_text_hash(metadata: dict[str, Any]) -> str:
    return hashlib.sha256(embedding_text(metadata).encode()).hexdigest()[:16]


class EmbeddingClient:
    def embed_query(self, query: str) -> list[float]:
        digest = hashlib.sha256(query.encode()).digest()
        return [byte / 255 for byte in digest[:8]]

