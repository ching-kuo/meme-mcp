"""Unit tests for SQLiteOAuthStore: token lifecycle, hashing discipline, fail-closed
parsing, client-secret encryption, consent approvals, and migration parity."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl, SecretStr

from meme_mcp.config import Settings
from meme_mcp.db.migrations import run_migrations
from meme_mcp.oauth.store import (
    REFRESH_GRACE_SECONDS,
    SQLiteOAuthStore,
    generate_token,
)

PEPPER = "oauth-token-pepper-32-chars-value-test"
ENC_KEY = "oauth-secret-enc-key-32-chars-value-test"

_OAUTH_TABLES = (
    "oauth_clients",
    "oauth_auth_codes",
    "oauth_refresh_tokens",
    "oauth_access_tokens",
    "oauth_client_approvals",
    "oauth_pending_requests",
)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_store(tmp_path: Path, clock: FakeClock | None = None) -> SQLiteOAuthStore:
    return SQLiteOAuthStore(
        tmp_path / "oauth.db",
        token_pepper=PEPPER,
        secret_enc_key=ENC_KEY,
        clock=clock,
    )


def _issue_pair(store: SQLiteOAuthStore) -> tuple[str, str]:
    return store.issue_initial_tokens(
        client_id="client-1", principal="github:alice", scopes=["meme:read", "meme:write"], resource="https://meme.igene.tw/mcp"
    )


# -- access tokens ------------------------------------------------------------


def test_access_token_round_trip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    access, _refresh = _issue_pair(store)
    loaded = store.load_access_token(access)
    assert loaded is not None
    assert loaded.principal == "github:alice"
    assert loaded.scopes == ("meme:read", "meme:write")
    assert loaded.resource == "https://meme.igene.tw/mcp"
    assert loaded.client_id == "client-1"


def test_unknown_access_token_returns_none(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.load_access_token("nonexistent-token") is None


def test_expired_access_token_is_invalid(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    access, _ = _issue_pair(store)
    assert store.load_access_token(access) is not None
    clock.advance(hours=1)  # past the 15-minute access TTL
    assert store.load_access_token(access) is None


# -- refresh rotation, reuse detection, idempotent grace ----------------------


def test_refresh_rotation_invalidates_prior_and_keeps_family(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    _access, refresh = _issue_pair(store)
    rotated = store.rotate_refresh_token(refresh, ["meme:read", "meme:write"])
    assert rotated is not None
    new_access, new_refresh = rotated
    # New tokens are usable; the new refresh shares the original family.
    first = store.load_refresh_token(refresh)
    assert first is not None and first.state == "grace"  # within grace, still loadable
    new_loaded = store.load_refresh_token(new_refresh)
    assert new_loaded is not None and new_loaded.family_id == first.family_id
    assert store.load_access_token(new_access) is not None


def test_reuse_beyond_grace_revokes_family(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    _access, refresh = _issue_pair(store)
    _, new_refresh = store.rotate_refresh_token(refresh, ["meme:read"]) or ("", "")
    clock.advance(seconds=REFRESH_GRACE_SECONDS + 5)
    # Replaying the rotated-away token beyond grace is reuse: family revoked.
    assert store.load_refresh_token(refresh) is None
    assert store.load_refresh_token(new_refresh) is None  # the successor dies too


def test_idempotent_rotation_within_grace_no_family_revocation(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    _access, refresh = _issue_pair(store)
    first = store.rotate_refresh_token(refresh, ["meme:read"])
    assert first is not None
    # A legitimate retry within the grace window mints a fresh usable pair and
    # does NOT revoke the family (no logout). Byte-identity is impossible under
    # hash-at-rest, so the contract is "valid pair + family intact".
    clock.advance(seconds=REFRESH_GRACE_SECONDS - 5)
    second = store.rotate_refresh_token(refresh, ["meme:read"])
    assert second is not None
    retry_access, retry_refresh = second
    assert store.load_access_token(retry_access) is not None
    assert store.load_refresh_token(retry_refresh) is not None
    # The first successor remains valid (family not revoked).
    assert store.load_refresh_token(first[1]) is not None


def test_refresh_carries_client_id_for_binding(tmp_path: Path) -> None:
    # The SDK TokenHandler enforces client_id binding using this field (provider
    # tests cover the rejection path); the store's job is to carry it faithfully.
    store = make_store(tmp_path)
    _access, refresh = _issue_pair(store)
    loaded = store.load_refresh_token(refresh)
    assert loaded is not None and loaded.client_id == "client-1"


def test_revoke_token_revokes_family(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    access, refresh = _issue_pair(store)
    store.revoke_token(access)  # revoking the access token kills the whole family
    assert store.load_access_token(access) is None
    assert store.load_refresh_token(refresh) is None


# -- authorization codes ------------------------------------------------------


def test_auth_code_single_use(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    code = store.create_auth_code(
        client_id="c1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        code_challenge="abc",
        scopes=["meme:read"],
        principal="github:alice",
        resource=None,
    )
    assert store.load_auth_code(code) is not None
    assert store.consume_auth_code(code) is True
    assert store.consume_auth_code(code) is False  # second redemption fails
    assert store.load_auth_code(code) is None


def test_auth_code_expires(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    code = store.create_auth_code(
        client_id="c1",
        redirect_uri="https://x/cb",
        redirect_uri_provided_explicitly=True,
        code_challenge="abc",
        scopes=["meme:read"],
        principal="github:alice",
        resource=None,
    )
    clock.advance(minutes=10)
    assert store.load_auth_code(code) is None


# -- fail-closed parsing ------------------------------------------------------


def test_corrupt_expires_at_fails_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    access, _ = _issue_pair(store)
    # Corrupt the stored expiry to a non-ISO string; it must read as invalid, not
    # sort past real timestamps as "never expires".
    with sqlite3.connect(tmp_path / "oauth.db") as conn:
        conn.execute("UPDATE oauth_access_tokens SET expires_at = 'not-a-date'")
    assert store.load_access_token(access) is None


def test_failure_modes_are_uniform_none(tmp_path: Path) -> None:
    # Unknown, revoked, and expired all return None via the same unconditional
    # hash lookup (timing-uniform, mirroring the PAT store discipline).
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    access_ok, _ = _issue_pair(store)
    access_revoked, _ = store.issue_initial_tokens(
        client_id="c", principal="github:bob", scopes=["meme:read"], resource=None
    )
    store.revoke_token(access_revoked)
    assert store.load_access_token("unknown") is None
    assert store.load_access_token(access_revoked) is None
    clock.advance(hours=1)
    assert store.load_access_token(access_ok) is None


# -- hashing discipline & client encryption -----------------------------------


def test_plaintext_token_never_stored(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    access, refresh = _issue_pair(store)
    with sqlite3.connect(tmp_path / "oauth.db") as conn:
        access_rows = conn.execute("SELECT * FROM oauth_access_tokens").fetchall()
        refresh_rows = conn.execute("SELECT * FROM oauth_refresh_tokens").fetchall()
        access_blob = " ".join(str(c) for row in access_rows for c in row)
        refresh_blob = " ".join(str(c) for row in refresh_rows for c in row)
    assert access not in access_blob
    assert refresh not in refresh_blob


def test_client_round_trips_with_all_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = OAuthClientInformationFull(
        client_id="client-xyz",
        client_secret="super-secret-value",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="meme:read meme:write",
        client_secret_expires_at=0,
    )
    store.register_client(client)
    loaded = store.get_client("client-xyz")
    assert loaded is not None
    assert [str(u) for u in loaded.redirect_uris] == ["https://claude.ai/api/mcp/auth_callback"]
    assert loaded.grant_types == ["authorization_code", "refresh_token"]
    assert loaded.response_types == ["code"]
    assert loaded.scope == "meme:read meme:write"
    assert loaded.token_endpoint_auth_method == "client_secret_post"
    # The secret decrypts back to the registered plaintext (F-001) ...
    assert loaded.client_secret == "super-secret-value"
    # ... but is stored encrypted, never plaintext.
    with sqlite3.connect(tmp_path / "oauth.db") as conn:
        info_json, enc = conn.execute(
            "SELECT client_info, client_secret_encrypted FROM oauth_clients "
            "WHERE client_id='client-xyz'"
        ).fetchone()
    assert "super-secret-value" not in str(info_json)
    assert "super-secret-value" not in str(enc)


def test_public_client_stores_no_secret(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = OAuthClientInformationFull(
        client_id="public-1",
        client_secret=None,
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="none",
    )
    store.register_client(client)
    loaded = store.get_client("public-1")
    assert loaded is not None and loaded.client_secret is None
    with sqlite3.connect(tmp_path / "oauth.db") as conn:
        enc = conn.execute(
            "SELECT client_secret_encrypted FROM oauth_clients WHERE client_id='public-1'"
        ).fetchone()[0]
    assert enc is None


# -- consent approvals --------------------------------------------------------


def test_approval_upsert_and_absence(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.has_approval("github:alice", "c1") is False
    store.record_approval("github:alice", "c1")
    store.record_approval("github:alice", "c1")  # idempotent
    assert store.has_approval("github:alice", "c1") is True
    assert store.has_approval("github:alice", "c2") is False
    assert store.delete_approvals_for_principal("github:alice") == 1
    assert store.has_approval("github:alice", "c1") is False


# -- pending requests ---------------------------------------------------------


def test_pending_request_round_trip_and_single_use(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    nonce = store.create_pending_request(
        client_id="c1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        code_challenge="chal",
        scopes=["meme:read"],
        resource="https://meme.igene.tw/mcp",
        state="xyz",
    )
    loaded = store.load_pending_request(nonce)
    assert loaded is not None
    assert loaded.client_id == "c1"
    assert loaded.code_challenge == "chal"
    assert loaded.scopes == ("meme:read",)
    assert loaded.state == "xyz"
    assert store.consume_pending_request(nonce) is True
    assert store.consume_pending_request(nonce) is False
    assert store.load_pending_request(nonce) is None


def test_pending_request_expires(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    nonce = store.create_pending_request(
        client_id="c1",
        redirect_uri="https://x/cb",
        redirect_uri_provided_explicitly=True,
        code_challenge="chal",
        scopes=["meme:read"],
        resource=None,
        state=None,
    )
    clock.advance(minutes=15)
    assert store.load_pending_request(nonce) is None


# -- GC -----------------------------------------------------------------------


def test_gc_expired_tokens(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    _issue_pair(store)
    clock.advance(days=60)  # past both access and refresh TTLs
    removed = store.gc_expired_tokens()
    assert removed >= 2
    with sqlite3.connect(tmp_path / "oauth.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM oauth_access_tokens").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM oauth_refresh_tokens").fetchone()[0] == 0


def test_gc_unused_clients(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 6, 6, tzinfo=UTC))
    store = make_store(tmp_path, clock)
    stale = OAuthClientInformationFull(
        client_id="stale", redirect_uris=[AnyUrl("https://x/cb")], token_endpoint_auth_method="none"
    )
    store.register_client(stale)
    clock.advance(days=40)
    fresh = OAuthClientInformationFull(
        client_id="fresh", redirect_uris=[AnyUrl("https://x/cb")], token_endpoint_auth_method="none"
    )
    store.register_client(fresh)
    store.mark_client_used("fresh")
    removed = store.gc_unused_clients(ttl_days=30)
    assert removed == 1
    assert store.get_client("stale") is None
    assert store.get_client("fresh") is not None


# -- migration parity ---------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
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


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_matches_store_schema(tmp_path: Path) -> None:
    # Migration 0004_oauth must produce a shape identical to the store's
    # self-created tables.
    migrated = tmp_path / "migrated"
    migrated.mkdir()
    run_migrations(_settings(migrated))
    migrated_db = migrated / "meme.db"

    store_db_dir = tmp_path / "store"
    store_db_dir.mkdir()
    SQLiteOAuthStore(
        store_db_dir / "meme.db", token_pepper=PEPPER, secret_enc_key=ENC_KEY
    )
    store_db = store_db_dir / "meme.db"

    for table in _OAUTH_TABLES:
        assert _columns(migrated_db, table) == _columns(store_db, table), table


def test_migration_downgrade_drops_tables(tmp_path: Path) -> None:
    from alembic import command
    from meme_mcp.db.migrations import _alembic_config

    settings = _settings(tmp_path)
    run_migrations(settings)
    cfg = _alembic_config(settings.database_url)
    command.downgrade(cfg, "0003_google_pins")
    with sqlite3.connect(tmp_path / "meme.db") as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in rows}
    for table in _OAUTH_TABLES:
        assert table not in names


def test_generate_token_is_unguessable() -> None:
    assert len({generate_token() for _ in range(100)}) == 100
    assert len(generate_token()) >= 40
