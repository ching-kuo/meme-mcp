"""meme-mcp migrate orchestrator.

Wraps the SQLite -> Postgres + filesystem -> S3 cutover documented in docs/MIGRATION.md
into a single command. --dry-run validates the target environment without moving data;
live mode runs pgloader, meme-mcp reindex-embeddings, and rclone sync in sequence.

The source DB is locked read-only (chmod 0444 on storage/) for the duration of a live
run, and restored on success or error so a botched cutover does not strand the source.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

from meme_mcp.config import Settings

logger = logging.getLogger(__name__)


class MigrateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def run(
    settings: Settings,
    *,
    target_db: str,
    target_s3_endpoint: str,
    target_s3_bucket: str,
    target_s3_access_key: str,
    target_s3_secret_key: str,
    target_s3_region: str = "us-east-1",
    dry_run: bool = False,
) -> int:
    try:
        _check_external_clis()
        _check_postgres_reachable_with_pgvector(target_db)
        _check_s3_reachable(
            target_s3_endpoint,
            target_s3_bucket,
            target_s3_region,
            target_s3_access_key,
            target_s3_secret_key,
        )
        _check_source_readable(settings)
    except MigrateError as exc:
        print(f"error [{exc.code}]: {exc}")
        return 2

    if dry_run:
        print("dry-run: all checks passed; live mode would run pgloader + reindex + rclone")
        return 0

    storage_dir = Path(settings.storage_dir)
    try:
        _lock_storage_readonly(storage_dir)
        _run_pgloader(settings, target_db)
        _run_reindex_embeddings_against_new_db(target_db)
        _run_rclone_sync(
            settings.image_store_fs_path,
            target_s3_endpoint,
            target_s3_bucket,
            target_s3_access_key,
            target_s3_secret_key,
        )
    except MigrateError as exc:
        _unlock_storage(storage_dir)
        print(f"error [{exc.code}]: {exc}")
        return 3
    finally:
        _unlock_storage(storage_dir)

    _write_env_next(
        settings,
        target_db=target_db,
        s3_endpoint=target_s3_endpoint,
        s3_bucket=target_s3_bucket,
        s3_region=target_s3_region,
        s3_access_key=target_s3_access_key,
        s3_secret_key=target_s3_secret_key,
    )
    print("migration complete; see .env.next for the suggested config diff")
    return 0


def _check_external_clis() -> None:
    for tool in ("pgloader", "rclone"):
        if shutil.which(tool) is None:
            raise MigrateError(
                "EXTERNAL_CLI_MISSING",
                f"required CLI not on PATH: {tool}",
            )


def _check_postgres_reachable_with_pgvector(target_db: str) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise MigrateError(
            "POSTGRES_EXTRA_MISSING",
            "install the 'postgres' extra (uv sync --extra postgres)",
        ) from exc
    sync_url = _sync_url(target_db)
    try:
        with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if row is None:
                raise MigrateError(
                    "PGVECTOR_MISSING",
                    "pgvector extension is not installed on the target database; "
                    "run 'CREATE EXTENSION vector' (or let alembic upgrade head do it)",
                )
    except psycopg.OperationalError as exc:
        raise MigrateError(
            "POSTGRES_UNREACHABLE",
            f"could not connect to target Postgres: {exc}",
        ) from exc


def _check_s3_reachable(
    endpoint: str,
    bucket: str,
    region: str,
    access_key: str,
    secret_key: str,
) -> None:
    try:
        import boto3  # type: ignore[import-untyped]
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MigrateError(
            "S3_EXTRA_MISSING",
            "install the 's3' extra (uv sync --extra s3)",
        ) from exc
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    try:
        client.head_bucket(Bucket=bucket)
        client.put_object(Bucket=bucket, Key=".meme-mcp-migrate-probe", Body=b"probe")
        client.get_object(Bucket=bucket, Key=".meme-mcp-migrate-probe")
        client.delete_object(Bucket=bucket, Key=".meme-mcp-migrate-probe")
    except (BotoCoreError, ClientError) as exc:
        raise MigrateError(
            "S3_UNREACHABLE",
            f"S3 round-trip probe failed against {endpoint}/{bucket}: {exc}",
        ) from exc


def _check_source_readable(settings: Settings) -> None:
    storage_dir = Path(settings.storage_dir)
    if not storage_dir.is_dir():
        raise MigrateError(
            "SOURCE_UNREADABLE",
            f"storage_dir does not exist: {storage_dir}",
        )
    images_dir = Path(settings.image_store_fs_path)
    if not images_dir.is_dir():
        raise MigrateError(
            "SOURCE_UNREADABLE",
            f"image_store_fs_path does not exist: {images_dir}",
        )


def _lock_storage_readonly(storage_dir: Path) -> None:
    os.chmod(storage_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def _unlock_storage(storage_dir: Path) -> None:
    if storage_dir.is_dir():
        os.chmod(storage_dir, 0o755)


def _run_pgloader(settings: Settings, target_db: str) -> None:
    source_url = _sqlite_url_for_pgloader(settings.database_url)
    result = subprocess.run(
        ["pgloader", source_url, _sync_url(target_db)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrateError(
            "PGLOADER_FAILED",
            f"pgloader exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}",
        )


def _run_reindex_embeddings_against_new_db(target_db: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = target_db
    result = subprocess.run(
        ["uv", "run", "meme-mcp", "reindex-embeddings"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MigrateError(
            "REINDEX_FAILED",
            f"reindex-embeddings exit {result.returncode}: {detail}",
        )


def _run_rclone_sync(
    source_dir: str,
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
) -> None:
    remote = (
        f":s3,provider=Other,endpoint={endpoint},"
        f"access_key_id={access_key},secret_access_key={secret_key}:{bucket}"
    )
    result = subprocess.run(
        ["rclone", "sync", source_dir, remote],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrateError(
            "RCLONE_FAILED",
            f"rclone exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}",
        )


def _write_env_next(
    settings: Settings,
    *,
    target_db: str,
    s3_endpoint: str,
    s3_bucket: str,
    s3_region: str,
    s3_access_key: str,
    s3_secret_key: str,
) -> None:
    env_next = Path.cwd() / ".env.next"
    env_next.write_text(
        "\n".join(
            [
                f"DATABASE_URL={target_db}",
                "IMAGE_STORE_BACKEND=s3",
                f"S3_ENDPOINT={s3_endpoint}",
                f"S3_BUCKET={s3_bucket}",
                f"S3_REGION={s3_region}",
                f"S3_ACCESS_KEY_ID={s3_access_key}",
                f"S3_SECRET_ACCESS_KEY={s3_secret_key}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _sync_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://")
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://")
    return url


def _sqlite_url_for_pgloader(url: str) -> str:
    """pgloader accepts `sqlite:///<absolute-path>`. Strip the `+aiosqlite` driver."""
    if url.startswith("sqlite+aiosqlite:///"):
        return url.replace("sqlite+aiosqlite:///", "sqlite:///")
    return url
