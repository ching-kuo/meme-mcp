from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
from moto import mock_aws
from pydantic import SecretStr

from meme_mcp.__main__ import run
from meme_mcp.auth.pat import SQLitePatStore, verify_pat
from meme_mcp.cli.gc_uploads import DEFAULT_GRACE_WINDOW
from meme_mcp.cli.reindex_embeddings import reindex_embeddings
from meme_mcp.config import Settings
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.db.vectors import SQLiteVecStore
from meme_mcp.rendering.image_store import ImageStore, make_image_store


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


def test_allowlist_cli_add_list_remove(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["allowlist", "add", "friend"], app_settings) == 0
    assert "friend" in (tmp_path / "allowlist.txt").read_text(encoding="utf-8")

    assert run(["allowlist", "list"], app_settings) == 0
    assert "friend" in capsys.readouterr().out

    assert run(["allowlist", "remove", "friend"], app_settings) == 0
    assert "friend" not in (tmp_path / "allowlist.txt").read_text(encoding="utf-8")


def test_pat_cli_issue_prints_verifiable_token(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["pat", "issue", "friend"], app_settings) == 0
    token = capsys.readouterr().out.strip()

    store = SQLitePatStore(tmp_path / "meme.db")
    assert verify_pat(store, token, app_settings.pat_hash_pepper.get_secret_value()) == (
        "friend",
        "readwrite",
    )


def test_pat_cli_issue_respects_ttl_and_scope_flags(tmp_path, capsys) -> None:
    from meme_mcp.auth.pat import list_pats

    app_settings = settings(tmp_path)

    assert run(["pat", "issue", "friend", "--ttl-days", "30", "--scope", "read"], app_settings) == 0
    token = capsys.readouterr().out.strip()
    assert token

    store = SQLitePatStore(tmp_path / "meme.db")
    verified = verify_pat(store, token, app_settings.pat_hash_pepper.get_secret_value())
    assert verified == ("friend", "read")
    [record] = [r for r in list_pats(store) if r.revoked_at is None]
    assert record.expires_at is not None
    # ttl_days=30 with a small tolerance for the 1-2 second test latency.
    from datetime import UTC, datetime, timedelta

    delta = record.expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) <= delta <= timedelta(days=30, minutes=1)


def test_pat_cli_issue_ttl_zero_means_never_expires(tmp_path, capsys) -> None:
    from meme_mcp.auth.pat import list_pats

    app_settings = settings(tmp_path)
    assert run(["pat", "issue", "friend", "--ttl-days", "0"], app_settings) == 0
    capsys.readouterr()

    store = SQLitePatStore(tmp_path / "meme.db")
    [record] = [r for r in list_pats(store) if r.revoked_at is None]
    assert record.expires_at is None


def test_pat_cli_list_shows_active_and_revoked(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)
    # Empty case prints the no-PATs notice.
    assert run(["pat", "list"], app_settings) == 0
    assert "no PATs issued" in capsys.readouterr().out

    assert run(["pat", "issue", "alice"], app_settings) == 0
    capsys.readouterr()  # discard issued token
    assert run(["pat", "issue", "bob", "--scope", "read", "--ttl-days", "0"], app_settings) == 0
    capsys.readouterr()
    # Reissuing for alice revokes the prior PAT.
    assert run(["pat", "issue", "alice"], app_settings) == 0
    capsys.readouterr()

    assert run(["pat", "list"], app_settings) == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out
    assert "active" in out
    assert "revoked" in out
    assert "never" in out  # bob has no expiry
    assert "read" in out
    assert "readwrite" in out


def test_pat_cli_issue_rejects_invalid_scope(tmp_path) -> None:
    import pytest

    app_settings = settings(tmp_path)
    with pytest.raises(SystemExit):
        run(["pat", "issue", "friend", "--scope", "admin"], app_settings)


def test_pat_cli_issue_rejects_negative_ttl_with_clean_message(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)
    assert run(["pat", "issue", "friend", "--ttl-days", "-5"], app_settings) == 2
    out = capsys.readouterr().out
    assert "--ttl-days must be >= 0" in out


class FakeEmbeddingClient:
    def embed_template(self, metadata: dict[str, object]) -> list[float]:
        assert metadata["description"] == "ship green"
        return [1.0, 0.0, 0.0]


def test_reindex_embeddings_rebuilds_vector_store_from_templates(tmp_path) -> None:
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    repo.upsert(
        TemplateCreate(
            template_id="deploy",
            slug="deploy",
            name="Deploy",
            source="friend",
            metadata={"description": "ship green", "tags": ["ci"]},
            slot_definitions=[{"position": "top"}],
            image_path="aa/deploy.png",
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    vectors = SQLiteVecStore(tmp_path / "vectors.db", dimensions=3)

    count = reindex_embeddings(repo, vectors, FakeEmbeddingClient())

    assert count == 1
    assert vectors.search([1.0, 0.0, 0.0], 1) == [("deploy", 1.0)]


def test_seed_memegen_cli_persists_default_templates(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["seed-memegen"], app_settings) == 0

    assert "seeded" in capsys.readouterr().out
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    assert repo.get("memegen-drake").name == "Drake Hotline Bling"


# --- gc-uploads -------------------------------------------------------------

_TTL = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(UTC)


def _pending_store_expiring_at(db, expires_at: datetime) -> PendingUploadStore:
    """A PendingUploadStore whose injected clock makes new rows expire at expires_at.

    create() sets expires_at = clock() + ttl, so we pick clock() = expires_at - ttl.
    """
    clock_value = expires_at - _TTL
    return PendingUploadStore(db, ttl=_TTL, clock=lambda: clock_value)


def _make_pending(db, *, image_path: str, friend_login: str, expires_at: datetime):
    store = _pending_store_expiring_at(db, expires_at)
    return store.create(
        friend_login=friend_login,
        image_path=image_path,
        metadata={"name": "X"},
        slot_definitions=[{"name": "top", "position": "top"}],
        exact_hash="0" * 64,
        perceptual_hash="0" * 16,
        duplicate_action="accept",
        duplicate_template_id=None,
        suspect_flags=[],
    )


def _store_for(app_settings: Settings) -> ImageStore:
    return make_image_store(
        app_settings.image_store_backend,
        fs_path=app_settings.image_store_fs_path,
        s3_endpoint=app_settings.s3_endpoint,
        s3_bucket=app_settings.s3_bucket,
        s3_region=app_settings.s3_region,
        s3_access_key_id=(
            app_settings.s3_access_key_id.get_secret_value()
            if app_settings.s3_access_key_id is not None
            else None
        ),
        s3_secret_access_key=(
            app_settings.s3_secret_access_key.get_secret_value()
            if app_settings.s3_secret_access_key is not None
            else None
        ),
    )


def _blob_exists(store: ImageStore, path: str) -> bool:
    try:
        store.get(path)
        return True
    except FileNotFoundError:
        return False


def s3_settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'meme.db'}",
        image_store_backend="s3",
        image_store_fs_path=str(tmp_path / "images"),
        # An AWS-domain endpoint so moto's mock_aws intercepts it (a custom host like
        # http://s3.local would trigger real DNS and bypass the mock).
        s3_endpoint="https://s3.us-east-1.amazonaws.com",
        s3_bucket="meme-test",
        s3_region="us-east-1",
        s3_access_key_id=SecretStr("test"),
        s3_secret_access_key=SecretStr("test"),
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


def _seed_template(db, *, template_id: str, image_path: str) -> None:
    repo = SQLiteTemplateRepository(db)
    repo.upsert(
        TemplateCreate(
            template_id=template_id,
            slug=template_id,
            name=template_id.title(),
            source="friend",
            metadata={"description": "d", "tags": []},
            slot_definitions=[{"position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )


def test_gc_uploads_filesystem_deletes_expired_unreferenced_blob(tmp_path, capsys) -> None:
    # AE11: expired pending, blob unreferenced -> blob deleted.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"orphan-bytes", "png")
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    pending = _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

    assert run(["gc-uploads"], app_settings) == 0

    assert not _blob_exists(store, path)
    assert PendingUploadStore(db).expired() == []
    assert pending.upload_id  # row existed; now removed
    out = capsys.readouterr().out
    assert "deleted 1 pending row(s), 1 orphaned blob(s)" in out


def test_gc_uploads_retains_blob_referenced_by_template(tmp_path) -> None:
    # Expired pending whose blob is referenced by a template -> row deleted, blob kept.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"shared-with-template", "png")
    _seed_template(db, template_id="keeper", image_path=path)
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

    assert run(["gc-uploads"], app_settings) == 0

    assert _blob_exists(store, path)  # blob retained
    assert PendingUploadStore(db).expired() == []  # row deleted


def test_gc_uploads_two_pendings_sharing_blob_deleted_once(tmp_path, capsys) -> None:
    # Two expired pendings sharing one blob -> blob deleted once, no error.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"shared-between-two-pendings", "png")
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)
    _make_pending(db, image_path=path, friend_login="bob", expires_at=expired_at)

    assert run(["gc-uploads"], app_settings) == 0

    assert not _blob_exists(store, path)
    assert PendingUploadStore(db).expired() == []
    out = capsys.readouterr().out
    assert "deleted 2 pending row(s), 1 orphaned blob(s)" in out


def test_gc_uploads_skips_rows_inside_grace_window(tmp_path, capsys) -> None:
    # A row inside the grace window is not swept even if expired.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"recently-expired", "png")
    # Expired (expires_at <= now) but within the grace window (> now - grace).
    expired_at = _now() - timedelta(minutes=1)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

    assert run(["gc-uploads"], app_settings) == 0

    assert _blob_exists(store, path)  # blob retained
    assert len(PendingUploadStore(db).expired()) == 1  # row retained
    out = capsys.readouterr().out
    assert "deleted 0 pending row(s), 0 orphaned blob(s)" in out


def test_gc_uploads_dry_run_deletes_nothing(tmp_path, capsys) -> None:
    # --dry-run reports without acting.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"orphan-dry-run", "png")
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

    assert run(["gc-uploads", "--dry-run"], app_settings) == 0

    assert _blob_exists(store, path)  # nothing deleted
    assert len(PendingUploadStore(db).expired()) == 1
    out = capsys.readouterr().out
    assert "would delete 1 pending row(s), 1 orphaned blob(s)" in out


def test_gc_uploads_s3_backend_deletes_orphaned_blob(tmp_path, capsys) -> None:
    # Guards the make_image_store requirement: the sweep must reclaim blobs on S3,
    # not silently no-op (it would if it instantiated FilesystemImageStore directly).
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.create_bucket(Bucket="meme-test")
        app_settings = s3_settings(tmp_path)
        db = tmp_path / "meme.db"
        store = _store_for(app_settings)
        path = store.put(b"orphan-s3", "png")
        expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
        _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

        assert run(["gc-uploads"], app_settings) == 0

        assert not _blob_exists(store, path)
        assert PendingUploadStore(db).expired() == []
        out = capsys.readouterr().out
        assert "deleted 1 pending row(s), 1 orphaned blob(s)" in out


def test_gc_uploads_s3_backend_retains_template_referenced_blob(tmp_path) -> None:
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.create_bucket(Bucket="meme-test")
        app_settings = s3_settings(tmp_path)
        db = tmp_path / "meme.db"
        store = _store_for(app_settings)
        path = store.put(b"shared-s3", "png")
        _seed_template(db, template_id="keeper", image_path=path)
        expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
        _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

        assert run(["gc-uploads"], app_settings) == 0

        assert _blob_exists(store, path)
        assert PendingUploadStore(db).expired() == []
