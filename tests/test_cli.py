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
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.db.uploads import PendingUploadStore
from meme_mcp.db.vectors import EmbeddingMetaStore, SQLiteVecStore
from meme_mcp.embeddings.client import validate_embedding_model
from meme_mcp.oauth.store import SQLiteOAuthStore
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


def test_gc_renders_cli_without_flag_inherits_retention(tmp_path, capsys) -> None:
    # The cronjob invokes `gc-renders` with no flag; it must fall back to the
    # configured RENDER_GC_TTL_DAYS and run a TTL sweep, not error out.
    app_settings = settings(tmp_path)
    assert run(["gc-renders"], app_settings) == 0
    assert "render(s)" in capsys.readouterr().out


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
        "github:friend",
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
    assert verified == ("github:friend", "read")
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
    model = "fake-embedding-model"

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


def test_reindex_embeddings_force_purges_stale_meta_and_orphan_vectors(tmp_path) -> None:
    # --force is the boot guard's documented remediation: it must clear stale
    # meta rows (old model/dimensions, deleted templates) and orphan vectors so
    # validate_embedding_model stops latching after the rebuild.
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
    db = tmp_path / "vectors.db"
    vectors = SQLiteVecStore(db, dimensions=3)
    meta = EmbeddingMetaStore(db)
    meta.record("deleted-template", model="old-model", text_hash="x", dimensions=1536)
    vectors.upsert("orphan", [0.0, 1.0, 0.0])
    embedder = FakeEmbeddingClient()

    count = reindex_embeddings(repo, vectors, embedder, meta, force=True)

    assert count == 1
    validate_embedding_model(meta, embedder.model, 3)  # guard unlatches
    assert vectors.search([1.0, 0.0, 0.0], 5) == [("deploy", 1.0)]


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


def test_gc_uploads_retains_blob_shared_with_live_pending(tmp_path, capsys) -> None:
    # Regression: an expired candidate shares a content-addressed blob with a still
    # valid (non-expired) pending upload. The blob MUST be retained -- the live
    # sibling depends on it -- while the expired row is deleted. Before the fix the
    # reference set enumerated only expired rows, so the live sibling's blob was
    # silently reclaimed and later surfaced as a FileNotFoundError on serve.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"shared-with-live-pending", "png")
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)
    live = _make_pending(db, image_path=path, friend_login="bob", expires_at=_now() + _TTL)

    assert run(["gc-uploads"], app_settings) == 0

    assert _blob_exists(store, path)  # blob retained for the live pending
    assert PendingUploadStore(db).expired() == []  # the expired row is gone
    assert PendingUploadStore(db).get(live.upload_id, "bob").upload_id == live.upload_id
    out = capsys.readouterr().out
    assert "deleted 1 pending row(s), 0 orphaned blob(s)" in out


def test_gc_uploads_dry_run_shared_blob_counts_once(tmp_path, capsys) -> None:
    # Dry-run accounting must equal the live sweep: two expired pendings sharing one
    # blob -> "would delete 2 pending row(s), 1 orphaned blob(s)".
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"dry-run-shared", "png")
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)
    _make_pending(db, image_path=path, friend_login="bob", expires_at=expired_at)

    assert run(["gc-uploads", "--dry-run"], app_settings) == 0

    assert _blob_exists(store, path)  # dry-run mutates nothing
    assert len(PendingUploadStore(db).expired()) == 2
    out = capsys.readouterr().out
    assert "would delete 2 pending row(s), 1 orphaned blob(s)" in out


def test_gc_uploads_dry_run_excludes_template_referenced_blob(tmp_path, capsys) -> None:
    # Dry-run must exclude a blob a template references -> 0 orphaned blobs, matching
    # the live sweep's retention.
    app_settings = settings(tmp_path)
    db = tmp_path / "meme.db"
    store = _store_for(app_settings)
    path = store.put(b"dry-run-template-shared", "png")
    _seed_template(db, template_id="keeper", image_path=path)
    expired_at = _now() - DEFAULT_GRACE_WINDOW - timedelta(minutes=5)
    _make_pending(db, image_path=path, friend_login="alice", expires_at=expired_at)

    assert run(["gc-uploads", "--dry-run"], app_settings) == 0

    assert _blob_exists(store, path)
    assert len(PendingUploadStore(db).expired()) == 1
    out = capsys.readouterr().out
    assert "would delete 1 pending row(s), 0 orphaned blob(s)" in out


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


# --- U8: provider-aware CLI ---------------------------------------------------


def _pin_store(tmp_path):
    from pathlib import Path

    from meme_mcp.auth.google_pins import SQLiteGooglePinStore
    from meme_mcp.db.engine import sqlite_path

    s = settings(tmp_path)
    return SQLiteGooglePinStore(sqlite_path(s.database_url, Path(s.storage_dir) / "meme.db"))


def test_allowlist_cli_adds_google_namespaced_entry(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    assert run(["allowlist", "add", "google:friend@gmail.com"], s) == 0
    assert run(["allowlist", "add", "bob"], s) == 0
    assert run(["allowlist", "list"], s) == 0
    listed = capsys.readouterr().out
    assert "google:friend@gmail.com" in listed
    assert "bob" in listed


def test_allowlist_cli_rejects_bare_email(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    assert run(["allowlist", "add", "friend@gmail.com"], s) == 2
    assert "google:friend@gmail.com" in capsys.readouterr().out


def test_allowlist_cli_provider_flag_prefixes(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    assert run(["allowlist", "add", "friend@gmail.com", "--provider", "google"], s) == 0
    run(["allowlist", "list"], s)
    assert "google:friend@gmail.com" in capsys.readouterr().out


def test_pat_cli_issue_for_pinned_google_friend(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    _pin_store(tmp_path).create_pin("sub-A", "alice@gmail.com")
    assert run(["pat", "issue", "alice@gmail.com"], s) == 0
    token = capsys.readouterr().out.strip()
    store = SQLitePatStore(tmp_path / "meme.db")
    assert verify_pat(store, token, s.pat_hash_pepper.get_secret_value()) == (
        "google:sub-A",
        "readwrite",
    )


def test_pat_cli_issue_unpinned_google_fails(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    assert run(["pat", "issue", "stranger@gmail.com"], s) == 2
    assert "sign in with Google" in capsys.readouterr().out


def test_pin_cli_show_and_revoke(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    pins = _pin_store(tmp_path)
    pins.create_pin("sub-A", "alice@gmail.com")
    assert run(["pin", "show", "alice@gmail.com"], s) == 0
    assert "google:sub-A" in capsys.readouterr().out
    assert run(["pin", "revoke", "alice@gmail.com"], s) == 0
    assert "revoked" in capsys.readouterr().out
    assert pins.email_for_sub("sub-A") is None


def test_pin_revoke_accepts_namespaced_sub_from_list_output(tmp_path, capsys) -> None:
    # `pin list` prints google:<sub>; revoking with that exact string must match.
    s = settings(tmp_path)
    pins = _pin_store(tmp_path)
    pins.create_pin("sub-A", "alice@gmail.com")
    assert run(["pin", "revoke", "google:sub-A"], s) == 0
    assert "revoked" in capsys.readouterr().out
    assert pins.email_for_sub("sub-A") is None


def test_allowlist_remove_bare_email_also_deletes_pin(tmp_path) -> None:
    # Defensive: a hand-edited bare email entry still evicts the pin on remove.
    s = settings(tmp_path)
    pins = _pin_store(tmp_path)
    pins.create_pin("sub-A", "alice@gmail.com")
    assert run(["allowlist", "remove", "alice@gmail.com"], s) == 0
    assert pins.email_for_sub("sub-A") is None


def test_allowlist_remove_google_deletes_pin(tmp_path) -> None:
    # R13: removing the invite also evicts the pin so a re-invite cannot
    # reactivate the previously pinned sub.
    s = settings(tmp_path)
    run(["allowlist", "add", "google:alice@gmail.com"], s)
    pins = _pin_store(tmp_path)
    pins.create_pin("sub-A", "alice@gmail.com")
    assert run(["allowlist", "remove", "google:alice@gmail.com"], s) == 0
    assert pins.email_for_sub("sub-A") is None


def test_pat_revoke_google_sub_after_pin_gone(tmp_path, capsys) -> None:
    s = settings(tmp_path)
    pins = _pin_store(tmp_path)
    pins.create_pin("sub-A", "alice@gmail.com")
    run(["pat", "issue", "alice@gmail.com"], s)
    capsys.readouterr()
    pins.delete_by_sub("sub-A")  # pin gone; email->sub no longer resolves
    assert run(["pat", "revoke", "google:sub-A"], s) == 0
    assert "revoked" in capsys.readouterr().out


# --- OAuth authorization-server CLI (U6) ---

_OAUTH_PEPPER = "oauth-token-pepper-32-chars-value-test"
_OAUTH_ENC_KEY = "oauth-secret-enc-key-32-chars-value-test"


def _oauth_settings(tmp_path) -> Settings:
    return settings(tmp_path).model_copy(
        update={
            "oauth_as_enabled": True,
            "oauth_token_pepper": SecretStr(_OAUTH_PEPPER),
            "oauth_secret_enc_key": SecretStr(_OAUTH_ENC_KEY),
        }
    )


def _oauth_store(tmp_path) -> SQLiteOAuthStore:
    db_path = sqlite_path(_oauth_settings(tmp_path).database_url, tmp_path / "meme.db")
    return SQLiteOAuthStore(db_path, token_pepper=_OAUTH_PEPPER, secret_enc_key=_OAUTH_ENC_KEY)


def test_oauth_gc_cli_without_secrets_errors(tmp_path, capsys) -> None:
    # No OAuth secrets configured -> the store cannot open -> clean error, exit 2.
    assert run(["oauth-gc"], settings(tmp_path)) == 2
    assert "OAUTH_TOKEN_PEPPER" in capsys.readouterr().out


def test_oauth_gc_cli_runs(tmp_path, capsys) -> None:
    assert run(["oauth-gc"], _oauth_settings(tmp_path)) == 0
    assert "oauth-gc: removed" in capsys.readouterr().out


def test_allowlist_remove_revokes_oauth_grants(tmp_path) -> None:
    app_settings = _oauth_settings(tmp_path)
    store = _oauth_store(tmp_path)
    _access, refresh = store.issue_initial_tokens(
        client_id="c1", principal="github:friend", scopes=["meme:read"], resource=None
    )
    store.record_approval("github:friend", "c1", ["meme:read"])
    assert run(["allowlist", "add", "friend"], app_settings) == 0
    assert run(["allowlist", "remove", "friend"], app_settings) == 0
    # Refresh family revoked and consent cleared (a re-added friend re-consents).
    assert store.load_refresh_token(refresh) is None
    assert store.has_approval("github:friend", "c1", ["meme:read"]) is False


def test_allowlist_remove_revokes_oauth_grants_even_when_flag_off(tmp_path) -> None:
    # Finding 3: admin revocation must not depend on the serving flag. With the AS
    # disabled but the secrets still configured (a disable-for-maintenance window),
    # `allowlist remove` must still revoke grants + clear consent.
    app_settings = _oauth_settings(tmp_path).model_copy(update={"oauth_as_enabled": False})
    store = _oauth_store(tmp_path)
    _access, refresh = store.issue_initial_tokens(
        client_id="c1", principal="github:friend", scopes=["meme:read"], resource=None
    )
    store.record_approval("github:friend", "c1", ["meme:read"])
    assert run(["allowlist", "remove", "friend"], app_settings) == 0
    assert store.load_refresh_token(refresh) is None
    assert store.has_approval("github:friend", "c1", ["meme:read"]) is False
