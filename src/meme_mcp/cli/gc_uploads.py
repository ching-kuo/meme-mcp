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
  by an approved template. For each candidate the referenced `image_path` set is
  recomputed immediately before deletion from `templates.list_rows()`, every live
  (non-expired) pending row, and the surviving (not-yet-deleted) expired pending
  rows; `image_store.delete(path)` is called only for blobs that nothing
  references. Two expired pendings sharing one blob therefore delete the blob
  exactly once, and a blob a still-valid pending upload shares with an expired
  sibling is never reclaimed.
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
from meme_mcp.db.uploads import ExpiredPending, PendingUploadStore
from meme_mcp.rendering.image_store import ImageStore, make_image_store

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
    store = _make_store(settings)

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


def _make_store(settings: Settings) -> ImageStore:
    """Build the image store via the factory, mirroring create_app()'s call so the
    sweep works on both the filesystem and S3 backends.
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


def _gc(
    pending: PendingUploadStore,
    templates: SQLiteTemplateRepository,
    store: ImageStore,
    *,
    grace_window: timedelta,
    dry_run: bool,
) -> tuple[int, int]:
    cutoff = datetime.now(UTC) - grace_window
    candidates = [row for row in pending.expired() if row.expires_at < cutoff]
    if not candidates:
        return 0, 0

    if dry_run:
        blob_count = _orphaned_blob_count(pending, candidates, templates)
        return len(candidates), blob_count

    deleted_rows = 0
    deleted_blobs = 0
    for candidate in candidates:
        # Recompute references immediately before each delete (KTD8): a template or a
        # surviving sibling pending row may still reference this shared blob. The
        # current candidate is excluded because we are about to delete its row.
        if candidate.image_path not in _referenced_paths(
            pending, templates, exclude_upload_id=candidate.upload_id
        ):
            store.delete(candidate.image_path)
            deleted_blobs += 1
        pending.delete(candidate.upload_id)
        deleted_rows += 1
    return deleted_rows, deleted_blobs


def _orphaned_blob_count(
    pending: PendingUploadStore,
    candidates: list[ExpiredPending],
    templates: SQLiteTemplateRepository,
) -> int:
    """Count blobs the sweep would reclaim, without acting.

    Mirrors the live sweep's reference set so the dry-run figure equals the real
    reclaim count: a candidate's blob is orphaned only if nothing else references it
    -- no template, no live (non-expired) pending row, and no still-present expired
    pending row that is not itself a candidate. Distinct candidate paths are counted
    once (shared-blob candidates collapse to a single reclaim, matching the live loop,
    which deletes such a blob exactly once on the last candidate that holds it).
    """
    candidate_ids = {c.upload_id for c in candidates}
    referenced = {row.image_path for row in templates.list_rows()}
    referenced |= pending.live_image_paths()
    for row in pending.expired():
        if row.upload_id not in candidate_ids:
            referenced.add(row.image_path)
    distinct = {c.image_path for c in candidates if c.image_path not in referenced}
    return len(distinct)


def _referenced_paths(
    pending: PendingUploadStore,
    templates: SQLiteTemplateRepository,
    *,
    exclude_upload_id: str,
) -> set[str]:
    """Recompute the set of image paths that must NOT be reclaimed while deleting the
    current candidate: every template's image, every live (non-expired) pending row's
    image, and every still-present expired pending row except the one about to be
    deleted.

    Blobs are content-addressed, so a still-valid pending upload can share one blob
    with an expired candidate; `live_image_paths()` keeps that blob alive. Querying
    `pending.expired()` fresh each call lets earlier deletions in the sweep drop out of
    the reference set (so a blob shared by two expired candidates is deleted exactly
    once on the second pass) while a not-yet-deleted expired sibling keeps the blob
    alive on the first pass. The grace window is a separate guard for the analyze
    put-before-row-create window, where the blob exists but its pending row has not
    been recorded yet, so it appears in neither set (KTD8).
    """
    paths = {row.image_path for row in templates.list_rows()}
    paths |= pending.live_image_paths()
    for row in pending.expired():
        if row.upload_id != exclude_upload_id:
            paths.add(row.image_path)
    return paths
