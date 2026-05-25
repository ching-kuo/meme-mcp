from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Protocol

from openai import OpenAI

from meme_mcp.errors import ErrorCode, MemeMCPError

if TYPE_CHECKING:
    from meme_mcp.db.vectors import EmbeddingMetaStore


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


def validate_embedding_model(
    meta_store: EmbeddingMetaStore,
    configured_model: str,
) -> None:
    """Refuse startup if persisted vectors were produced by a different model.

    Plan U8: mixing dimensions/models in one corpus produces silent retrieval garbage.
    Catches both recorded drift AND orphan vectors that predate the meta-store guard.
    Remediation is `meme-mcp reindex-embeddings --force`.
    """
    in_use = meta_store.models_in_use()
    drift = in_use - {configured_model}
    if drift:
        raise MemeMCPError(
            ErrorCode.INTERNAL_ERROR,
            [
                {
                    "field": "embedding_model",
                    "reason": (
                        f"corpus has embeddings from {sorted(drift)!r}; "
                        f"configured model is {configured_model!r} - "
                        "run `meme-mcp reindex-embeddings --force`"
                    ),
                }
            ],
        )
    orphan = meta_store.orphan_vector_count()
    if orphan:
        raise MemeMCPError(
            ErrorCode.INTERNAL_ERROR,
            [
                {
                    "field": "embedding_model",
                    "reason": (
                        f"{orphan} stored vectors have no recorded model - "
                        "run `meme-mcp reindex-embeddings --force`"
                    ),
                }
            ],
        )
