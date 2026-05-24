from __future__ import annotations

import hashlib
from pathlib import Path


class FilesystemImageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, ext: str) -> str:
        digest = hashlib.sha256(content).hexdigest()[:16]
        path = Path(digest[:2]) / f"{digest[2:]}.{ext}"
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        return path.as_posix()

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


class S3ImageStore:
    """v1.5 implementation stub for S3-compatible object storage."""

    def put(self, content: bytes, ext: str) -> str:
        del content, ext
        raise NotImplementedError("S3ImageStore is v1.5 - see docs/MIGRATION.md")

    def get(self, path: str) -> bytes:
        del path
        raise NotImplementedError("S3ImageStore is v1.5 - see docs/MIGRATION.md")

