from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai import OpenAI

from meme_mcp.vlm.sanitize import flag_anomalies


@dataclass(frozen=True)
class EnrichmentResult:
    status: Literal["success", "timeout", "error", "schema_invalid"]
    metadata: dict[str, Any] | None
    raw_response: str | None
    suspect_flags: list[str]


class VLMProvider(Protocol):
    chat: Any


METADATA_TOOL = {
    "type": "function",
    "function": {
        "name": "record_template_metadata",
        "description": "Record structured meme template metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "emotion": {"type": "string"},
                "usage_context": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "format": {"type": "string", "enum": ["static"]},
                "slot_definitions": {"type": "array", "items": {"type": "object"}},
            },
            "required": [
                "name",
                "description",
                "emotion",
                "usage_context",
                "tags",
                "format",
                "slot_definitions",
            ],
            "additionalProperties": False,
        },
    },
}


class VLMClient:
    def __init__(
        self,
        model: str,
        provider: VLMProvider | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.provider = provider or OpenAI(api_key=api_key, base_url=base_url)

    def enrich_template(
        self,
        image_bytes: bytes,
        title_hint: str | None = None,
    ) -> EnrichmentResult:
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
        prompt = "Describe this meme template for private retrieval."
        if title_hint:
            prompt += f" Title hint: {title_hint}"
        try:
            chat: Any = self.provider.chat
            response = chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                tools=[METADATA_TOOL],
                tool_choice={"type": "function", "function": {"name": "record_template_metadata"}},
                timeout=60,
            )
            raw_args = response.choices[0].message.tool_calls[0].function.arguments
            metadata = json.loads(str(raw_args))
        except TimeoutError:
            return EnrichmentResult("timeout", None, None, [])
        except (AttributeError, IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return EnrichmentResult("schema_invalid", None, str(exc), [])
        return self.from_metadata(metadata)

    def from_metadata(self, metadata: dict[str, Any]) -> EnrichmentResult:
        return EnrichmentResult("success", metadata, None, flag_anomalies(metadata))
