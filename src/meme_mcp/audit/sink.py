from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from meme_mcp.audit.events import MemeEvent

LOGGER = logging.getLogger(__name__)


class JsonlAuditSink:
    def __init__(self, path: str | Path, max_bytes: int = 100 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: MemeEvent) -> None:
        try:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_jsonable(), sort_keys=True) + "\n")
            os.chmod(self.path, 0o600)
        except OSError:
            LOGGER.exception("audit write failed")

    def _rotate_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        rotated = self.path.with_suffix(self.path.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        self.path.rename(rotated)
