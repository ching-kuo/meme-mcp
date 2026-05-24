from __future__ import annotations

from typing import Any

EXPECTED_TOOLS = {"find", "generate"}


def tool_schemas() -> dict[str, dict[str, Any]]:
    return {
        "find": {
            "description": "Find 3-5 ranked meme templates for a query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "filters": {"type": "object", "additionalProperties": True},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        "generate": {
            "description": "Render a selected meme template and return a receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "slot_fills": {"type": "array", "items": {"type": "string"}},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["template_id", "slot_fills"],
                "additionalProperties": False,
            },
        },
    }

