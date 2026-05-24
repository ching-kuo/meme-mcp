from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from meme_mcp.vlm.sanitize import flag_anomalies


@dataclass(frozen=True)
class EnrichmentResult:
    status: Literal["success", "timeout", "error", "schema_invalid"]
    metadata: dict[str, Any] | None
    raw_response: str | None
    suspect_flags: list[str]


class VLMClient:
    """OpenAI-compatible VLM client placeholder.

    The network call is intentionally not wired in this test-first slice; callers can inject
    metadata through `from_metadata` while the real provider adapter is added behind this contract.
    """

    def from_metadata(self, metadata: dict[str, Any]) -> EnrichmentResult:
        return EnrichmentResult("success", metadata, None, flag_anomalies(metadata))

