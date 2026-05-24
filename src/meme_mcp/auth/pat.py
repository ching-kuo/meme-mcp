from __future__ import annotations

import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class PatRecord:
    friend_login: str
    pat_hash: str
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class InMemoryPatStore:
    def __init__(self) -> None:
        self.records: list[PatRecord] = []


class SQLitePatStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    friend_login TEXT NOT NULL,
                    pat_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def issue(self, friend_login: str, pat_hash: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pats SET revoked_at = ? WHERE friend_login = ? AND revoked_at IS NULL",
                (now, friend_login),
            )
            conn.execute(
                "INSERT INTO pats (friend_login, pat_hash, created_at) VALUES (?, ?, ?)",
                (friend_login, pat_hash, now),
            )

    def verify(self, pat_hash: str) -> str | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT friend_login FROM pats WHERE pat_hash = ? AND revoked_at IS NULL",
                (pat_hash,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE pats SET last_used_at = ? WHERE pat_hash = ?", (now, pat_hash))
            return str(row[0])


def hash_pat(plaintext: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), plaintext.encode(), "sha256").hexdigest()


def issue_pat(store: InMemoryPatStore | SQLitePatStore, friend_login: str, pepper: str) -> str:
    now = datetime.now(UTC)
    plaintext = secrets.token_urlsafe(32)
    digest = hash_pat(plaintext, pepper)
    if isinstance(store, SQLitePatStore):
        store.issue(friend_login, digest)
        return plaintext
    for record in store.records:
        if record.friend_login == friend_login and record.revoked_at is None:
            record.revoked_at = now
    store.records.append(PatRecord(friend_login, digest, now))
    return plaintext


def verify_pat(store: InMemoryPatStore | SQLitePatStore, plaintext: str, pepper: str) -> str | None:
    digest = hash_pat(plaintext, pepper)
    if isinstance(store, SQLitePatStore):
        return store.verify(digest)
    for record in store.records:
        if record.revoked_at is None and hmac.compare_digest(record.pat_hash, digest):
            record.last_used_at = datetime.now(UTC)
            return record.friend_login
    # Keep one compare on the failure path so obviously short-circuit timing is avoided.
    hmac.compare_digest(digest, "0" * 64)
    return None
