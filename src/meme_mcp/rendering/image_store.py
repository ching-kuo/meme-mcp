from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import portalocker

from meme_mcp.config import ConfigError


class ImageStore(Protocol):
    def put(self, content: bytes, ext: str) -> str: ...

    def get(self, path: str) -> bytes: ...


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

    def path_for_hash(self, rendered_hash: str, ext: str = "png") -> Path:
        return self.root / rendered_hash[:2] / f"{rendered_hash[2:]}.{ext}"

    def size_of(self, rendered_hash: str, ext: str = "png") -> int:
        path = self.path_for_hash(rendered_hash, ext)
        if not path.exists():
            return 0
        return path.stat().st_size

    def delete(self, rendered_hash: str, ext: str = "png") -> bool:
        path = self.path_for_hash(rendered_hash, ext)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    @contextmanager
    def shard_lock(self, rendered_hash: str):  # type: ignore[no-untyped-def]
        """Per-shard advisory lock used by GC to avoid racing with a concurrent put.

        FilesystemImageStore.put is naturally idempotent (stat-then-skip), so puts do
        not need the lock; only GC takes it before unlinking. The lock file lives in
        the shard directory and is created on demand.
        """
        shard = self.root / rendered_hash[:2]
        shard.mkdir(parents=True, exist_ok=True)
        lockfile = shard / ".gc.lock"
        with portalocker.Lock(str(lockfile), mode="a", timeout=10):
            yield


class S3ImageStore:
    """v1.5 implementation stub for S3-compatible object storage."""

    def put(self, content: bytes, ext: str) -> str:
        del content, ext
        raise NotImplementedError("S3ImageStore is v1.5 - see docs/MIGRATION.md")

    def get(self, path: str) -> bytes:
        del path
        raise NotImplementedError("S3ImageStore is v1.5 - see docs/MIGRATION.md")


def make_image_store(backend: str, fs_path: str) -> ImageStore:
    """Factory dispatching by `backend`. Returns the Protocol so callers stay
    independent of the concrete implementation.
    """
    if backend == "filesystem":
        return FilesystemImageStore(fs_path)
    if backend == "s3":
        raise ConfigError(
            "S3 image store backend lands in U15; configure image_store_backend='filesystem'"
        )
    raise ConfigError(f"unknown image_store_backend: {backend!r}")

