"""Pending-upload GC. Reclaims orphaned pending-upload blobs left behind when a
review is abandoned (the 24h TTL lapses) or discarded (discard deletes only the
row, never the blob -- KTD8).

The sweep is:

- Grace-windowed: it considers only rows whose `expires_at` is older than
  `now - grace_window`. The grace window must exceed a worst-case analyze+VLM
  round-trip, because `analyze` writes the blob (`image_store.put`) BEFORE it
  creates the pending row; without the window the sweep could delete a blob a
  concurrent in-flight `analyze` just put but has not yet recorded.
- Reference-aware: a content-addressed blob can be shared by two pending rows or
  by an approved template. The protected `image_path` set -- every template image,
  every live (non-expired) pending blob, and every expired-but-within-grace sibling
  -- is computed once (it is invariant across the sweep, which only deletes
  candidate rows), and `image_store.delete(path)` runs only for a candidate blob
  that is not protected and not already reclaimed this sweep. Two expired pendings
  sharing one blob therefore delete it exactly once, and a blob a still-valid
  pending upload shares with an expired sibling is never reclaimed.
- Backend-agnostic: the image store is built via `make_image_store` with the same
  backend + explicit kwargs `create_app()` uses, so the sweep reclaims blobs on
  both the filesystem and S3 backends. Instantiating `FilesystemImageStore`
  directly (as `gc_renders` does) would make the sweep silently no-op on S3,
  where orphan cost matters most.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meme_mcp.config import Settings
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.rendering.image_store import ImageStore, make_image_store_from_settings

logger = logging.getLogger(__name__)

# Exceeds a worst-case analyze+VLM round-trip so the analyze put-before-row-create
# window has closed before any blob is reclaimed (KTD8).
DEFAULT_GRACE_WINDOW = timedelta(minutes=15)


def run(
    settings: Settings,
    *,
    grace_window: timedelta = DEFAULT_GRACE_WINDOW,
    dry_run: bool = False,
) -> int:
    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    pending = PendingUploadStore(db_path)
    templates = SQLiteTemplateRepository(db_path)
    store = make_image_store_from_settings(settings)

    deleted_rows, deleted_blobs = _gc(
        pending,
        templates,
        store,
        grace_window=grace_window,
        dry_run=dry_run,
    )
    verb = "would delete" if dry_run else "deleted"
    print(f"{verb} {deleted_rows} pending row(s), {deleted_blobs} orphaned blob(s)")
    return 0


def _gc(
    pending: PendingUploadStore,
    templates: SQLiteTemplateRepository,
    store: ImageStore,
    *,
    grace_window: timedelta,
    dry_run: bool,
) -> tuple[int, int]:
    cutoff = datetime.now(UTC) - grace_window
    all_expired = pending.expired()
    candidates = [row for row in all_expired if row.expires_at < cutoff]
    if not candidates:
        return 0, 0

    # Paths nothing in this sweep may reclaim: every template image, every live
    # (non-expired) pending blob, and every expired-but-within-grace sibling (present
    # in expired() yet not a candidate). This set is invariant across the sweep -- the
    # loop only deletes candidate rows -- so it is computed once rather than re-queried
    # per candidate. Blobs are content-addressed, so any of these references keeps a
    # candidate's shared blob alive; the grace window separately guards the analyze
    # put-before-row-create window, where the blob exists but its row does not (KTD8).
    candidate_ids = {row.upload_id for row in candidates}
    protected = {row.image_path for row in templates.list_rows()}
    protected |= pending.live_image_paths()
    protected |= {row.image_path for row in all_expired if row.upload_id not in candidate_ids}

    if dry_run:
        orphaned = {c.image_path for c in candidates if c.image_path not in protected}
        return len(candidates), len(orphaned)

    deleted_rows = 0
    reclaimed: set[str] = set()
    for candidate in candidates:
        path = candidate.image_path
        # Reclaim each orphaned blob exactly once even when several candidates share it.
        if path not in protected and path not in reclaimed:
            store.delete(path)
            reclaimed.add(path)
        pending.delete(candidate.upload_id)
        deleted_rows += 1
    return deleted_rows, len(reclaimed)
