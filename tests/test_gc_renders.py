from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from meme_mcp.__main__ import run
from meme_mcp.cli.gc_renders import run as run_gc
from meme_mcp.config import Settings
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.receipts import ReceiptStore
from meme_mcp.rendering.image_store import FilesystemImageStore


def settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'meme.db'}",
        image_store_backend="filesystem",
        image_store_fs_path=str(tmp_path / "images"),
        github_client_id="cid",
        github_client_secret=SecretStr("secret-32-chars-value-for-tests"),
        github_redirect_uri="http://localhost:8000/auth/callback",
        github_allowlist_path=str(tmp_path / "allowlist.txt"),
        operator_github_login="operator",
        session_secret=SecretStr("session-secret-32-chars-value-tests"),
        pat_hash_pepper=SecretStr("pepper-secret-32-chars-value-tests"),
        vlm_base_url="https://example.test/v1",
        vlm_api_key=SecretStr("vlm-key"),
        vlm_model="vlm-model",
        embedding_api_key=SecretStr("embedding-key"),
    )


def _seed_render(
    store: FilesystemImageStore, receipts: ReceiptStore, *, age_days: int, friend: str = "alice"
) -> str:
    content = f"render-{age_days}-{friend}".encode()
    store.put(content, "png")
    import hashlib
    digest = hashlib.sha256(content).hexdigest()[:16]
    # Manually set created_at in receipts so we can simulate aging.
    created_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(receipts.path) as conn:
        conn.execute(
            "INSERT INTO generated_receipts (hash, template_id, friend_login, created_at) "
            "VALUES (?, ?, ?, ?)",
            (digest, "t", friend, created_at),
        )
    return digest


def test_gc_requires_ttl_or_max_bytes(tmp_path, capsys) -> None:
    assert run(["gc-renders"], settings(tmp_path)) == 2
    assert "--ttl-days" in capsys.readouterr().out


def test_gc_ttl_deletes_old_renders_and_keeps_young_ones(tmp_path) -> None:
    s = settings(tmp_path)
    db_path = sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db")
    receipts = ReceiptStore(db_path)
    store = FilesystemImageStore(s.image_store_fs_path)
    old = _seed_render(store, receipts, age_days=30)
    young = _seed_render(store, receipts, age_days=2)

    assert run_gc(s, ttl_days=7) == 0

    assert not store.path_for_hash(old).exists()
    assert store.path_for_hash(young).exists()


def test_gc_dry_run_leaves_disk_and_receipts_untouched(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    db_path = sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db")
    receipts = ReceiptStore(db_path)
    store = FilesystemImageStore(s.image_store_fs_path)
    old = _seed_render(store, receipts, age_days=30)

    assert run_gc(s, ttl_days=7, dry_run=True) == 0
    assert "would delete 1" in capsys.readouterr().out

    assert store.path_for_hash(old).exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM generated_receipts").fetchone()[0] == 1


def test_gc_max_bytes_evicts_lru_until_under_budget(tmp_path) -> None:
    s = settings(tmp_path)
    db_path = sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db")
    receipts = ReceiptStore(db_path)
    store = FilesystemImageStore(s.image_store_fs_path)
    oldest = _seed_render(store, receipts, age_days=30)
    middle = _seed_render(store, receipts, age_days=10)
    youngest = _seed_render(store, receipts, age_days=1)

    total = store.size_of(oldest) + store.size_of(middle) + store.size_of(youngest)
    # Budget just below total -> evict the single oldest.
    assert run_gc(s, max_bytes=total - 1) == 0
    assert not store.path_for_hash(oldest).exists()
    assert store.path_for_hash(middle).exists()
    assert store.path_for_hash(youngest).exists()


def test_gc_does_not_touch_template_seed_images(tmp_path) -> None:
    s = settings(tmp_path)
    store = FilesystemImageStore(s.image_store_fs_path)
    # Put a template image but never register a receipt — GC must leave it alone.
    seed_path = store.put(b"a template seed", "png")
    receipts = ReceiptStore(sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db"))
    _seed_render(store, receipts, age_days=30)  # a real render to trigger the GC pass

    run_gc(s, ttl_days=7)

    assert (store.root / seed_path).exists()


def test_gc_handles_missing_blob_with_extant_receipt(tmp_path) -> None:
    s = settings(tmp_path)
    db_path = sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db")
    receipts = ReceiptStore(db_path)
    store = FilesystemImageStore(s.image_store_fs_path)
    orphan = _seed_render(store, receipts, age_days=30)
    # Delete the blob but leave the receipt row.
    store.delete(orphan)

    assert run_gc(s, ttl_days=7) == 0
    # Receipt row should be gone after GC.
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM generated_receipts").fetchone()[0] == 0


def test_gc_rejects_negative_args(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    assert run_gc(s, ttl_days=-1) == 2
    assert "must be >= 0" in capsys.readouterr().out
    assert run_gc(s, max_bytes=-1) == 2
    assert "must be >= 0" in capsys.readouterr().out
