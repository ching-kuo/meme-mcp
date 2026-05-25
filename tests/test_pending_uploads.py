from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meme_mcp.db.uploads import PendingUploadStore


def _create(store: PendingUploadStore, **overrides: object):
    defaults: dict[str, object] = {
        "friend_login": "friend",
        "image_path": "ab/cdef.png",
        "metadata": {"name": "X"},
        "slot_definitions": [{"name": "top", "position": "top"}],
        "exact_hash": "0" * 64,
        "perceptual_hash": "0" * 16,
        "duplicate_action": "accept",
        "duplicate_template_id": None,
        "suspect_flags": [],
    }
    defaults.update(overrides)
    return store.create(**defaults)  # type: ignore[arg-type]


def test_get_rejects_expired_pending_upload(tmp_path) -> None:
    db = tmp_path / "meme.db"
    base = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    store = PendingUploadStore(db, ttl=timedelta(hours=24), clock=lambda: base)
    pending = _create(store)

    later = PendingUploadStore(
        db,
        ttl=timedelta(hours=24),
        clock=lambda: base + timedelta(hours=25),
    )
    with pytest.raises(KeyError):
        later.get(pending.upload_id, "friend")


def test_get_returns_unexpired_pending_upload(tmp_path) -> None:
    db = tmp_path / "meme.db"
    base = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    store = PendingUploadStore(db, ttl=timedelta(hours=24), clock=lambda: base)
    pending = _create(store)

    later = PendingUploadStore(
        db,
        ttl=timedelta(hours=24),
        clock=lambda: base + timedelta(hours=23, minutes=59),
    )
    assert later.get(pending.upload_id, "friend").upload_id == pending.upload_id


def test_init_migrates_pre_ttl_schema(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "meme.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE pending_uploads (
                id TEXT PRIMARY KEY,
                friend_login TEXT NOT NULL,
                image_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                slot_definitions_json TEXT NOT NULL,
                exact_hash TEXT NOT NULL,
                perceptual_hash TEXT NOT NULL,
                duplicate_action TEXT NOT NULL,
                duplicate_template_id TEXT,
                suspect_flags_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO pending_uploads VALUES "
            "('old', 'friend', 'p', '{}', '[]', 'a', 'b', 'accept', NULL, '[]', '2020-01-01')"
        )

    PendingUploadStore(db, ttl=timedelta(hours=24))

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_uploads)")}
    assert "expires_at" in columns


def test_cleanup_expired_removes_only_old_rows(tmp_path) -> None:
    db = tmp_path / "meme.db"
    base = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    old_store = PendingUploadStore(db, ttl=timedelta(hours=24), clock=lambda: base)
    old = _create(old_store, friend_login="alice")

    new_store = PendingUploadStore(
        db,
        ttl=timedelta(hours=24),
        clock=lambda: base + timedelta(hours=23),
    )
    fresh = _create(new_store, friend_login="bob")

    cleaner = PendingUploadStore(
        db,
        ttl=timedelta(hours=24),
        clock=lambda: base + timedelta(hours=25),
    )
    removed = cleaner.cleanup_expired()
    assert removed == 1

    with pytest.raises(KeyError):
        cleaner.get(old.upload_id, "alice")
    assert cleaner.get(fresh.upload_id, "bob").upload_id == fresh.upload_id
