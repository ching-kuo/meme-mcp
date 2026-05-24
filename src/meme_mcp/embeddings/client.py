from __future__ import annotations

import hashlib
from typing import Any, Protocol

from openai import OpenAI


class EmbeddingProvider(Protocol):
    embeddings: Any


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
    def __init__(
        self,
        model: str = "text-embedding-3-small",
        provider: EmbeddingProvider | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.provider = provider or OpenAI(api_key=api_key, base_url=base_url)

    def embed_template(self, metadata: dict[str, Any]) -> list[float]:
        response = self.provider.embeddings.create(
            model=self.model,
            input=[embedding_text(metadata)],
        )
        return [float(value) for value in response.data[0].embedding]

    def embed_query(self, query: str) -> list[float]:
        response = self.provider.embeddings.create(model=self.model, input=[query])
        return [float(value) for value in response.data[0].embedding]
