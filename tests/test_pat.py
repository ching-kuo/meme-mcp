from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meme_mcp.auth.pat import (
    DEFAULT_CAPABILITY,
    InMemoryPatStore,
    SQLitePatStore,
    issue_pat,
    verify_pat,
)


def test_issue_and_verify_pat() -> None:
    store = InMemoryPatStore()
    plaintext = issue_pat(store, "alice", "pepper")
    assert len(plaintext) >= 40
    assert plaintext not in store.records[0].pat_hash
    assert verify_pat(store, plaintext, "pepper") == ("alice", DEFAULT_CAPABILITY)


def test_second_pat_revokes_first() -> None:
    store = InMemoryPatStore()
    first = issue_pat(store, "alice", "pepper")
    second = issue_pat(store, "alice", "pepper")
    assert verify_pat(store, first, "pepper") is None
    assert verify_pat(store, second, "pepper") == ("alice", DEFAULT_CAPABILITY)


def test_pepper_rotation_invalidates_existing_pat() -> None:
    store = InMemoryPatStore()
    token = issue_pat(store, "alice", "pepper")
    assert verify_pat(store, token, "new-pepper") is None


def test_issue_with_explicit_capability(tmp_path: Path) -> None:
    store = SQLitePatStore(tmp_path / "pats.db")
    read_token = issue_pat(store, "alice", "pepper", capability="read")
    assert verify_pat(store, read_token, "pepper") == ("alice", "read")


def test_issue_rejects_unknown_capability() -> None:
    store = InMemoryPatStore()
    with pytest.raises(ValueError, match="capability"):
        issue_pat(store, "alice", "pepper", capability="admin")  # type: ignore[arg-type]


def test_issue_rejects_negative_ttl() -> None:
    store = InMemoryPatStore()
    with pytest.raises(ValueError, match="ttl_days"):
        issue_pat(store, "alice", "pepper", ttl_days=-1)


def test_ttl_zero_means_never_expires(tmp_path: Path) -> None:
    db_path = tmp_path / "pats.db"
    store = SQLitePatStore(db_path)
    token = issue_pat(store, "alice", "pepper", ttl_days=0)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT expires_at FROM pats WHERE friend_login = ?", ("alice",)
        ).fetchone()
    assert row is not None and row[0] is None
    assert verify_pat(store, token, "pepper") == ("alice", DEFAULT_CAPABILITY)


def test_expired_pat_returns_none(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    clock_value = [fixed_now]
    store = SQLitePatStore(tmp_path / "pats.db", clock=lambda: clock_value[0])
    token = issue_pat(store, "alice", "pepper", ttl_days=1)
    assert verify_pat(store, token, "pepper") == ("alice", DEFAULT_CAPABILITY)
    clock_value[0] = fixed_now + timedelta(days=2)
    assert verify_pat(store, token, "pepper") is None


def test_back_compat_legacy_row_without_new_columns(tmp_path: Path) -> None:
    """Pre-v1.5 DBs created the `pats` table with only the original 6 columns. The
    SQLitePatStore initializer must ALTER the table idempotently and treat legacy rows
    (NULL expires_at, default capability) as active readwrite tokens.
    """
    db_path = tmp_path / "pats.db"
    legacy_hash = "0" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE pats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                friend_login TEXT NOT NULL,
                pat_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO pats (friend_login, pat_hash, created_at) VALUES (?, ?, ?)",
            ("legacy-alice", legacy_hash, datetime.now(UTC).isoformat()),
        )
    store = SQLitePatStore(db_path)
    columns = _columns(db_path, "pats")
    assert "expires_at" in columns
    assert "capability" in columns
    result = store.verify(legacy_hash)
    assert result == ("legacy-alice", DEFAULT_CAPABILITY)


def test_alter_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "pats.db"
    SQLitePatStore(db_path)
    # Second initializer must not error or duplicate columns.
    SQLitePatStore(db_path)
    columns = list(_columns(db_path, "pats"))
    # Each column appears exactly once.
    assert columns.count("expires_at") == 1
    assert columns.count("capability") == 1


def test_naive_expires_at_fails_closed(tmp_path: Path) -> None:
    """A timezone-naive expires_at (e.g., from operator tampering or a buggy migration)
    cannot be safely compared to the timezone-aware `now`; the verifier must reject
    rather than raise TypeError mid-request.
    """
    db_path = tmp_path / "pats.db"
    store = SQLitePatStore(db_path)
    pat_hash = "c" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pats (friend_login, pat_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            ("dave", pat_hash, datetime.now(UTC).isoformat(), "2099-01-01T00:00:00"),
        )
    assert store.verify(pat_hash) is None


def test_malformed_expires_at_fails_closed(tmp_path: Path) -> None:
    """An expires_at that does not parse as ISO 8601 must fail verification rather
    than sort lexicographically past valid timestamps and be treated as "never
    expires." Real entry paths always write isoformat output; this guards corrupt
    or hand-edited rows.
    """
    db_path = tmp_path / "pats.db"
    store = SQLitePatStore(db_path)
    pat_hash = "b" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pats (friend_login, pat_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            ("carol", pat_hash, datetime.now(UTC).isoformat(), "zzz-not-a-timestamp"),
        )
    assert store.verify(pat_hash) is None


def test_corrupt_capability_value_fails_closed(tmp_path: Path) -> None:
    """A row with a `capability` outside the enum (e.g., manually injected "admin")
    must fail verification rather than fall back to readwrite. The SQL DDL constrains
    the column to TEXT only, so corruption is the only realistic source of an
    unexpected value — fail closed is the safer posture for a security boundary.
    """
    db_path = tmp_path / "pats.db"
    store = SQLitePatStore(db_path)
    pat_hash = "a" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pats (friend_login, pat_hash, created_at, capability) "
            "VALUES (?, ?, ?, ?)",
            ("bob", pat_hash, datetime.now(UTC).isoformat(), "admin"),
        )
    assert store.verify(pat_hash) is None


def test_verify_sql_does_not_filter_by_expires_or_capability() -> None:
    """SEC-001 timing: the SQL used by SQLitePatStore.verify MUST NOT push expires_at,
    capability, or revoked_at into the WHERE clause. The query plan for an unknown
    token, a revoked token, and an expired token must be identical (a single primary-key
    lookup on pat_hash). The checks live in Python after fetch.
    """
    sql = SQLitePatStore._VERIFY_SQL.lower()
    where = sql.split("where", 1)[1] if "where" in sql else ""
    assert "expires_at" not in where
    assert "capability" not in where
    assert "revoked_at" not in where
    assert "pat_hash" in where


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
