from __future__ import annotations

from meme_mcp.config import Settings, validate_at_startup


def run() -> int:
    validate_at_startup(Settings())  # type: ignore[call-arg]
    return 0

