from __future__ import annotations

import hashlib
import math
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
    parts = [
        str(metadata.get("description", "")),
        str(metadata.get("emotion", "")),
        str(metadata.get("usage_context", "")),
        " ".join(sorted(str(tag) for tag in tags)),
    ]
    locales = metadata.get("locales")
    if isinstance(locales, dict):
        for block in locales.values():
            if not isinstance(block, dict):
                continue
            locale_tags = block.get("tags", [])
            if not isinstance(locale_tags, list):
                locale_tags = []
            parts.extend(
                [
                    str(block.get("description", "")),
                    str(block.get("emotion", "")),
                    str(block.get("usage_context", "")),
                    " ".join(sorted(str(tag) for tag in locale_tags)),
                ]
            )
    return " | ".join(parts)


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
        return _l2_normalized([float(value) for value in response.data[0].embedding])

    def embed_query(self, query: str) -> list[float]:
        response = self.provider.embeddings.create(model=self.model, input=[query])
        return _l2_normalized([float(value) for value in response.data[0].embedding])


def _l2_normalized(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def validate_embedding_model(
    meta_store: EmbeddingMetaStore,
    configured_model: str,
    configured_dimensions: int | None = None,
) -> None:
    """Refuse startup if persisted vectors were produced by a different model.

    Plan U7: mixing dimensions/models in one corpus produces silent retrieval garbage.
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
    if configured_dimensions is not None:
        dimensions = meta_store.dimensions_in_use()
        dimension_drift = dimensions - {configured_dimensions}
        if dimension_drift:
            raise MemeMCPError(
                ErrorCode.INTERNAL_ERROR,
                [
                    {
                        "field": "embedding_dimensions",
                        "reason": (
                            f"corpus has embedding dimensions {sorted(dimensions)!r}; "
                            f"configured dimensions are {configured_dimensions!r} - "
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
