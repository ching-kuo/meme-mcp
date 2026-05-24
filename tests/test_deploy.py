from __future__ import annotations

from pathlib import Path


def test_dockerfile_uses_configured_app_factory() -> None:
    dockerfile = Path("deploy/Dockerfile").read_text(encoding="utf-8")
    assert "meme_mcp.app:create_configured_app" in dockerfile
