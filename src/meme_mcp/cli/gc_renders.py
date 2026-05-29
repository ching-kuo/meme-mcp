"""Render-output GC. Prunes blobs tracked by generated_receipts; never touches
template-seed images (those have no receipt row). TTL mode deletes everything older
than --ttl-days; max-bytes mode evicts LRU by created_at until under budget.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meme_mcp.config import Settings
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.rendering.image_store import FilesystemImageStore

logger = logging.getLogger(__name__)


def run(
    settings: Settings,
    *,
    ttl_days: int | None = None,
    max_bytes: int | None = None,
    dry_run: bool = False,
) -> int:
    if ttl_days is None and max_bytes is None:
        print("error: pass --ttl-days N or --max-bytes N (or both)")
        return 2
    if ttl_days is not None and ttl_days < 0:
        print(f"error: --ttl-days must be >= 0, got {ttl_days}")
        return 2
    if max_bytes is not None and max_bytes < 0:
        print(f"error: --max-bytes must be >= 0, got {max_bytes}")
        return 2

    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    receipts = ReceiptStore(db_path)
    store = FilesystemImageStore(settings.image_store_fs_path)

    deleted_count, freed_bytes = _gc(receipts, store, ttl_days, max_bytes, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    print(f"{verb} {deleted_count} render(s), freed {freed_bytes} bytes")
    return 0


def _gc(
    receipts: ReceiptStore,
    store: FilesystemImageStore,
    ttl_days: int | None,
    max_bytes: int | None,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    candidates: list[tuple[str, datetime, int]] = []
    if ttl_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        for h in receipts.hashes_older_than(cutoff):
            candidates.append((h, cutoff, store.size_of(h)))
    if max_bytes is not None:
        all_rows = receipts.all_hashes_oldest_first()
        total = sum(store.size_of(h) for h, _ in all_rows)
        for h, ts in all_rows:
            if total <= max_bytes:
                break
            size = store.size_of(h)
            candidates.append((h, ts, size))
            total -= size
    # Dedupe — both modes may identify the same hash.
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for h, _ts, size in candidates:
        if h in seen:
            continue
        seen.add(h)
        unique.append((h, size))

    if dry_run:
        return len(unique), sum(size for _, size in unique)

    deleted = 0
    freed = 0
    for rendered_hash, size in unique:
        with store.shard_lock(rendered_hash):
            if store.delete_by_hash(rendered_hash):
                freed += size
            receipts.delete(rendered_hash)
            deleted += 1
    return deleted, freed
