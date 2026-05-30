from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import portalocker

from meme_mcp.config import ConfigError, Settings


class ImageStore(Protocol):
    def put(self, content: bytes, ext: str) -> str: ...

    def path_for(self, content: bytes, ext: str) -> str: ...

    def get(self, path: str) -> bytes: ...

    def delete(self, path: str) -> bool: ...


class FilesystemImageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, content: bytes, ext: str) -> str:
        """The content-addressed path put() would return, without writing.

        Lets a caller record the image_path (e.g. in a pending row) before the blob
        is written, so the reference is observable before the bytes land.
        """
        digest = hashlib.sha256(content).hexdigest()[:16]
        return (Path(digest[:2]) / f"{digest[2:]}.{ext}").as_posix()

    def put(self, content: bytes, ext: str) -> str:
        rel = self.path_for(content, ext)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        return rel

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def delete(self, path: str) -> bool:
        """Path-keyed delete, keyed on the `image_path` returned by `put`.

        Resolves `self.root / path` and refuses to unlink anything that escapes the
        store root (traversal guard), so a hostile `../../etc/passwd` returns False
        without touching the filesystem. An absent path is idempotent (returns False).
        """
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            return False
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False

    def path_for_hash(self, rendered_hash: str, ext: str = "png") -> Path:
        return self.root / rendered_hash[:2] / f"{rendered_hash[2:]}.{ext}"

    def size_of(self, rendered_hash: str, ext: str = "png") -> int:
        path = self.path_for_hash(rendered_hash, ext)
        if not path.exists():
            return 0
        return path.stat().st_size

    def delete_by_hash(self, rendered_hash: str, ext: str = "png") -> bool:
        """Hash-keyed delete used by the render-output GC (`gc_renders`).

        Distinct from the path-keyed `delete(path)` Protocol method: this resolves the
        blob from a content hash + extension, while `delete` takes the `image_path`
        string returned by `put`.
        """
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
    """Sync boto3-backed object storage for S3 and S3-compatible endpoints (MinIO, R2, B2).

    Content-addressed keys mirror FilesystemImageStore (`<aa>/<bb..>.ext`). `put` is
    idempotent via HeadObject-then-PutObject. `get` raises FileNotFoundError on NoSuchKey
    to match the filesystem store's `Path.read_bytes` failure surface.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        try:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]
            from botocore.exceptions import ClientError  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ConfigError("S3ImageStore requires the 's3' extra (boto3)") from exc
        self._client_error = ClientError
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )

    def path_for(self, content: bytes, ext: str) -> str:
        """The content-addressed key put() would return, without writing."""
        digest = hashlib.sha256(content).hexdigest()[:16]
        return f"{digest[:2]}/{digest[2:]}.{ext}"

    def put(self, content: bytes, ext: str) -> str:
        key = self.path_for(content, ext)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except self._client_error as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
            self.client.put_object(Bucket=self.bucket, Key=key, Body=content)
        return key

    def get(self, path: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=path)
        except self._client_error as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(path) from exc
            raise
        body = response["Body"].read()
        return bytes(body)

    def delete(self, path: str) -> bool:
        """Path-keyed delete via DeleteObject. S3 DeleteObject is idempotent: an absent
        key succeeds, so this returns True whenever the call completes without error.
        """
        self.client.delete_object(Bucket=self.bucket, Key=path)
        return True


def make_image_store(
    backend: str,
    *,
    fs_path: str | None = None,
    s3_endpoint: str | None = None,
    s3_bucket: str | None = None,
    s3_region: str | None = None,
    s3_access_key_id: str | None = None,
    s3_secret_access_key: str | None = None,
) -> ImageStore:
    """Factory dispatching by `backend`. Returns the Protocol so callers stay
    independent of the concrete implementation.
    """
    if backend == "filesystem":
        if fs_path is None:
            raise ConfigError("filesystem backend requires fs_path")
        return FilesystemImageStore(fs_path)
    if backend == "s3":
        missing = [
            name
            for name, value in (
                ("endpoint", s3_endpoint),
                ("bucket", s3_bucket),
                ("region", s3_region),
                ("access_key_id", s3_access_key_id),
                ("secret_access_key", s3_secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"s3 backend missing config: {', '.join(missing)}")
        return S3ImageStore(
            endpoint=s3_endpoint or "",
            bucket=s3_bucket or "",
            region=s3_region or "",
            access_key_id=s3_access_key_id or "",
            secret_access_key=s3_secret_access_key or "",
        )
    raise ConfigError(f"unknown image_store_backend: {backend!r}")


def make_image_store_from_settings(settings: Settings) -> ImageStore:
    """Build the image store from a Settings object.

    The single place that maps Settings to make_image_store's explicit kwargs, so
    create_app and the gc CLIs cannot drift in how they configure the backend (a
    mismatch would silently make a sweep no-op on S3).
    """
    return make_image_store(
        settings.image_store_backend,
        fs_path=settings.image_store_fs_path,
        s3_endpoint=settings.s3_endpoint,
        s3_bucket=settings.s3_bucket,
        s3_region=settings.s3_region,
        s3_access_key_id=(
            settings.s3_access_key_id.get_secret_value()
            if settings.s3_access_key_id is not None
            else None
        ),
        s3_secret_access_key=(
            settings.s3_secret_access_key.get_secret_value()
            if settings.s3_secret_access_key is not None
            else None
        ),
    )

